import base64
import hashlib
import os
import pwd

import pytest

from cluster_execd.runner import make_workdir


def collect_req(run_id="c_1", **extra):
    req = {"v": 1, "run_id": run_id, "kind": "collect", "paths": ["out/*", "out/**/*"]}
    req.update(extra)
    return req


def files_from(events):
    """Reassemble streamed files the way the agent does: file, data..., file_end."""
    files, current, chunks = {}, None, []
    for e in events:
        if e["ev"] == "file":
            current, chunks = e, []
        elif e["ev"] == "data":
            chunks.append(base64.b64decode(e["data"]))
        elif e["ev"] == "file_end":
            data = b"".join(chunks)
            assert hashlib.sha256(data).hexdigest() == e["sha256"]
            files[current["name"]] = (data, current["truncated"])
    return files


def skipped(events):
    done = [e for e in events if e["ev"] == "collected"]
    assert done, events
    return {s["name"]: s["reason"] for s in done[-1]["skipped"]}


def populate(ctx, run_id, owner=None):
    workdir = make_workdir(ctx, run_id)
    out = os.path.join(workdir, "out")
    os.mkdir(out)
    os.makedirs(os.path.join(out, "sub"))
    with open(os.path.join(out, "result.json"), "w") as f:
        f.write('{"ok": true}')
    with open(os.path.join(out, "sub", "deep.txt"), "w") as f:
        f.write("deep")
    with open(os.path.join(out, "big.bin"), "wb") as f:
        f.write(b"x" * 5000)
    os.symlink("/etc/passwd", os.path.join(out, "passwd_link"))
    os.symlink("/etc", os.path.join(out, "etc_dir_link"))
    os.mkfifo(os.path.join(out, "pipe"))
    if owner is not None:
        for root, dirs, names in os.walk(workdir):
            for n in dirs + names:
                os.lchown(os.path.join(root, n), owner, owner)
    return workdir, out


async def test_collects_regular_files_and_skips_links(make_server, ctx):
    workdir, out = populate(ctx, "c_1")
    client = await make_server(ctx)
    events = await client.call(collect_req(max_file_bytes=1000))
    files = files_from(events)
    assert files["out/result.json"] == (b'{"ok": true}', False)
    assert files["out/sub/deep.txt"] == (b"deep", False)
    assert files["out/big.bin"] == (b"x" * 1000, True)  # capped and marked truncated
    skip = skipped(events)
    assert skip["out/passwd_link"] == "symlink"
    assert skip["out/pipe"] == "not_regular"
    assert not any(name.startswith("out/etc_dir_link/") for name in files)


async def test_missing_run_directory(make_server, ctx):
    client = await make_server(ctx)
    events = await client.call(collect_req(run_id="nope"))
    assert events[-1]["ev"] == "rejected"


async def test_file_count_limit(make_server, ctx):
    populate(ctx, "c_2")
    client = await make_server(ctx)
    events = await client.call(collect_req(run_id="c_2", max_files=1))
    assert len(files_from(events)) == 1
    assert "too_many_files" in skipped(events).values()


async def test_cleanup_removes_tree_without_following_links(make_server, ctx, tmp_path):
    keep = tmp_path / "keep"
    keep.mkdir()
    (keep / "precious").write_text("x")
    workdir = make_workdir(ctx, "c_3")
    os.symlink(str(keep), os.path.join(workdir, "link"))
    client = await make_server(ctx)
    events = await client.call({"v": 1, "run_id": "c_3", "kind": "cleanup"})
    assert events == [{"ev": "done"}]
    assert not os.path.exists(workdir)
    assert (keep / "precious").exists()


@pytest.mark.root
async def test_collect_as_run_user_refuses_root_files(make_server, root_ctx, worker_python):
    uid = pwd.getpwnam(root_ctx.run_user).pw_uid
    workdir, out = populate(root_ctx, "c_4", owner=uid)
    # a root-owned file and a hardlink to it planted in the output directory
    root_file = os.path.join(out, "rootfile")
    with open(root_file, "w") as f:
        f.write("root secret")
    os.chown(root_file, 0, 0)
    os.chmod(root_file, 0o644)
    os.link(root_file, os.path.join(out, "hardlink"))
    client = await make_server(root_ctx)
    events = await client.call(collect_req(run_id="c_4"))
    files = files_from(events)
    skip = skipped(events)
    assert "out/result.json" in files
    assert skip["out/rootfile"] == "hardlink"  # two links now; also not owned by cluster-run
    assert skip["out/hardlink"] == "hardlink"
    assert skip["out/passwd_link"] == "symlink"


@pytest.mark.root
async def test_collect_foreign_owner(make_server, root_ctx, worker_python):
    uid = pwd.getpwnam(root_ctx.run_user).pw_uid
    workdir, out = populate(root_ctx, "c_5", owner=uid)
    other = os.path.join(out, "other")
    with open(other, "w") as f:
        f.write("not yours")
    os.chown(other, 0, 0)
    os.chmod(other, 0o644)
    client = await make_server(root_ctx)
    events = await client.call(collect_req(run_id="c_5"))
    assert skipped(events)["out/other"] == "foreign_owner"

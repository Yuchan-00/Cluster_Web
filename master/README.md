# cluster-master

Cluster Web의 마스터 프로세스 (Phase 2 코어). RDK X3 `rdkx3-01`에서 `cluster-master` 계정으로 돌며,
노드 에이전트의 WebSocket 허브, 웹/서비스/관리자 API, 감사 로그를 한 프로세스에서 제공한다.

설계: `docs/PLAN.md` (4, 12, 13장), `docs/design/security.md`, 프로토콜: `docs/protocol.md`.

## 리스너 (security.md 4.1)

| 이름 | 주소 | 경로 | 누가 |
|---|---|---|---|
| web | `127.0.0.1:8000` | `/api/*`, `/ws/ui` | 사용자 (앞에 `tailscale serve`) |
| agent | `127.0.0.1:8001` | `/ws/agent` | 노드 에이전트 (앞에 Caddy, `master.cluster.internal:443`) |
| internal | `/run/cluster-master/internal.sock` 0660 root:cluster-svc | `/internal/api/*` | 서비스 토큰 (`cst_…`: telegram-bot, ai-operator) |
| admin | `/run/cluster-master/admin.sock` 0600 root | `/internal/admin/*` | `cluster-master-admin` (root) |

네 리스너는 서로 다른 FastAPI 앱이다. 다른 리스너의 경로는 핸들러에 닿기 전에 404가 난다.
모든 라우트는 `require(...)`로 호출자를 선언해야 하고, 선언이 빠진 라우트가 있으면 기동이 실패한다
(`tests/route_snapshot.txt`가 전체 표).

## 설치

```bash
sudo useradd --system --home /var/lib/cluster-master --shell /usr/sbin/nologin cluster-master
sudo groupadd --system cluster-svc
sudo install -d -o cluster-master -g cluster-master -m 0750 /var/lib/cluster-master
sudo install -d -m 0750 /etc/cluster-master
# Python >= 3.10 (RDK OS Ubuntu 22.04)
sudo -u cluster-master python3 -m venv /var/lib/cluster-master/venv
sudo -u cluster-master /var/lib/cluster-master/venv/bin/pip install ./common ./master
```

`/etc/cluster-master/config.yaml` (모든 키는 선택, 모르는 키는 오류):

```yaml
data_dir: /var/lib/cluster-master
log_level: INFO
listeners:
  web:   {host: 127.0.0.1, port: 8000}
  agent: {host: 127.0.0.1, port: 8001}
  internal: {path: /run/cluster-master/internal.sock, mode: "0660", group: cluster-svc}
  admin:    {path: /run/cluster-master/admin.sock, mode: "0600"}
agent:
  metrics_interval_s: 5
  offline_after_s: 15
web:
  origins: ["https://master.<tailnet>.ts.net"]
```

web/agent 리스너는 loopback 주소만 허용된다. 외부 노출은 `tailscale serve`와 Caddy의 일이다 (Phase 6).

## 운영

```bash
cluster-master --check                     # 설정 검증
cluster-master                             # systemd 유닛은 Phase 6에서 추가
cluster-master-admin status
cluster-master-admin node register rpi3-01 --board rpi3 --label zone=shelf-a --slots 2 \
    --token-file /root/rpi3-01.token        # 토큰은 이때 한 번만 나온다 (DB에는 해시만)
cluster-master-admin node list
cluster-master-admin node rotate rpi3-01    # 새 토큰, 접속 중인 에이전트는 끊긴다
cluster-master-admin node revoke rpi3-01    # 즉시 거부
cluster-master-admin node set rpi3-01 --sched-state cordoned --reason "SD card check"
cluster-master-admin service-token create telegram-bot --scope read
cluster-master-admin lockdown on --reason "suspicious login"
cluster-master-admin audit verify           # 해시 체인 검증 (exit 1이면 변조)
cluster-master-admin audit tail -n 20
```

마스터가 돌고 있으면 CLI는 `admin.sock`으로 요청해 즉시 반영된다. 아니면 DB를 직접 열어 같은 일을
하고, 마스터는 다음 기동 때 읽는다. 두 경우 모두 감사 로그에 `cli:<sudo 사용자>`로 남는다.

## 개발 / 모의 클러스터

```bash
scripts/dev_cluster.sh            # 마스터(--dev) + 모의 노드 5개 (ws://127.0.0.1:8001)
curl -s http://127.0.0.1:8000/api/nodes | python3 -m json.tool
```

`--dev DATA_DIR`는 loopback 리스너와 `dev.unauthenticated_admin`(모든 loopback 요청이 admin)을 켠다.
Phase 3의 로그인이 들어오기 전까지의 개발용이며, 실제 설정 파일에서는 web 리스너가 loopback일 때만
허용된다.

```bash
cd master
uv run --python 3.10 --extra dev -- python -m pytest -q      # 90 tests, ~12 s
uvx ruff@0.16.10 check . && uvx ruff@0.16.10 format --check .
```

## 데이터

- SQLite (WAL) `data_dir/master.db`, 0600. 스키마는 `cluster_master/migrations/NNNN_*.sql`로
  기동 시 적용된다. 표준 라이브러리 `sqlite3`만 쓴다 (ORM 없음): 감사 체인은 `BEGIN IMMEDIATE`로
  이전 해시를 읽고 쓰는 순서를 직접 제어해야 한다.
- `audit_log`는 UPDATE/DELETE 트리거로 막혀 있고 각 행은 `sha256(prev_hash ‖ canonical_json(row))`로
  이어진다. 상세(`detail`)는 저장 전에 `cluster_common.redact`로 비밀을 가린다.
- 메트릭: 메모리 링 버퍼(노드당 720 샘플) + 1분 롤업 `metrics_1m` (30일 보관).
- `/etc/cluster-master/credentials/` 또는 systemd `LoadCredential`: 0600이 아닌 비밀 파일은 거부한다.

## 아직 없는 것 (다음 Phase)

- 로그인/TOTP/RBAC 세션 (Phase 3), 명령 실행 API와 승인 (Phase 4), 알림 임계값과 Telegram (Phase 5),
  systemd 유닛·Caddy·tailscale serve (Phase 6), 작업 큐 (Phase 7–8), AI 오퍼레이터 (Phase 9+).
- 명령 run은 현재 메모리에만 있다. 마스터 재시작 뒤 끝난 run의 결과(`pending_results`)는 경고 로그로
  버려진다 (Phase 4에서 `commands` 테이블로 영속화).

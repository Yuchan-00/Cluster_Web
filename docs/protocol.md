# Agent ↔ Master 프로토콜 (v1)

Phase 2 기준 구현 명세. 설계 배경은 `docs/PLAN.md` 12.1, 보안 요구는 `docs/design/security.md` 8장.
구현: 에이전트 `agent/cluster_agent/agent.py`, 마스터 `master/cluster_master/ws/agent_hub.py`,
메시지 검증 `master/cluster_master/models.py`.

## 1. 연결

- 방향: 에이전트 → 마스터 (push). 마스터는 어떤 노드에도 먼저 연결하지 않는다.
- URL: `wss://master.cluster.internal/ws/agent` (Caddy가 TLS 종단, 마스터는 `127.0.0.1:8001`에서
  `ws://`로 받는다). 모의 클러스터는 loopback `ws://`만 허용.
- 인증은 **업그레이드 요청 헤더**로만 한다. 메시지 본문에 토큰이 들어가는 일은 없다.

  ```
  GET /ws/agent HTTP/1.1
  Authorization: Bearer cat_<43 base64url chars>
  X-Node-Id: rpi3-01
  ```

- 마스터는 `X-Node-Id`로 노드를 찾고 저장된 SHA-256 해시와 상수 시간 비교한다. 노드가 없거나,
  토큰이 폐기(revoke)됐거나, 형식이 틀리면 **HTTP 401** (WebSocket 핸드셰이크 거절). 거절 응답을
  보낼 수 없는 서버 구현에서는 close `4401`.
- 같은 peer에서 10분 안에 5번 실패하면 `agent_auth_failures` 알림(critical). 감사 로그에는 peer당
  10분 창마다 한 줄만 남긴다 (로그 폭주 방지).
- 핸드셰이크 뒤 **첫 메시지는 `hello`**여야 하고 `hello_timeout_s`(기본 10초) 안에 도착해야 한다.
  `hello.node_id`는 헤더의 `X-Node-Id`와 같아야 한다. 다르면 close `4403` + `agent_identity`
  알림 + 감사 기록 (토큰이 다른 머신에서 쓰이고 있다는 신호).
- 노드당 연결은 하나. 이미 살아 있는 연결(마지막 메시지가 `2 × metrics_interval` 이내)이 있으면
  새 연결을 close `4409`로 끊고 `agent_duplicate` 알림(critical) + 감사 기록. 기존 연결이 그보다
  오래 조용하면 죽은 것으로 보고 기존 연결을 `4409`로 닫고 새 연결을 받는다 (알림 없음).

### Close 코드

| 코드 | 의미 | 에이전트 동작 |
|---|---|---|
| 1001 | 마스터 종료 | 백오프 재접속 |
| 1008 | 프로토콜 위반 (잘못된 hello, 위반 누적 10회, hello 타임아웃, 바이너리 프레임) | 백오프 재접속 |
| 1009 | 메시지 너무 큼 (`static_info` > 16 KiB, 프레임 > 1 MiB) | 백오프 재접속 |
| 4401 | 인증 실패 / 토큰 폐기·교체 / 노드 삭제 | 300초 뒤 재시도 (`AUTH_RETRY_S`) |
| 4403 | identity 불일치 | 300초 뒤 재시도 |
| 4408 | `offline_after_s`(기본 15초) 동안 메시지 없음 | 백오프 재접속 |
| 4409 | 중복 연결 | 60초 뒤 재시도 |
| 4429 | 속도 제한 초과 | 60초 뒤 재시도 |

## 2. 메시지 형식과 한도

- 모든 메시지는 UTF-8 JSON 객체 텍스트 프레임. 바이너리 프레임은 위반.
- 프레임 ≤ 1 MiB, 연결당 50 msg/s · 1 MiB/s (토큰 버킷, burst = 1초치). 초과 시 `4429`.
- `metrics`는 2초보다 자주 보낼 수 없다 (더 빠른 것은 버리고 위반 1회).
- `metrics.data.extra`는 허용 키만 남긴다: `bpu`, `throttled`, `core_volts`, `reboot_required`,
  `isolation_mode`. 필터 후에도 4 KB 또는 64키를 넘으면 `extra`를 비우고 위반 1회.
- `cmd_output.data`는 직렬화 기준 64 KiB 이하.
- `NaN`/`Infinity`는 JSON이 아니므로 어디에 있어도 위반.
- 알 수 없는 최상위 필드는 위반 (마스터→에이전트 방향은 반대로, 모르는 타입은 무시한다: 구 버전
  에이전트가 새 마스터와 공존할 수 있게).
- 위반 10회 누적 시 close `1008`. 위반은 로그에 남고 연결마다 센다.

## 3. 에이전트 → 마스터

### `hello` (첫 메시지, 1회)

```json
{
  "type": "hello",
  "node_id": "rpi3-01",
  "board": "rpi3",
  "agent_version": "0.1.0",
  "static_info": {"hostname": "...", "os": "...", "kernel": "...", "arch": "...",
                  "cpu_model": "...", "cpu_count": 4, "mem_total": 1073741824,
                  "boot_time": 1700000000.0, "device_model": "...", "interfaces": {},
                  "python": "3.11.2", "bpu_cores": null,
                  "labels": {"...": "..."}, "capacity": {"slots": 2}, "isolation": "systemd",
                  "node_policy": {"...": "..."}},
  "running_tasks": [], "unacked_results": [], "orphaned": [],
  "running_commands": ["<run_id>", "..."],
  "pending_results": ["<run_id>", "..."]
}
```

- `node_id`: `^[a-z0-9][a-z0-9-]{0,62}$`. `board`: `rpi3 | rdkx3 | generic`.
- `static_info`는 **참고 정보**다. 특히 `labels`/`capacity`는 레지스트리의 관리자 설정과 다르면
  경고만 남기고 무시한다 (security.md 8.3). `board`가 등록값과 다르면 `node_board_mismatch` 알림.
- `running_commands`: 재접속 시 아직 실행 중인 run_id. 마스터가 발급한 적 없는 id는 경고 로그.
- `pending_results`: 끊긴 동안 끝난 결과. `welcome` 직후 `cmd_result`로 이어서 보낸다.
- `running_tasks`/`unacked_results`/`orphaned`: Phase 7 작업 큐용, 지금은 빈 배열.

### `metrics` (주기, 기본 5초)

```json
{
  "type": "metrics",
  "ts": 1700000000.123,
  "data": {
    "cpu": {"percent": 12.5, "per_core": [...], "freq_mhz": 1200, "load": [0.1, 0.1, 0.1]},
    "mem": {"total": 0, "available": 0, "used": 0, "percent": 40.0},
    "swap": {"total": 0, "used": 0, "percent": 0.0},
    "disk": [{"mount": "/", "fstype": "ext4", "total": 0, "used": 0, "percent": 37.5}],
    "disk_io": {"read_bps": 0, "write_bps": 0},
    "net": {"eth0": {"rx_bps": 0, "tx_bps": 0}},
    "temp_c": 45.5, "uptime_s": 100, "procs": 99,
    "top": [{"pid": 1, "name": "systemd", "user": "root", "cpu": 0.1, "mem": 1.2}],
    "extra": {"throttled": "0x0"}
  },
  "sched": {"free_slots": 2, "free_bpu_slots": 0, "job_mem_free_mb": 384,
            "running": [], "cached_bundles": []}
}
```

- `ts`는 에이전트 벽시계(양수). 마스터는 수신 시각도 따로 기록한다.
- `data` 안의 키는 수집기가 늘릴 수 있다. 마스터는 `cpu.percent`, `mem.percent`, `temp_c`,
  `disk[mount="/"].percent`, `net.*.rx_bps/tx_bps`, `extra.bpu`를 요약(`Sample`)해 링 버퍼(노드당 720개)
  와 1분 롤업(`metrics_1m`)에 넣고, 원본 `data`는 "최신 1개"만 메모리에 둔다.
- `sched`는 Phase 7 스케줄러용. 지금은 그대로 저장해 `/api/nodes/{id}`에 노출.

### `cmd_output`

```json
{"type": "cmd_output", "run_id": "<id>", "stream": "stdout", "data": "..."}
```

- `run_id`가 **이 연결의 노드에 마스터가 발급한 run**이어야 한다. 아니면 위반 (다른 노드의 출력을
  위조할 수 없다).
- `stream`: `stdout | stderr`. 에이전트는 5000자 조각으로 나눠 보낸다.

### `cmd_result`

```json
{"type": "cmd_result", "run_id": "<id>",
 "status": "ok", "exit_code": 0, "duration_ms": 12, "output_bytes": 7,
 "truncated": false, "reason": null, "dropped_bytes": 0}
```

- `status`: `ok | error | timeout | cancelled | oom | scheduled | rejected | failed_to_start`.
- 같은 소유권 규칙. 수신 시 run이 종료되고 `command.finished` 이벤트가 난다.
- 마스터가 run을 발급한 뒤 `timeout + 120초` 안에 결과가 없으면 마스터가 합성 결과
  (`failed_to_start` 또는 `error`, reason `no result from the agent`)로 종료시킨다.

### `pong`

`{"type": "pong"}` — 예약. 무시된다 (WebSocket 레벨 ping/pong은 uvicorn이 20초 주기로 처리).

## 4. 마스터 → 에이전트

### `welcome` (hello 응답)

```json
{"type": "welcome", "metrics_interval": 5, "server_time": 1700000000.0,
 "lockdown": false, "lease_ttl_s": 45, "work_request_interval_s": 5}
```

- `metrics_interval`: 에이전트는 1~60초로 clamp.
- `lockdown: true`면 에이전트는 즉시 lockdown 상태로 들어가 모든 `exec`를 `rejected`로 답한다.

### `exec`

```json
{"type": "exec", "run_id": "<[A-Za-z0-9_-]{1,64}>",
 "mode": "shell", "command": "echo hi",
 "as_root": false, "timeout": 600, "network": "internet",
 "limits": {"mem_mb": 256}, "env": {"CW_X": "1"}}
```

- `mode: preset`이면 `argv` 배열, `root_op`가 있으면 `params`와 함께 execd 정책 템플릿 실행.
- `network`: `none | lan | internet`. `limits`/`env`는 execd가 clamp·allow-list한다.
- 에이전트가 모르는 run_id 형식, 중복 run_id, lockdown 중의 exec는 각각 무시/무시/`rejected`.

### `cancel`

`{"type": "cancel", "run_id": "<id>"}` — 시작 중인 run도 포함해 종료.

### `config`

`{"type": "config", "metrics_interval": 10}` — 런타임 조정.

### `lockdown` / `unlock`

`{"type": "lockdown"}` / `{"type": "unlock"}` — 전 노드 브로드캐스트. lockdown은 실행 중인 모든 run을
취소시키고 (`cancel_all`), 마스터는 발급해 둔 run을 `cancelled`(reason `lockdown`)로 종료시킨다.

## 5. 마스터 측 상태 전이

```
(없음) --hello ok--> online --메시지--> online
online --agent close / 4408 / 4401(revoke, remove)--> offline  → node_offline 알림(warning)
offline --hello ok--> online → node_offline 알림 resolve
```

- `last_seen`/`last_ip`는 DB에 노드당 60초마다 한 번(그리고 접속·해제 시) 기록한다.
- `node.online`, `node.offline`, `metrics`, `cmd_output`, `cmd_result`, `command.finished`,
  `alert.raised/resolved`, `system.lockdown` 이벤트가 이벤트 버스로 나가고, `/ws/ui`는 그중
  `cmd_output`을 제외한 것을 브라우저에 중계한다.

## 6. 브라우저 `/ws/ui`

- 웹 리스너의 인증(Phase 2: loopback dev admin, Phase 3: 세션)을 통과해야 한다.
- `Origin` 헤더가 있으면 `web.origins` 또는 요청 `Host`와 같은 출처여야 한다. 아니면 HTTP 403.
- 서버 → 클라이언트: `{"type": "hello", "user": ..., "role": ...}` 뒤
  `{"type": "event", "event": "<name>", "ts": ..., "data": {...}}`.
- 클라이언트 → 서버: `{"type": "ping"}` → `{"type": "pong"}`,
  `{"type": "subscribe", "events": ["metrics", ...]}`. 그 외/4 KB 초과/비JSON은 close `1008`.
- 256개 이벤트 이상 밀린 느린 클라이언트는 close `4000` (재접속 후 REST로 재동기화).

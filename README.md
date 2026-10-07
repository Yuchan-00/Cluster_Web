# Cluster Web

RDK X3 2대와 Raspberry Pi 3B 3대로 구성한 클러스터를 관리하는 웹 서비스입니다. 노드 상태를 실시간으로 모니터링하고, 웹과 텔레그램에서 명령을 내리고, 분산 작업을 실행합니다. 작업이 끝나면 텔레그램으로 보고하고, master의 AI 에이전트가 자연어 지시를 사람 승인 아래에서 수행합니다. 보안을 최우선으로 설계했습니다.

- 마스터 계획서: [docs/PLAN.md](docs/PLAN.md)
- 세부 설계: [topology](docs/design/topology.md) · [security](docs/design/security.md) · [jobs](docs/design/jobs.md) · [telegram](docs/design/telegram.md) · [ai-agent](docs/design/ai-agent.md)

## 진행 상황

| Phase | 내용 | 상태 |
|---|---|---|
| 0 | 환경 준비, 실기기 확인 (topology.md 8장 체크리스트) | 하드웨어에서 진행 필요 |
| 1 | Agent + cluster-execd | 구현됨. 실기기 확인은 Phase 0·6에서 |
| 2 | Master 코어: 4개 리스너, 에이전트 허브, 노드 레지스트리, 감사 체인, 관리자 CLI | 구현됨 (`master/README.md`, `docs/protocol.md`). 모의 노드 5대로 검증 |
| 3~12 | 로그인·대시보드, 명령, 텔레그램, 배포·외부 접속, 잡, AI | 예정 (PLAN.md 18장) |

## 저장소 구성

| 경로 | 내용 |
|---|---|
| `master/` | master (rdkx3-01): 에이전트 WebSocket 허브, 웹/서비스/관리자 API, SQLite + 감사 체인, `cluster-master-admin` |
| `agent/` | 노드 agent (비root): 메트릭 수집, master와 TLS WebSocket 연결, 실행은 execd에 위임 |
| `execd/` | root 실행 위임 데몬: 노드 로컬 정책, systemd 샌드박스 실행, 안전한 산출물 수집 |
| `common/` | master와 agent가 공유하는 코드: 비밀 마스킹(`redact`), canonical JSON |
| `deploy/` | systemd 유닛, 설치 스크립트, 내부 CA 스크립트 |
| `scripts/` | `dev_cluster.sh`: 개발용 master + 모의 노드 5대 |
| `docs/` | 계획서, 설계 문서, `protocol.md`(agent↔master 프로토콜) |

## 개발

```bash
cd agent && uv run --extra dev -- python -m pytest -q                 # execd/, common/도 같은 방식
cd master && uv run --python 3.10 --extra dev -- python -m pytest -q  # master는 Python >= 3.10
scripts/dev_cluster.sh                                                 # master + 모의 노드 5대 (loopback)
```

CI는 agent·execd를 Python 3.8·3.11·3.13에서, master를 3.10·3.12·3.13에서, common을 3.8·3.13에서 lint·테스트합니다. root 권한과 systemd가 필요한 execd 통합 테스트(uid 전환, 수집 공격 차단, 샌드박스 속성)는 별도 잡에서 실행합니다.

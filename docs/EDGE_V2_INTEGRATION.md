# 엣지 v2 수신 API

백엔드 수신 API 구현 요청서 `2.0-r1`에 맞춰 관측·판단·장비 상태·설정을 독립 수신한다. 기존 `/api/v1` 계약은 유지하며, 실제 엣지의 운영 전환은 별도 통합 시험 이후 진행한다.

## API와 인증

| 메서드 | 경로 | 내용 |
| --- | --- | --- |
| POST | `/api/edge/v2/measurements` | 정기 관측 및 추가 근거 스캔 |
| POST | `/api/edge/v2/assessments` | 현재 판단 및 사건 개정 |
| POST | `/api/edge/v2/status` | 센서·서비스·전송·저장 상태 |
| POST | `/api/edge/v2/configurations` | 좌표계·각도 구간·보정 버전 등록 |
| GET | `/api/edge/v2/devices/{site_id}/{edge_id}/status` | 대시보드 세션으로 장비 연결 상태 조회 |

수신 요청은 `Content-Type: application/json`, `Authorization: Bearer <device-token>`, 본문의 `message_id`와 같은 `Idempotency-Key`가 필요하다. 압축 요청은 `415`로 거부한다. 기존 `X-Edge-API-Key`나 공용 `EDGE_API_KEY`는 v2 인증에 사용하지 않는다.

비공개 `.env`의 `EDGE_V2_DEVICES`에 장치별 허용 범위를 JSON 배열로 설정한다. 기본값 `[]`에서는 모든 장치의 v2 인증을 거부한다. 아래 값은 실행용 인증정보가 아닌 형식 설명이다.

```dotenv
EDGE_V2_DEVICES='[{"site_id":"site-demo","edge_id":"edge-demo","sensor_ids":["lidar-a","lidar-b"],"token":"REPLACE_WITH_RANDOM_DEVICE_TOKEN"}]'
```

각 장치에는 서로 다른 충분히 긴 임의 토큰을 발급한다. 로컬에서 `uv run python -c 'import secrets; print(secrets.token_urlsafe(32))'`로 생성하고 비공개 경로로 장치에 전달한다. 실제 토큰을 공개 문서·로그·이슈에 기록하지 않는다. 토큰에 등록된 현장·장치와 본문이 다르면 `403`을 반환한다. 센서 ID 두 개도 실제 설치 식별자로 설정한다. 정기 관측은 두 센서 항목을 모두 유지하며, 추가 근거는 한 센서만 포함할 수 있다.

토큰 교체 시 같은 장치·센서에 새 토큰을 가진 항목을 추가하고 백엔드를 재시작한다. 엣지에서 새 토큰 사용을 확인한 뒤 기존 항목을 제거하고 다시 시작한다. 하나의 토큰을 여러 항목에 중복 등록하면 기동 설정 검증에 실패한다. Compose의 기존 `env_file`이 이 값을 전달하므로 별도 compose 환경 변수 편집은 필요하지 않다.

## 저장 ACK와 오류

신규 메시지는 DB 커밋 완료 후 `201`을 반환한다.

```json
{"message_id":"m-1","accepted":true,"duplicate":false,"received_at":"2026-09-20T01:00:01.030Z"}
```

같은 장치의 동일 메시지를 재전송하면 `200`, `duplicate: true`와 최초 `received_at`을 반환한다. JSON 객체의 키 순서·공백은 무시하지만 배열 순서와 본문 값은 유지해야 한다. `message_id`는 네 수신 API 전체에서 장치별로 유일해야 한다. 같은 ID의 다른 본문은 `409 IDEMPOTENCY_CONFLICT`다.

```json
{"message_id":"m-1","accepted":false,"error":{"code":"IDEMPOTENCY_CONFLICT","retryable":false,"field":null}}
```

형식 오류는 `400/422 SCHEMA_VALIDATION_FAILED`, 인증 실패는 `401 AUTHENTICATION_FAILED`, 저장 장애는 `503 STORAGE_UNAVAILABLE`과 `retryable: true`, `Retry-After: 1`로 응답한다. 오류에는 검증 입력이나 점 배열을 포함하지 않는다. 메시지 ID를 읽지 못한 경우 `message_id`는 `null`이다. 인증·전송 형식 검사에서 먼저 거부된 요청도 본문을 확인하지 않아 ID가 `null`일 수 있다.

설정·보정·스캔 결과·판단 개정 등의 식별자를 유지한 채 내용을 바꾸면 `409 ARTIFACT_CONFLICT`를 반환한다. 설정·보정·판단의 변경에는 각각 새 버전이나 개정 번호를 사용한다. 저장 실패 및 충돌에서는 해당 메시지와 함께 생성 중이던 색인을 모두 롤백한다.

## 데이터와 도착 순서

- v2 원본 거리·좌표는 **정수 mm**, 각도는 **정수 mdeg**, 판단의 적재 비율은 **0~1**로 보존한다. v1 화면 API의 cm·% 모델과 분리한다. 백엔드가 좌표를 적재 높이로 간주하거나 적재율을 재계산하지 않는다.
- 모든 시각은 UTC의 RFC3339 문자열이어야 한다. 순번은 `0..9007199254740991` 정수, 단조 ns는 10진 문자열이다. 정기 스캔 나이는 서버 도착 시각이 아닌 `generated_at` 기준으로 검사하므로 재전송 지연을 허용한다.
- 점 수·원본 index·각도 구간·null 좌표·센서 항목·100ms 짝맞춤의 일관성을 검증한다. 미검증 보정의 좌표는 null로 유지하고 SDK 거리·품질 숫자를 보존한다. 두 센서의 같은 index를 하나의 점으로 합치지 않는다.
- `UNAVAILABLE`과 `fill_estimate: null`을 0%로 바꾸지 않는다. 검증되지 않은 기능에는 숫자 적재율을 허용하지 않는다. 검증된 적재 판단도 이 변경에서는 원본으로 보관하며 v1 상태·경보 엔진이나 자동 수거·녹화를 호출하지 않는다.
- 관측·판단·설정은 도착 순서에 관계없이 독립 저장한다. 아직 없는 설정이나 스캔을 참조해도 ACK는 해당 메시지의 저장만 확인한다. 원본의 `scan_id`, 설정·보정·좌표계 및 판단 근거 ID를 보존해 이후 도착한 데이터와 연결할 수 있다.
- `scan_id`별 원본 스캔 메타데이터와 `scan_id + config_revision + calibration_version + coordinate_frame_id`별 결과를 구분한다. 반복 참조는 결과 색인을 늘리지 않는다. 다른 설정에서 선택된 점 배열은 결과별로 보존한다. 서로 다른 결과 버전의 모든 점을 합쳐 원본 전체 스캔을 복원하는 기능은 포함하지 않는다.
- 추가 근거는 원래 관측 시각을 보존하며 최신 정기 측정으로 갱신하지 않는다. 사건 개정은 높은 revision이 먼저 도착해도 낮은 revision의 별도 저장을 허용한다.

## 장비 상태 조회

현재 STATE 판정과 장비 상태를 함께 조회하는 대시보드 v2 API 및 SSE는 [MONITORING_V2.md](MONITORING_V2.md)를 참고한다. 피트·장비 연결을 명시적으로 설정하며 기존 v1 메트릭·경보 엔진은 호출하지 않는다.

조회 API에는 기존 대시보드 세션 쿠키가 필요하다. `snapshot`은 마지막 판단된 원본 상태이며, `effective_state`는 백엔드가 연결 상태까지 반영한 값이다.

최신 신규 status의 **서버 수신 시각**에서 30초가 지나면 `connection_state: UNKNOWN`, `effective_state: UNKNOWN`, `reason_codes: ["STATUS_NOT_RECEIVED"]`을 반환한다. 과거 snapshot을 유지하되 센서 고장이나 수거 완료로 확정하지 않는다. 같은 메시지의 재전송은 최초 수신 시각을 갱신하지 않는다.

같은 오케스트레이터 인스턴스에서는 높은 sequence의 snapshot을 선택한다. 인스턴스 간에는 `generated_at`으로 가장 새로운 인스턴스를 선택한다. 서로 다른 부팅의 단조 시각은 비교하지 않는다. 시계가 잘못된 여러 인스턴스의 실제 전후 관계를 서버가 추정하지 않으므로 현장 시각 동기화와 재부팅 시험이 필요하다.

## 배포와 검증

기존 DB 테이블 변경 없이 `edge_v2_messages`와 `edge_v2_artifacts`를 DB 초기화 때 생성한다. 메시지와 색인은 DB 고유 제약으로 동시 요청의 중복을 방지한다. 현재 저장 정책은 자동 삭제 없이 보존한다. 실제 점 수·생성량에 따라 운영 보관 기간·DB 용량·조회 색인을 확정한다.

운영 엣지는 HTTPS로 연결한다. TLS 종료 프록시에서 `/api/edge/v2/`를 백엔드로 전달하고 리다이렉트 없이 네 POST 경로가 열리는지 확인한다. 포함된 선택형 미디어 Nginx 게이트웨이도 이 경로를 지원한다. 요청 byte 상한은 계약상 미확정이므로 v2 위치에는 Nginx 기본 1MiB 제한을 적용하지 않는다. 실제 JSON 크기를 실측해 엣지 담당자와 합의한 제한을 모든 프록시와 수신 계층에 함께 적용한다. Pi의 4GiB 저장 한도를 HTTP 요청 한도로 사용하지 않는다.

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest -v
docker build -t scrap-backend-issue27:local .
uv run python tests/integration/verify_media_gateway.py --image scrap-backend-issue27:local
```

계약 테스트는 합성 데이터로 64KiB 초과 점 배열, 부분·빈 관측, null 좌표, 짝맞춤, 순서 역전, ACK 유실, 동시 중복, 재시작, 충돌, 인증·형식 오류, 저장 실패 및 30초 무보고를 검증한다. 마지막 명령은 Docker가 필요하며 실제 Nginx를 거치는 1MiB 초과 합성 요청과 카메라 경로를 확인한다. 실제 엣지의 1초 주기 전송·HTTPS·요청 크기·5초 ACK 시간은 배포 환경에서 별도 통합 시험한다.

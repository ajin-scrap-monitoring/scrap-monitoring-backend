# 라이다 v2 경보 및 수거 사건 이력

검증된 현재 STATE 판정으로 위험 상태와 경보를 기록하고, COLLECTION 사건의 개정 이력을 조회한다. 기존 v1 수신·상태 엔진과 기존 수거 완료 API는 유지한다. v2 점 배열에서 높이·적재율을 계산하지 않는다.

## 활성화와 기준

실제 수정할 파일과 컨테이너 적용 명령은 [V2_ALERT_DEPLOYMENT.md](V2_ALERT_DEPLOYMENT.md)에 정리되어 있다. 기본 배포의 `.env`와 별도 테스트 백엔드의 환경 설정 파일을 구분한다.

먼저 [MONITORING_V2.md](MONITORING_V2.md)에 따라 장치와 피트를 연결한다. 경보 처리는 기본적으로 꺼져 있으며 비공개 `.env`에서 다음과 같이 켠 뒤 백엔드를 재시작한다.

```dotenv
EDGE_V2_OPERATIONS_ENABLED=true
EDGE_V2_CRITICAL_THRESHOLD=90
ALERT_COOLDOWN_SECONDS=1800
```

명시적인 피트 설정이 없으면 WARNING은 기존 기본 경고 임계치(75%), CRITICAL은 v2 기본 임계치(90%)다. CRITICAL에 진입하면 85% 이하가 될 때까지 유지한다. DB의 피트 임계치가 있으면 그 값을 우선하고, 복귀 기준은 위험 임계치에서 5%p를 뺀 값이다. 예를 들어 위험 진입 95% 설정에서는 90% 이하에서 복귀한다. `0 ≤ 경고 ≤ 복귀 < 위험 ≤ 100`을 만족하지 않는 설정은 경보 처리에서 건너뛴다. 기존 v1의 기본 임계치와 상태 전이 코드는 변경하지 않는다.

경보 비교에는 원본 ratio를 %로 바꾼 값을 사용하고 화면 표시용 반올림 값은 사용하지 않는다. 피트 ID는 기존 피트·알림 DB의 길이와 일치하도록 최대 50자다.

현재 판정이 FRESH이고 숫자 적재율이 있는 경우에만 처리한다. 시각 이상, 관측 지연, status 미수신, 설정 버전 불일치, 미검증 기능 및 판정 불가는 알림을 만들거나 기존 위험 상태를 초기화하지 않는다. 도착이 늦은 STATE, EVENT 또는 같은 관측 시각의 다른 메시지도 처리된 상태를 되돌리지 않는다. 처리한 STATE보다 생성 시각과 관측 시각이 모두 새로운 STATE만 다음 평가에 사용한다.

수신 API는 원본 저장 후 ACK하며 경보 작업자를 기다리지 않는다. 작업자는 약 1초마다 현재 판정만 확인한다. 따라서 과거 데이터 전체나 짧게 지나간 모든 중간 상태를 재생하는 기능은 아니다. 작업자가 내려가 있을 때의 오래된 위험 관측을 뒤늦게 새 경보로 발송하지 않는다. 새 status가 나중에 도착하면, 여전히 신선한 미처리 STATE를 평가할 수 있다.

## 영속 상태와 알림

DB 초기화 시 `edge_v2_operations`와 `edge_v2_alert_links` 두 테이블을 추가한다. 기존 테이블을 수정하지 않는다. `edge_v2_operations`에 마지막 처리 메시지·생성/관측 시각·위험 상태·마지막 경보 시각을 보관한다. 경보의 `AlertLog`, 원본 판정 연결 및 처리 상태는 같은 트랜잭션으로 커밋한다. 실패 시 함께 롤백하고 다음 회차에 재시도한다.

위험 상태가 지속되어도 마지막 경보부터 30분 동안 추가 기록을 만들지 않는다. 재시작과 동일 메시지 재전송은 쿨다운을 초기화하지 않는다. 30분 뒤 새로운 유효 STATE도 위험 상태이면 경보를 추가할 수 있다. 수치가 낮아졌다 다시 위험해져도 쿨다운은 유지한다. 읽음 처리는 쿨다운을 해제하지 않는다.

경보는 기존 알림함 `/api/v1/notifications`에 `collection_required`, `severity: error`로 표시하고 기존 읽음 처리 API를 사용한다. `channel: LOG`, `status: RECORDED`는 DB 기록을 뜻한다. SMTP·문자 등의 외부 발송 성공을 뜻하지 않는다. 외부 발송과 v2 자동 카메라 녹화 연결은 별도 후속 작업이다. 미디어 서비스가 설정되어 있어도 이 작업자는 영상 요청을 생성하지 않는다.

스냅샷과 SSE의 `operations`는 비활성화 시 null이다. 활성화 시 `lastKnownState`, `lastEvaluatedAt`, `lastAlertAt`, `notificationId`를 제공한다. 현재 STATE의 평가가 끝나고 신선할 때만 `effectiveState`에 NORMAL/WARNING/CRITICAL을 표시한다. 처리 대기나 데이터 지연·연결 불명은 UNKNOWN, 판정 불가는 ERROR다. 마지막 정상 판정과 마지막 알림은 근거 확인을 위해 유지한다. 원본 적재율과 경보 평가 상태는 별개이므로 화면에서 둘을 구분한다.

현재 배포는 Uvicorn 단일 worker다. 프로세스 내 잠금과 DB 트랜잭션으로 처리하며 작업자 종료 시 태스크를 취소·회수한다. 다중 worker·복제 배포에는 공유 세션뿐 아니라 DB 기반 분산 작업 선점이 추가로 필요하다. 피트에 다른 장치를 다시 연결하면 기존 피트의 위험 상태와 쿨다운을 보존하고 새 장치의 신선한 STATE부터 평가한다.

## 이력 API

모든 API는 기존 브라우저 세션 쿠키가 필요하다. 조회 사용자와 관리자가 이용할 수 있다. `pit_id`는 명시적인 v2 연결 설정을 사용하며 다른 현장·장비 데이터를 섞지 않는다. 응답은 캐시하지 않는다.

| 경로 | 응답 |
| --- | --- |
| `GET /api/v2/monitoring/alerts?pit_id=pit-demo` | v2 원본 판정과 연결된 경보 목록 |
| `GET /api/v2/monitoring/collection-events?pit_id=pit-demo` | 수거 사건별 가장 높은 revision의 판정 |
| `GET /api/v2/monitoring/collection-events/{assessment_id}/revisions?pit_id=pit-demo` | 해당 사건의 수신된 개정 이력 |

`limit`은 기본 20, 최대 100이고 `offset`은 기본 0, 최대 10000이다. 응답은 `items`, `totalItems`, `limit`, `offset`이다. 경보 목록은 경보 ID 내림차순으로 정렬하며 `eventId`, `notificationId`, `assessmentId`, `loadPercent`, 관측·경보·읽음 시각을 제공한다. 경보의 원본 현장·장비 연결로 필터하므로 기존 v1 경보는 이 목록에 포함하지 않는다.

수거 사건은 저장된 EVENT 판정을 그대로 반환한다. 목록은 각 사건의 최고 revision 행의 저장 ID 내림차순, 개정 이력은 revision 내림차순이다. 높은 revision이 먼저 도착해도 이후 낮은 revision으로 되돌리지 않는다. 동일 사건·revision이 다른 메시지 ID로 다시 수신돼도 이력은 하나이며 최초 원본 메시지 ID를 유지한다. 최신 판정이 더 이상 COLLECTION이 아니면 이전 COLLECTION을 현재 사건으로 되살리지 않는다.

SUSPECTED는 후보, CONFIRMED는 판단 조건 충족, RESOLVED는 상황 종료, RETRACTED는 철회다. `capabilities.collection`, `decision_state`, `reason_codes`와 근거 참조도 함께 확인한다. 미구현·미검증 상태를 실제 수거 완료로 표시하지 않는다. 특히 CONFIRMED나 집게 탐지가 기존 수거 완료 확인을 자동 대체하지 않는다. 규격에는 작업자·잔여 높이가 없으므로 이를 임의로 만들어 `CollectionLog`에 저장하지 않으며 히스테리시스를 사건만으로 초기화하지 않는다.

## 검증

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest -v
docker build -t scrap-backend-issue31:local .
```

합성 데이터로 임계치 복귀, 30분 쿨다운, 재시작·중복·장애 복구, 오래된 관측, 사건 개정·철회, 장치 격리와 API 인증을 검증한다. 실제 장비 송신·프론트엔드 표시·외부 알림 전달 검증은 별도로 진행한다.

# 라이다 v2 대시보드 API

표본 타입과 오류 응답 모델은 [대시보드 응답 명세](DASHBOARD_RESPONSE_CONTRACT.md)를 참고한다.

30일·90일 적재율 조회와 집계 간격은 [장기간 이력 조회](LONG_LOAD_HISTORY.md)를 참고한다.

[최신 관측 모드](COMPACT_LIDAR_STORAGE.md)를 켜면 표시 적재율에 중앙값을 적용하고 준비/오류는 null로 표시한다. 최신 프로필의 보정 상태와 상태 사건 조회 API도 제공한다.

v2 수신 데이터의 현재 판정과 장비 상태를 읽는 API다. 프론트엔드에서 아래 API를 호출하고 새로운 응답의 null·상태 값을 표시하도록 연결해야 한다. 기존 `/api/v1/monitoring/*`와 `/api/v1/stream/*`의 경로·응답·데이터 선택은 유지한다. v2 수신 데이터를 기존 `ScrapMetric`으로 변환하지 않는다.

## 설정

비공개 `.env`의 `EDGE_V2_DEVICES`로 장치를 등록한 뒤 피트 연결을 추가한다. 아래는 합성 식별자 예시이며 실제 토큰은 공개 문서에 기록하지 않는다.

```dotenv
EDGE_V2_MONITORING_SOURCES='[{"pit_id":"pit-demo","site_id":"site-demo","edge_id":"edge-demo"}]'
EDGE_V2_ASSESSMENT_TTL_SECONDS=30
```

각 피트에는 장치 하나만 연결하고, 같은 장치를 여러 피트에 연결하지 않는다. 등록되지 않은 장치나 중복 연결은 설정 검증에서 거부한다. 토큰 교체용으로 같은 장치가 여러 인증 항목에 있어도 연결 항목은 하나다. 기본값 `[]`에서는 조회할 피트가 없다. 피트의 임계치가 DB에 있으면 해당 설정을, 없으면 기존 기본 임계치를 반환한다.

## 조회

| 메서드 | 경로 | 내용 |
| --- | --- | --- |
| GET | `/api/v2/monitoring/snapshot?pit_id=pit-demo` | 현재 판정·적재율·장비 상태 |
| GET | `/api/v2/monitoring/events?pit_id=pit-demo` | 동일한 스냅샷의 SSE |
| GET | `/api/v2/monitoring/profiles?pit_id=pit-demo` | 별도 높이 곡선과 단면 기준 비율 |

최근 24시간 적재율 그래프용 `/api/v2/monitoring/load-history`는 [V2_LOAD_HISTORY.md](V2_LOAD_HISTORY.md)를 참고한다. 과거 STATE 판정과 compact 분 이력을 조회하며 현재 장비 상태로 과거 값을 지우지 않는다. 화면별 요청과 연결 주의사항은 [DASHBOARD_API_GUIDE.md](DASHBOARD_API_GUIDE.md)를 따른다.

조회 API는 `/api/v1/session`으로 발급한 `scrap_session` 쿠키가 필요하다. 관리자와 조회 사용자 모두 이용할 수 있다. 장치 Bearer 토큰으로 브라우저 세션을 대체하지 않는다. 세션이 없거나 만료되면 `401`, 연결 설정이 없는 피트는 `404 MONITORING_SOURCE_NOT_CONFIGURED`다. 응답은 캐시하지 않는다. 전체 스키마는 `/docs`에서 확인한다.

핵심 응답 필드:

| 필드 | 의미 |
| --- | --- |
| `schemaVersion` | `"2"`. v1 화면 모델과 별도로 처리 |
| `snapshotId` | `serverTime`을 제외한 내용의 해시. 변경 감지용이며 순번이 아님 |
| `pitId`, `source` | 명시적으로 연결한 피트·현장·장비 |
| `measuredAt` | 기본 모드는 STATE의 `observed_to`, 높이 모드는 계산 관측 시각. 없으면 null |
| `assessmentReceivedAt` | 선택한 STATE의 서버 수신 시각. 신선도 기준으로 사용하지 않음 |
| `dataState`, `reasonCodes` | 화면 표시 가능 여부와 이유 |
| `summary.loadPercent` | 기본 모드는 검증 STATE의 비율(시간 필터 활성 시 안정화한 값), 높이 모드는 안정화된 높이의 단면 비율. 불가는 null |
| `summary.rawLoadPercent`, `summary.filterState` | 시간 필터 전 비율과 필터 준비 상태 |
| `summary.inputSource`, `fillObservation` | EDGE_STATE 또는 HEIGHT_SECTIONS와 별도 높이 계산 관측. 원본 엣지 판정과 구분 |
| `summary.warningThresholdPercent`, `summary.collectionThresholdPercent`, `summary.recoveryThresholdPercent` | 경고·위험 진입·위험 복귀 임계치 |
| `assessment` | 선택한 STATE의 원본 판정·기능 상태·근거 참조. 없으면 null |
| `deviceStatus` | 기존 v2 장비 상태 조회와 동일한 연결 상태·원본 snapshot |
| `operations` | 선택적으로 활성화한 경보 처리 상태. `evaluationSource`로 최신 관측 평가/짧은 대기의 최근 완료 판정을 구분한다. 실제 평가 시각은 `lastEvaluatedAt`을 사용한다. 자세한 설정은 [V2_OPERATIONS.md](V2_OPERATIONS.md) 참고 |

`assessment`에는 점 배열이나 영상이 없다. 명시적 높이 모드를 켜지 않은 기본 설정에서는 보정 좌표를 높이로 간주하거나 점으로 적재율을 계산하지 않는다. 임의의 `fill_state` 문자열을 기존 NORMAL/WARNING/CRITICAL로 추정하지 않고 그대로 제공한다. 유효한 0%는 `0`, 판정 불가는 `null`이므로 프론트엔드는 둘을 구분해야 한다.

별도 `/profiles`는 명시적으로 등록한 검증 기준이 있을 때 [높이 프로필과 단면 기준 계산](LIDAR_HEIGHT_PROFILES.md)을 제공한다. 기본 설정에서 이 계산은 snapshot의 `assessment`나 `summary.loadPercent`를 바꾸지 않는다. `EDGE_V2_HEIGHT_FILL_ENABLED=true`에서는 [높이 시간 중앙값](HEIGHT_TEMPORAL_MEDIAN.md)을 거친 단면 비율을 summary에 사용하고, 원본 assessment와 capabilities는 유지한다. 이 모드는 전체 체적 적재율과 구분한다.

## 최신 판정과 신선도

아래 STATE 선택 규칙은 기본 EDGE_STATE 모드다. HEIGHT_SECTIONS 모드의 관측 선택·설정 변경·창 무효화는 [HEIGHT_FILL_PIPELINE.md](HEIGHT_FILL_PIPELINE.md)를 따른다. 두 모드 모두 FRESH 이외의 숫자를 현재 값으로 표시하지 않는다.

- 현장·장비가 일치하는 `assessment_type: STATE`만 선택한다. `generated_at`이 가장 큰 STATE가 현재 판정이다. 시각이 같으면 먼저 저장된 행을 선택해 재시도로 화면이 바뀌지 않게 한다. EVENT 및 사건의 높은 revision은 현재 STATE를 대체하지 않는다.
- 과거 `generated_at`의 지연 메시지나 중복 재전송은 현재 값을 갱신하지 않는다. STATE에는 부팅 인스턴스·sequence가 없으므로 장치 시각 동기화가 필요하다. 미래 시각이 있는 최신 판정을 받으면 이전 값을 현재 값으로 대신 표시하지 않고 UNKNOWN으로 보관한다.
- 표시 신선도는 `observed_to` 기준이다. 없으면 상태 판정의 나이를 확인할 때만 `generated_at`을 사용하며, 관측 시각 없는 숫자 적재율은 표시하지 않는다. 신선도 한도에 도달하면 STALE이다. 서버 도착 시각으로 과거 관측을 새 관측으로 만들지 않는다.
- 미래 관측 시각 또는 서버보다 5초 넘게 앞선 생성 시각은 UNKNOWN이다. 마지막 장비 상태가 서버에 새로 수신된 지 30초가 되면 연결 상태는 UNKNOWN이다. 동일 status 재전송은 수신 시각을 갱신하지 않는다.
- 장비가 `MEASUREMENT_UNAVAILABLE`, `CONFIG_ERROR`, `UNKNOWN`이면 표시 적재율은 null이다. 현재 장비 상태와 판정의 설정 버전이 다르면 UNKNOWN이다. `HEALTHY`·`DEGRADED`에서는 검증된 판정 값이 있으면 표시할 수 있으며 원본 센서 상태와 reason을 함께 제공한다.
- STATE가 없으면 NO_DATA, 오래됐으면 STALE, 장비 연결·시각·설정 일치 여부를 확인할 수 없으면 UNKNOWN, 기능 미검증 또는 판정 불가는 UNAVAILABLE이다. 표시 가능한 값은 FRESH다. 여러 문제가 겹치면 시각 이상 → 관측 신선도 → 장비 연결 → 장비 판정 → 설정 버전 → 적재 판단 순서로 `dataState`를 정한다. 상세 장비 상태는 별도로 유지한다.

FRESH 이외의 상태에서는 `summary.loadPercent`가 null이다. 과거 `assessment.fill_estimate`는 근거 확인을 위해 남아 있으므로 현재 수치 게이지에 직접 사용하지 않는다. 알림 기록과 히스테리시스는 별도로 활성화하는 작업자가 담당하며 이 조회 기능에서 실행하지 않는다. 수거 완료 확정과 카메라 녹화 요청도 이 조회 기능에서 수행하지 않는다. 미검증 표면 높이·카메라 상태를 임의로 추정하지 않으며 관련 조회 계약으로 구분한다.

## SSE 연결

```javascript
const stream = new EventSource(
  "/api/v2/monitoring/events?pit_id=pit-demo",
  { withCredentials: true }
);
stream.addEventListener("monitoring.snapshot", (event) => {
  const snapshot = JSON.parse(event.data);
  // summary.loadPercent === null이면 수치 대신 판정 불가 상태를 표시한다.
  render(snapshot);
});
stream.addEventListener("session.expired", () => {
  stream.close();
  // 다시 로그인한 뒤 새 연결을 만든다.
});
```

연결 직후 최신 스냅샷을 전송하고, 약 1초 간격으로 커밋된 STATE·status 메타데이터를 확인한다. 내용이 바뀌면 `event: monitoring.snapshot`, 같으면 keepalive 주석을 보낸다. 메시지가 새로 오지 않아도 시간이 지나 STALE·UNKNOWN이 되면 전송한다. 매 확인 때 세션을 재검사하며 로그아웃·만료 시 `session.expired`를 보낸 후 닫는다.

연결마다 짧은 조회를 수행하고 DB 읽기 트랜잭션을 프레임 사이에 종료한다. 점 배열 조회나 장기 트랜잭션, 무한 메시지 큐는 사용하지 않는다. 느린 클라이언트에는 중간 상태를 쌓지 않으며 다음 조회에서 최신 상태를 전달한다. SSE는 사건 이력 전달 기능이 아니므로 `Last-Event-ID` 재생을 제공하지 않는다. 재접속하면 DB에서 현재 상태를 다시 읽는다. 브라우저 화면 종료 시 `stream.close()`를 호출한다.

로그인 세션은 DB에 저장되어 재시작 후에도 유지되며 같은 DB를 읽는 프로세스에서 확인할 수 있다. 다만 미디어·경보 작업자의 분산 작업 선점은 아직 없으므로 현재 배포의 단일 worker 전제는 유지한다. 접속 수에 비례해 조회량이 증가하므로 다수 클라이언트 도입 전 부하를 측정한다. 운영 TLS 프록시는 SSE 버퍼링을 끄고 연결 종료를 전달한다.

## 검증

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest -v
docker build -t scrap-backend-issue29:local .
```

실제 엣지 송신, 프론트엔드 표시, TLS 및 현장 영상과의 통합 검증은 별도로 진행한다.

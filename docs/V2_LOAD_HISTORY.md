# 라이다 v2 적재율 추이 API

[최신 관측 모드](COMPACT_LIDAR_STORAGE.md)에서 생성한 분 요약도 함께 조회한다. 기존 대표값 선택은 마지막 값이며 평균/최소/최대와 품질 수를 추가 제공한다. 원문 모드와 분 요약의 `valuePolicy`를 구분한다.

대시보드의 최근 24시간 대표 적재율 그래프를 위한 조회 API다. 기본 원문 모드는 저장된 v2 STATE 판정을 시간 구간별로 선택한다. compact 모드는 분 이력을 함께 사용하고, 선택적 높이 모드에서는 안정화된 높이에서 계산한 비율을 저장한다. 카메라 API와 독립적이며 기존 v1 추이 API와 SSE 응답은 유지한다.

## 요청

```http
GET /api/v2/monitoring/load-history?pit_id=pit-demo
```

기존 `/api/v1/session`으로 로그인한 브라우저 세션 쿠키가 필요하다. 관리자와 조회 사용자가 이용할 수 있으며 장치 Bearer 토큰으로 대체하지 않는다. 피트와 장치는 `EDGE_V2_MONITORING_SOURCES`로 연결한다. 없는 세션은 401, 연결하지 않은 피트는 404다. 응답은 `Cache-Control: no-store`다.

| 매개변수 | 의미 |
| --- | --- |
| `pit_id` | 기본 `pit-01`. 명시적으로 연결한 피트 |
| `from` | 시작 시각 포함. 시간대가 있는 ISO 8601 |
| `to` | 종료 시각 제외. 시간대가 있는 ISO 8601 |
| `bucket_seconds` | 선택값 60, 300, 900, 3600. 생략 시 자동 선택 |

`to` 생략 시 서버 현재 UTC 시각, `from` 생략 시 종료 시각의 24시간 전이다. 시간대 오프셋은 UTC로 변환하며 시간대 없는 시각은 거부한다. 조회 기간은 양수이며 최대 7일이다. 서버보다 5초 넘게 미래인 종료 시각은 거부한다. 날짜 검색에서 오늘이 포함되면 종료 시각을 현재 시각으로 제한한다.

구간은 UTC epoch 기준으로 정렬한다. 기본 24시간은 60초 단위이며 시작·종료의 부분 구간을 포함해 최대 1,441개다. 자동 선택은 가능한 가장 작은 구간을 사용하고, 명시한 구간으로 응답 개수를 초과하면 422다. 예를 들어 7일 조회는 자동으로 900초 단위를 선택한다. 실제 데이터가 없는 구간도 null 표본으로 반환한다.

## 응답

상위 필드는 `schemaVersion: "2"`, `pitId`, `source`, `from`, `to`, `bucketSeconds`, `selectionPolicy: "LATEST_STATE_PER_BUCKET"`, `samples`다. 각 표본은 다음 필드를 가진다.

| 필드 | 의미 |
| --- | --- |
| `bucketStartAt`, `bucketEndAt` | 요청 범위로 잘라낸 구간, 종료 미포함 |
| `generatedAt` | 선택한 원본/계산 관측의 생성 시각. 보관되지 않았으면 null |
| `observedAt` | 선택한 원본/계산 관측 시각. 보관되지 않았으면 null |
| `assessmentId` | 원본 엣지 판단 ID. 높이 관측이나 보관되지 않은 경우 null |
| `valuePercent` | 원문 모드는 ratio × 100, 분 이력은 안정화된 비율. 알 수 없으면 null |
| `valid` | 숫자 적재율 제공 여부. 실제 0%는 true |
| `dataState` | AVAILABLE / LIMITED / UNAVAILABLE / STALE / UNKNOWN / NO_DATA |
| `reasonCodes` | 원본 판정 이유와 이력 조회의 유효성 판단 이유 |

표본은 구간 시작 시각 오름차순이다. 프론트엔드는 `valid: false` 또는 `valuePercent: null`인 구간을 0으로 바꾸거나 이전 값으로 채우지 않고 그래프를 끊어서 표시한다. `LIMITED` 값은 수치를 제공하지만 입력 제한 상태도 표시한다.

## compact 분 이력과 선택적 높이 입력

`valuePolicy=RAW_STATE`는 원문 STATE, `MEDIAN_MINUTE`는 compact 분 이력이다. 분 이력에는 평균·최소·최대·유효/무효 표본 수와 `inputSource`, `methodVersion`, `observationId`도 제공한다. 현재 모드의 대표값은 구간의 마지막 값이며 평균으로 바꾸지 않는다.

부분 분 범위에서 원래 정밀 시각·ID를 보존하지 않았다면 `generatedAt`, `observedAt`, `assessmentId`는 null이고 `COMPACT_SUBMINUTE_METADATA_NOT_RETAINED`를 표시한다. 숫자 슬롯이 유효하면 `valid=true`와 숫자는 유지하며 bucket 위치로 표시할 수 있다. 이 조건을 원문 STATE의 시각 누락까지 확장하거나 원래 시각을 만들어내지 않는다.

높이 모드에서는 `inputSource=HEIGHT_SECTIONS`, 별도 `observationId`를 사용하고 assessmentId는 null이다. 서로 다른 유효 출처/방법의 숫자가 같은 bucket에 섞이면 `MIXED`/null과 `FILL_METHOD_CHANGED_WITHIN_BUCKET`을 반환한다. [높이 판정 연결](HEIGHT_FILL_PIPELINE.md)과 [대시보드 연결 안내](DASHBOARD_API_GUIDE.md)를 참고한다.

## 선택과 유효성 기준

아래는 기본 원문 STATE 선택 규칙이다. compact와 높이 입력의 처리 차이는 위 절을 따른다.

- 그래프의 시간 축은 **판정 생성 시각 `generated_at`**이다. 관측 시각은 `observedAt`으로 별도 제공한다. 서버 수신 시각은 시간 축이나 신선도 계산에 사용하지 않는다.
- 연결된 현장·장비의 STATE만 조회한다. EVENT·수거 개정·점 배열·현재 status는 이 조회에 사용하지 않는다.
- 각 구간에서 `generated_at`이 가장 큰 STATE를 선택한다. 같은 시각이면 먼저 저장된 행을 선택한다. 중복 재전송이나 같은 판정의 새 메시지 ID가 그래프를 늘리거나 평균값을 바꾸지 않는다.
- 평균·최대값이 아니라 **마지막 보고 판정**이다. 최신 보고가 판정 불가라면 이전의 유효한 수치로 대신하지 않는다.
- 생성 당시 `generated_at - observed_to`가 현재 설정의 `EDGE_V2_ASSESSMENT_TTL_SECONDS` 이상이면 STALE이다. 생성 시각보다 미래인 관측, 서버보다 5초 넘게 미래인 생성은 UNKNOWN이다. 관측 시각 없는 숫자 판정도 UNKNOWN이다.
- 기능 미검증, fill_estimate 누락, UNAVAILABLE 판정은 null이다. 유효한 LIMITED 수치는 제한 상태와 함께 제공한다. 저장 당시 수신 스키마로 검증된 ratio만 사용하며 좌표·거리로 적재율을 계산하지 않는다.
- 과거의 유효한 판정은 지금 오래됐거나 현재 장비가 오프라인이라는 이유만으로 null로 바꾸지 않는다. 이 API는 **과거 STATE 보고 이력**이며 당시의 status·통신 가용성을 재구성하는 API는 아니다.

각 표본은 구간 마지막 보고를 대표하며, 그 구간 내내 영상·센서·통신이 정상이라는 의미는 아니다. 구간보다 짧은 장애나 중간 판정 변경을 모두 보여주지는 않는다. NO_DATA는 해당 해상도의 구간에 STATE가 없다는 뜻이며 센서 고장을 확정하지 않는다. 임계치 변경 이력을 저장하지 않으므로 현재 임계치를 과거 임계치로 만들어 반환하지 않는다. 화면 참고용 현재 임계치는 기존 v2 snapshot에서 조회한다.

## 조회 비용과 화면 연결

기존 `edge_v2_messages`의 현장·장비·메시지 종류·생성 시각 색인을 사용해 범위를 제한한다. SQL의 `row_number`로 대표 행을 먼저 고른 뒤 Python으로 최대 1,441개 STATE 본문만 가져온다. 조회에서 새 DB 테이블이나 경보·녹화 작업을 만들지 않는다. DB가 확인하는 원본 행 수는 기간의 보고량에 비례하므로 장기 대량 데이터에는 추후 사전 집계가 필요할 수 있다.

프론트엔드는 화면 진입·기간 변경 시 조회하고, 약 1분마다 최신 구간을 갱신하는 방향으로 연결한다. 현재값 SSE가 올 때마다 24시간 전체를 다시 조회하지 않는다. 이 PR은 백엔드 조회 기능이며 실제 그래프 표시는 프론트엔드 연결이 필요하다.

## 검증

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest -v
docker build -t scrap-backend-issue34:local .
```

합성 데이터로 기간·구간 제한, UTC 및 마이크로초 경계, null과 0%, 최신 판정 불가, 과거·중복·EVENT 제외, 현장·장비 격리, SQL 대표값 선택과 v1 호환성을 검증한다.

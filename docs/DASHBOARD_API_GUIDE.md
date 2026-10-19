# 대시보드 화면별 백엔드 API 연결 안내

실행 중인 화면을 기준으로 API 연결을 확인할 때 사용하는 백엔드 안내서다. 대시보드의 계산·표시 구현을 복사하지 않고 요청 경로, 응답 의미와 확인 순서를 정리한다. 이 문서의 예시 식별자를 실제 장비 설정으로 그대로 사용하지 않는다.

## 화면별 요청

아래 경로는 대시보드와 같은 origin의 게이트웨이를 통해 호출한다. 실제 backend/media 내부 주소나 장치 Bearer token을 브라우저 코드에 넣지 않는다.

| 화면 | 요청 | 확인할 내용 |
| --- | --- | --- |
| 로그인 | `POST /api/v1/session`, `GET /api/v1/session` | 사용자·역할·세션 쿠키. 로그아웃은 DELETE |
| 현재 상태 | `GET /api/v2/monitoring/snapshot?pit_id=...` | `dataState`, `summary.loadPercent`, 장비 상태와 선택적 판정 출처 |
| 실시간 갱신 | `GET /api/v2/monitoring/events?pit_id=...` | SSE `monitoring.snapshot`, `session.expired`, 끊김·재연결 |
| 센서별 높이 | `GET /api/v2/monitoring/profiles?pit_id=...` | 기존 높이 필드·cm 단위·null·처리 방법 |
| 현재/과거 추이 | `GET /api/v2/monitoring/load-history?pit_id=...&from=...&to=...` | 숫자와 결측, 원문/분 이력 정책, 부분 구간 메타데이터 |
| 경보 상세 | `GET /api/v2/monitoring/alerts?pit_id=...` | 경보 ID·숫자·시각·출처. 실제 외부 발송 여부와 구분 |
| 수거 사건 | `GET /api/v2/monitoring/collection-events?pit_id=...` | 사건·개정·철회 이력. 수거 완료 확인으로 자동 해석하지 않음 |
| 공통 알림 | `GET /api/v1/notifications` | 사용자별 읽음과 미확인 개수 |
| 기존 사건 목록 | `GET /api/v1/events` | 기존 이력의 사건 목록·필터·페이지 |
| 날짜 녹화 | `GET /api/v2/recordings/daily`, `GET /api/v2/recordings/daily/{id}` | 날짜 목록·확보/누락 구간·재생과 export 상태 |
| 영상 재생 | `/media/cameras/{id}/live`, `/media/daily-recordings/{id}/hls/index.m3u8` | 게이트웨이가 영상 바이트와 인증을 처리. FastAPI로 영상 본체를 보내지 않음 |
| 관리자 | `GET /api/v1/settings/alerts?pit_id=...`, `GET /api/v1/notification-recipients` | administrator 권한. 변경은 CSRF, 알림 설정 변경은 If-Match 추가 |

현재 상태 응답의 `pitId`·`source`와 runtime 설정의 `pitId`를 맞춘다. 다른 예시 pit_id를 넣어 받은 404를 API 자체의 미구현으로 판단하지 않는다. v1과 v2의 응답 모델을 섞지 않는다.

## 높이와 중앙값: 계산은 백엔드, 화면은 기존 필드 표시

[높이 시간 중앙값](HEIGHT_TEMPORAL_MEDIAN.md)을 활성화하면 백엔드가 구간별 높이(cm)를 안정화해 기존 필드로 전달한다. 프론트엔드는 중앙값을 다시 계산하지 않는다.

| v2 응답 필드 | 화면에서 사용할 의미 |
| --- | --- |
| `profiles[].sensorId` | 각 센서의 식별자. 배열 순서만으로 센서를 바꾸지 않음 |
| `profiles[].unit` | cm. 화면이 m를 사용하면 한 번만 100으로 나눔 |
| `profiles[].samples[].positionRatio` | 측정 구간의 상대 위치 0..1. 수신점 모드의 잘못된 점에서는 null |
| `profiles[].samples[].height` | 높이(cm). 공간 모드는 시간 필터 활성 시 구간 중앙값, SENSOR_ANGLE은 최신 수신점 높이 |
| `profiles[].samples[].valid` | false 또는 height null이면 선을 끊음. 0cm는 유효한 숫자 |
| `profiles[].average / minimum / maximum` | 반환된 유효 높이의 평균·최소·최대. SENSOR_ANGLE은 최신 스캔 통계이며 시간 창 준비 중에도 제공 |
| `profiles[].calibrationState / reasonCodes` | 검증·결측·준비 상태. 과거 값으로 대신 채우지 않음 |
| `profiles[].valuePolicy` | 일반 공간 모드는 SPATIAL_BIN_MEDIAN / SPATIAL_TEMPORAL_MEDIAN, 시뮬레이션 기본 각도 모드는 SENSOR_ANGLE(최신 수신점 높이) |
| `profiles[].coverageBasis` | SPATIAL_BINS 또는 RECEIVED_POINTS. RECEIVED_POINTS의 유효 비중은 표시 차단 조건이 아님 |
| `profiles[].summaryValuePolicy / filteredAverage` | 수신점 모드는 SCAN_MEAN_TEMPORAL_MEDIAN일 때 대표 스캔 평균의 시간 중앙값(cm)을 별도로 제공. 개별 samples.height는 최신 측정값 |
| `profiles[].samples[].angleMdeg / sourceIndex` | 각도 모드의 원래 측정 각도(1/1000도) / 원본 점 index. 수신한 점 수만큼 제공하며 20개로 고정하지 않음 |
| `profiles[].sectionFillPercent` | 해당 센서의 높이 비율(%). SENSOR_ANGLE 시간 필터 모드는 filteredAverage 기준. 전체 피트 부피 비율과 구분 |

v1 snapshot의 `lidarProfiles`는 별도 m 계약이다. v2에 cm 값을 넣는 것과 v1 단위를 바꾸는 것은 다른 작업이다.

화면이 이미 이 API와 높이 필드를 읽는다면 중앙값 때문에 표시 코드를 바꿀 필요는 없다. 하지만 `/profiles`를 호출하지 않거나 v2에서는 높이 카드를 고정적으로 미지원 처리한다면 백엔드 응답을 바꾸는 것만으로 높이 그래프가 나타나지 않는다. 이 경우 담당자에게 API 조회·매핑 연결을 전달한다. 원본 엣지 STATE나 capability를 조작해 연결된 것처럼 만들지 않는다.

## 현재 게이지와 출처

- 현재 게이지는 `dataState=FRESH`일 때의 `summary.loadPercent`를 사용한다. null을 0%로 바꾸지 않는다.
- `summary.inputSource=EDGE_STATE`면 기존 엣지 판정 입력이다. `HEIGHT_SECTIONS`면 백엔드가 높이 단면에서 계산한 비율이다. `summary.simulated=true`이면 합성 기준으로 계산한 시뮬레이션 값이다. 단면 기준이라는 설명을 표시한다.
- `assessment`와 `assessmentReceivedAt`은 원래 엣지 STATE를 유지한다. 높이 모드에서 숫자를 제공하더라도 엣지 `capabilities.fill`을 ENABLED로 바꾸지 않는다.
- 높이 모드의 `fillObservation`·`observationId`는 원본 엣지 assessment ID와 다르다. 이력/경보의 nullable assessmentId를 채우려고 가짜 엣지 ID를 만들지 않는다.
- 실제 임계치는 snapshot의 값을 사용한다. 문서의 기본 예시 값을 화면에 고정하지 않는다.

## 적재율 숫자의 반올림

화면에 사용하는 v2 적재율은 JSON 응답에서 소수점 셋째 자리에서 반올림해 최대 둘째 자리까지 숫자로 제공한다. 예: `57.740568 → 57.74`, `57.745 → 57.75`, `99.999 → 100.0`. 현재값/SSE의 `summary.loadPercent / rawLoadPercent`, 높이 단면의 `sectionFillPercent / meanSectionFillPercent`, 이력의 `valuePercent / averagePercent / minimumPercent / maximumPercent`, v2 경보의 `loadPercent`에 같은 규칙을 적용한다.

DB와 내부 계산·중앙값·임계치 판정은 반올림 전 정밀도를 유지한다. 원본 엣지 assessment, fillObservation의 계산 근거, 높이(cm), 설정 임계치는 이 표시용 반올림 대상이 아니다. 따라서 화면 숫자가 임계치와 같게 반올림되더라도 판정이 달라질 수 있으며, 상태는 서버가 제공한 판정 필드를 사용한다.

null은 null, 실제 0은 숫자 0으로 유지한다. JSON number는 `57.70`의 끝 0이나 `%` 문자·글자 크기를 보장하지 않는다. 항상 두 자릿수로 표시하거나 `%`의 크기를 줄이는 서식은 대시보드에서 적용한다.

## 분 이력과 부분 구간

[분 이력](V2_LOAD_HISTORY.md)은 원문 STATE 또는 compact 숫자 슬롯을 읽는다. `valuePolicy=RAW_STATE / MEDIAN_MINUTE`를 구분한다.

compact 조회가 분 중간에서 잘리면 숫자는 남아 있어도 원래 정밀 시각·ID는 보존되지 않았을 수 있다. 이때 `valid=true`, 숫자 값과 함께 `generatedAt / observedAt / assessmentId=null`, `COMPACT_SUBMINUTE_METADATA_NOT_RETAINED`를 제공한다. 해당 조건에서는 bucket 위치에 숫자를 표시하고 메타데이터 부재를 유지한다. 이 예외를 원문 모드까지 넓히지 않는다.

- 유효 숫자여도 시각이 null이라는 이유만으로 응답 전체를 거부하면 compact 부분 구간 그래프가 사라질 수 있다.
- 원래 시각을 만들어내거나 null인 판정 ID를 임의의 값으로 채우지 않는다.
- 서로 다른 유효 출처/계산 방법이 섞인 bucket은 `inputSource=MIXED`, 값 null과 `FILL_METHOD_CHANGED_WITHIN_BUCKET`이다. 서로 다른 계산 방법을 평균해 연결하지 않는다.
- 관측하지 않은 구간, 잘못된 표본과 유효 0%를 구분한다. 누락을 이전 값으로 채우지 않는다.

## 연결 점검 순서

1. 실제 배포 버전과 runtime 피트 연결을 확인한다. 작업 브랜치에 존재하는 API/필드가 서버에 적용됐다고 가정하지 않는다.
2. 로그인 → 현재 상태 → SSE → 추이 → 높이 → 영상/관리자 순서로 요청 상태와 화면을 함께 본다. 로그인 전 401과 화면 이동 시 취소된 요청을 장애로 합산하지 않는다.
3. 현재 서버 데이터 확인과 합성 fixture의 계약 검증을 구분한다. 현재 값이 null뿐이면 유효 숫자·부분 이력 계약까지 통과했다고 판단하지 않는다.
4. 새 기능은 로컬 합성 입력과 기존 테스트로 검증하고, 코드 검토·병합 이후 서버 적용을 별도로 확인한다. 실제 데이터나 설정을 화면 시험용으로 덮어쓰지 않는다.
5. 화면 연결이 빠져 있으면 해당 담당자에게 요청 경로·필드·단위·결측 정책을 전달한다. 백엔드 작업이 다른 저장소 수정 권한을 뜻하지 않는다.

날짜 녹화 504는 [미디어 연동](DAILY_RECORDINGS.md)의 timeout과 upstream 상태를 구분해 확인한다. 한 번의 timeout이나 재조회 성공만으로 항상 고장/항상 정상이라고 단정하지 않는다. 다른 담당 서비스 재시작이나 설정 변경은 자동으로 수행하지 않는다.

## 현황 화면의 수거 통계·활성 경보

[현황 화면 연결 안내](DASHBOARD_OVERVIEW.md)에서 항목별 API와 빈 값의 의미를 확인한다. `collection-summary`는 실제 완료 기록으로 최근 수거·평균 주기를 집계하고, `active-alerts`는 과거 알림함과 구분한 현재 적재율 경보를 제공한다.

## 상세 계약

높이 감소로 추정한 수거의 마지막 활동 시각은 [수거 감지 안내](HEIGHT_COLLECTION_DETECTION.md)의 `lastCollectionActivityAt`과 `activitySource`로 연결한다. 감지 설정은 m, 기존 높이 API는 cm다.

- [MONITORING_V2.md](MONITORING_V2.md): 현재값과 SSE
- [LIDAR_HEIGHT_PROFILES.md](LIDAR_HEIGHT_PROFILES.md), [HEIGHT_TEMPORAL_MEDIAN.md](HEIGHT_TEMPORAL_MEDIAN.md): 검증 높이와 cm 시간 중앙값
- [HEIGHT_FILL_PIPELINE.md](HEIGHT_FILL_PIPELINE.md), [V2_OPERATIONS.md](V2_OPERATIONS.md): 판정 출처·이력·경보
- [V2_LOAD_HISTORY.md](V2_LOAD_HISTORY.md): 구간 조회·부분 metadata
- [DAILY_RECORDINGS.md](DAILY_RECORDINGS.md): 날짜 녹화·HLS·MP4
- [AUTH_STORAGE.md](AUTH_STORAGE.md), [ADMIN_ACCESS.md](ADMIN_ACCESS.md): 로그인·권한·CSRF

## 시뮬레이션 높이 연결

[시뮬레이션 높이 API 안내](SIMULATION_HEIGHT_API.md)에 기존 세 API의 연결 필드와 합성 출처 표시, 개발 환경 활성화 조건을 정리했다. 높이는 기존 height 필드를 사용하며, 화면에서 중앙값을 다시 계산하지 않는다.

# 설정 항목과 실행 조건

실제 환경값과 secret은 배포 Repository가 외부에서 주입한다. 이 문서는 개발 Repository가 제공하는
설정 명세다. 공개 예시는 [`.env.example`](../.env.example), 이미지 게시 절차는 [RELEASING.md](RELEASING.md)를 따른다.

## 읽는 방법

`src/app/core/config.py`의 `Settings`가 앱 설정 정본이다. 프로세스 환경변수가 `.env`보다 우선하며
알 수 없는 키는 무시한다. 문자열·숫자·boolean은 환경변수 문자열로, 목록과 구조체는 JSON 문자열로 제공한다.
선택 문자열을 사용하지 않으면 해당 변수를 생략한다. 숫자·JSON 항목을 빈 문자열로 채우지 않는다.
시작 시 읽고 캐시하므로 변경한 설정을 적용하려면 앱을 다시 시작한다. DB에 저장된 계정·피트 임계치·알림 정책은
관련 환경변수의 기본값과 구분하며, 환경 파일 변경만으로 기존 DB 설정을 덮어쓰지 않는다.

아래 기본값은 `.env.example`을 적용하기 전 코드 기본값이다. ‘없음’은 `None`, `[]`는 빈 JSON 목록이다.
필수 열의 ‘선택’은 기본값으로 실행할 수 있다는 뜻이며 활성화한 기능의 조건부 요구사항까지 생략해도 된다는 뜻은 아니다.
형식 예시는 공개 합성 값이다. `<...>`는 배포 담당자가 비공개 값으로 바꿀 자리이며 그대로 실행할 값이 아니다.
Release의 `settings.schema.json`은 **환경변수 파싱 후 Settings 객체**의 형식·범위·중첩 필드를 설명한다.
`.env` 파일 문법 검사기가 아니며 교차 필드 조건은 앱 검증과 아래 활성화 조건을 함께 따른다.

## 앱 기본·계정·저장소

| 항목 | 의미 | 필수 | 코드 기본값 | 형식 예시 |
| --- | --- | --- | --- | --- |
| `APP_ENV` | 실행 환경 | 선택 | `development` | `production` (`development/production/test`) |
| `APP_NAME` | 앱 식별 설정. 현재 API 제목·이미지 이름을 바꾸지 않음 | 선택 | `scrap-monitoring-backend` | `scrap-monitoring-backend` |
| `HOST` | 실행기 참고용 호스트 설정. 기본 이미지 ENTRYPOINT에는 자동 적용되지 않음 | 선택 | `0.0.0.0` | `0.0.0.0` |
| `PORT` | 실행기 참고용 포트 설정. 기본 이미지 ENTRYPOINT에는 자동 적용되지 않음 | 선택 | `8000` | `8000` |
| `LOG_LEVEL` | 앱 로그 수준 | 선택 | `INFO` | `INFO` |
| `DEBUG` | DB 쿼리 로그 등 개발 진단 | 선택 | `false` | `false` |
| `ADMIN_PASSWORD` | 빈 계정 테이블에 최초 관리자 생성 | 최초 새 설치 시 필수 | 없음 | `<12~1024자 비공개 초기 비밀번호>` |
| `OPERATOR_PASSWORD` | 빈 계정 테이블에 조회 계정도 생성 | 선택 | 없음 | `<별도의 12~1024자 비공개 비밀번호>` |
| `CORS_ORIGINS` | 허용할 브라우저 origin, 쉼표 구분. 빈 값은 `*`로 정규화 | 선택 | `http://localhost:3000,http://127.0.0.1:3000` | `https://dashboard.example.test` |
| `DATABASE_URL` | SQLAlchemy 비동기 DB 접속 URL | production에 PostgreSQL 필요 | `sqlite+aiosqlite:///./test_scrap_monitoring.db` | `postgresql+asyncpg://<user>:<url-encoded-password>@<db-host>:5432/<database>` |
| `EDGE_API_KEY` | 기존 v1 장치 인증 키 | production에 비기본 키 필수 | `dev-edge-secret-key-12345` (개발 전용) | `<비공개 장치 키>` |
| `DEFAULT_WARNING_THRESHOLD` | 최초 피트 설정의 경고 기본값(%) | 선택 | `75.0` | `75.0` (0~100) |
| `DEFAULT_CRITICAL_THRESHOLD` | 최초 피트 설정의 수거 필요 기본값(%) | 선택 | `85.0` | `85.0` (0~100) |
| `ALERT_COOLDOWN_SECONDS` | 기존 알림 엔진의 반복 억제 시간(초) | 선택 | `1800` | `1800` (0 이상) |

기존 계정이 있으면 `ADMIN_PASSWORD/OPERATOR_PASSWORD` 변경으로 비밀번호를 재설정하지 않는다.
[계정 관리](AUTH_STORAGE.md)를 사용한다. 이미지 기본 명령은 `0.0.0.0:8000`에 바인딩한다.
HOST/PORT만 변경하여 컨테이너 리스너가 바뀐다고 가정하지 않는다. 명령·포트 매핑은 배포 구성에서 맞춘다.

## 엣지 v2·판정·높이

| 항목 | 의미 | 필수 | 코드 기본값 | 형식 예시 |
| --- | --- | --- | --- | --- |
| `EDGE_V2_DEVICES` | 장치별 식별자·두 센서 ID·비공개 토큰 목록 | v2 수신 시 필요 | `[]` | 아래 JSON A |
| `EDGE_V2_MONITORING_SOURCES` | 피트와 장치의 1:1 화면 연결 | v2 조회 시 필요 | `[]` | 아래 JSON B |
| `EDGE_V2_ASSESSMENT_TTL_SECONDS` | 현재 관측으로 볼 최대 나이(초) | 선택 | `30` | `30` (1~3600) |
| `EDGE_V2_OPERATIONS_ENABLED` | v2 운영 판정 작업자 | 선택 | `false` | `true` |
| `EDGE_V2_CRITICAL_THRESHOLD` | 피트 DB 설정이 없을 때 v2 위험 기준(%) | 선택 | `90.0` | `90.0` (5 초과~100) |
| `EDGE_V2_COMPACT_STORAGE_ENABLED` | 최신 투영·분 요약 저장 사용 | 높이·필터 기능에 필요 | `false` | `true` |
| `EDGE_V2_RAW_RETENTION_ENABLED` | 오래된 스캔 원문 자동 정리 | 활성 시 compact 필요 | `false` | `true` |
| `EDGE_V2_RAW_RETENTION_DAYS` | 스캔 원문 보관 기간(일) | 선택 | `30` | `30`, `14`, `7` (1~3650) |
| `EDGE_V2_RAW_RETENTION_BATCH_SIZE` | 테이블별 한 배치 최대 대상 수 | 선택 | `100` | `100` (1~1000) |
| `EDGE_V2_RAW_RETENTION_INTERVAL_SECONDS` | 한 순회 완료 후 다음 순회 대기(초) | 선택 | `3600` | `3600` (60~86400) |
| `EDGE_V2_RECEIVER_TIMING_ENABLED` | 수신 처리 구간의 임시 진단 로그 | 선택 | `false` | `false` |
| `EDGE_V2_RECEIVER_TIMING_MIN_MS` | 진단 로그의 최소 처리 시간(ms) | 선택 | `0` | `100` (0~60000) |
| `EDGE_V2_MEDIAN_WINDOW_SECONDS` | 시간 중앙값 창 길이(초) | 선택 | `5` | `5` (3~31 홀수) |
| `EDGE_V2_CRITICAL_CONFIRM_SECONDS` | 수거 필요 지속 확인(초) | 선택 | `3` | `3` (0~60) |
| `EDGE_V2_EMPTY_FLOOR_DISTANCE_CM` | 검증 전 센서~빈 바닥 참고 거리(cm). 피트 깊이와 별개 | 선택 | `1000.0` | `1000.0` (0 초과~100000) |
| `EDGE_V2_HEIGHT_REFERENCES` | 실측 검증 좌표·바닥·구간 기준 목록 | 해당 높이 모드에 필요 | `[]` | [실측 구조와 단위](LIDAR_HEIGHT_PROFILES.md), schema의 `HeightReference` |
| `EDGE_V2_SIMULATION_HEIGHT_REFERENCES` | 시뮬레이션 전용 좌표 기준 목록 | 시뮬레이션 높이 모드에 필요 | `[]` | [합성 구조](SIMULATION_HEIGHT_API.md), schema의 `SimulationHeightReference` |
| `EDGE_V2_HEIGHT_FILL_ENABLED` | 높이 단면 기반 적재율을 판정 입력으로 사용 | 선택 | `false` | `true` |
| `EDGE_V2_HEIGHT_MEDIAN_ENABLED` | 높이 시간 필터 활성화 | 선택 | `false` | `true` |
| `EDGE_V2_FILTERED_PROFILES_ENABLED` | 엣지가 제공한 필터 점의 높이 프로필 표시 | 선택 | `false` | `true` |
| `EDGE_V2_COLLECTION_DETECTION_ENABLED` | 추정 수거 활동 감지 | 선택 | `false` | `false` |
| `EDGE_V2_COLLECTION_DROP_METERS` | 수거 감지의 검증된 최소 감소량(m) | 수거 감지 활성 시 필수 | 없음 | `<검증한 양수>` (0 초과~1000) |
| `EDGE_V2_COLLECTION_STABLE_BAND_METERS` | 안정 구간의 폭(m) | 선택 | `0.2` | `0.2` (0 초과~100, 감소량보다 작음) |
| `EDGE_V2_COLLECTION_BASELINE_SECONDS` | 감소 전 기준 관측 시간(초) | 선택 | `10` | `10` (1~300) |
| `EDGE_V2_COLLECTION_CONFIRM_SECONDS` | 감소 후 안정 확인 시간(초) | 선택 | `10` | `10` (1~300) |
| `EDGE_V2_COLLECTION_SETTLE_SECONDS` | 현장 수거 활동 종료 대기(초) | 선택 | `300` | `300` (1~1800) |
| `EDGE_V2_COLLECTION_SIMULATION_SETTLE_SECONDS` | 명시적 시뮬레이션의 종료 대기(초) | 선택 | `20` | `20` (1~1800) |
| `EDGE_V2_COLLECTION_HEIGHT_BASIS` | 수거 감지 높이 기준 | 선택 | `UPPER` | `UPPER` 또는 `REPRESENTATIVE` |

JSON A (토큰 자리는 실행 전에 비공개 값으로 교체):

```dotenv
EDGE_V2_DEVICES=[{"site_id":"site-example","edge_id":"edge-example","sensor_ids":["sensor-a","sensor-b"],"token":"<private-token>"}]
EDGE_V2_MONITORING_SOURCES=[{"pit_id":"pit-example","site_id":"site-example","edge_id":"edge-example"}]
```

두 번째 줄이 JSON B다. 토큰의 허용 문자와 회전 시 센서 일치는 [v2 인증](EDGE_V2_INTEGRATION.md)을 따른다.
위 `<private-token>`은 형식 자리표시이며 실제 허용 토큰 문자가 아니다. 높이 기준은 배포 담당자가
검증한 자료를 등록한다. 운영 자료로 오인할 합성 치수·보정 리비전을 기본값으로 제공하지 않는다.
중첩 JSON의 필드·기본값·허용 형식은 Release schema와 링크 문서의 예시를 함께 사용한다.

높이 기준의 최소 형식 예시는 아래와 같다. 두 배열은 **서로 다른 입력 모드의 예시**이며
같은 피트에 함께 등록하지 않는다. 좌표·ID는 형식을 설명하는 공개 합성 값이며 현장 보정 자료가 아니다.
실측 확인 전 `verified`를 true로 변경하지 않는다.

`EDGE_V2_HEIGHT_REFERENCES`:

```json
[{"pit_id":"pit-example","config_revision":"config-example","coordinate_frame_id":"frame-example",
  "calibration_versions":{"sensor-a":"calibration-example-a","sensor-b":"calibration-example-b"},
  "verified":false,"floor_z_cm":0,"x_min_cm":0,"x_max_cm":100,
  "y_min_cm":0,"y_max_cm":100,"profile_axis":"x"}]
```

`EDGE_V2_SIMULATION_HEIGHT_REFERENCES` (development/test 전용):

```json
[{"pit_id":"pit-example","config_revision":"config-example","coordinate_frame_id":"frame-example",
  "environment_id":"simulation-example","simulator_revision":"0000000000000000000000000000000000000000",
  "floor_z_cm":0,"top_z_cm":100,"boundary_xy_cm":[[0,0],[100,0],[100,100],[0,100]],
  "sensor_poses":{
    "sensor-a":{"origin_cm":[0,0,100],"direction0":[1,0,0],"direction90":[0,0,-1]},
    "sensor-b":{"origin_cm":[100,0,100],"direction0":[-1,0,0],"direction90":[0,0,-1]}
  }}]
```

활성화 조건:

- 장치와 피트 연결은 1:1이며 높이 기준은 연결된 피트·센서·좌표/보정 리비전과 일치해야 한다.
- 높이 기준·높이 필터는 compact 저장을 요구한다. `EDGE_V2_FILTERED_PROFILES_ENABLED`는 추가로
  `EDGE_WORLD`/`SENSOR_ANGLE` 시뮬레이션 기준을 요구한다. `APP_ENV=production`에는 시뮬레이션 기준을 등록할 수 없다.
- 수거 감지는 운영 판정·높이 적재율과 검증한 감소량 설정을 요구한다. 추정 감지는 사람의 수거 완료 확인과 구분한다.
- 계산 최신성1초, 계산 연속성1.5초, 화면 유지3초, 스캔 운영 경고3초 초과, 손실 정상1시간은
  위 TTL·필터 창·위험 지속 시간과 목적이 다르다. 이 문서를 이유로 기준을 일괄 변경하지 않는다.

## 미디어·영상 연결

| 항목 | 의미 | 필수 | 코드 기본값 | 형식 예시 |
| --- | --- | --- | --- | --- |
| `MEDIA_SERVER_WHEP_URL` | 선택적 v1 WHEP 상위 주소 | 해당 모드 사용 시 필요 | 없음 | `http://media.example.test:8889` |
| `WHEP_SESSION_TTL_SECONDS` | WHEP 세션 보관 시간(초) | 선택 | `1800` | `1800` |
| `WHEP_MOCK_FALLBACK` | WHEP 연결 실패 시 mock SDP 허용 | 선택 | `true` | `true` |
| `MEDIA_SERVICE_URL` | 미디어 v1 JSON API 기본 주소 | v1 미디어 연동 시 필요 | 없음 | `http://media.example.test:8080/api/v1` |
| `MEDIA_API_TOKEN` | 미디어 v1 서비스 인증 토큰 | v1 주소와 함께 필요 | 없음 | `<비공개 Bearer 토큰>` |
| `MEDIA_DAILY_SERVICE_URL` | 독립 미디어 v2 JSON API 기본 주소 | 선택 | 없음 | `http://media.example.test:8080/api/v2` |
| `MEDIA_CAMERA_ID` | 기본 카메라 식별자 | 선택 | `camera-main` | `camera-example` (1~128자, 영숫자 시작) |
| `MEDIA_PIT_ID` | 카메라의 피트 식별자 | 선택 | `pit-01` | `pit-example` (1~50자) |
| `MEDIA_REQUEST_TIMEOUT_SECONDS` | 일반 미디어 JSON 호출 I/O 제한(초). 전용 메타데이터 예산은 별도 | 선택 | `3.0` | `3` (0 초과~30) |
| `MEDIA_POLL_INTERVAL_SECONDS` | 작업 상태 재조회 간격(초) | 선택 | `2.0` | `2` (1~60) |
| `MEDIA_V2_EVENT_CLIPS_ENABLED` | 새 v2 이벤트의 자동 클립 요청 | 선택 | `false` | `false` |
| `MEDIA_V2_EVENT_CLIP_MAX_DELAY_SECONDS` | 자동 클립 요청의 최대 이벤트 나이(초) | 선택 | `30` | `30` (1~300) |

v1 URL/토큰은 둘 다 설정하거나 생략한다. URL에는 사용자정보·query·fragment를 넣지 않고 지정한
`/api/v1` 또는 `/api/v2`로 끝나야 한다. v2 주소를 생략하면 기존 미디어 클라이언트의 v1 호스트 기반
v2 주소 선택을 따른다. 미디어 영상 바이트는 Backend가 직접 전달하지 않으며 브라우저 게이트웨이가 담당한다.
WHEP와 MJPEG/HLS 게이트웨이는 서로 다른 영상 방식이다. [미디어 계약](MEDIA_INTEGRATION.md)을 따른다.

## 이메일 알림

| 항목 | 의미 | 필수 | 코드 기본값 | 형식 예시 |
| --- | --- | --- | --- | --- |
| `GMAIL_EMAIL_ENABLED` | 실제 Gmail API 발송 활성화 | 선택 | `false` | `false` |
| `GMAIL_CREDENTIALS_FILE` | 비공개 OAuth authorized-user JSON의 컨테이너 경로 | Gmail 활성 시 필수 | 없음 | `/run/secrets/gmail-authorized-user.json` |
| `GMAIL_SENDER_EMAIL` | 승인한 발신 계정 주소 | Gmail 활성 시 필수 | 없음 | `sender@example.test` |
| `GMAIL_POLL_SECONDS` | 전달 작업 확인 간격(초) | 선택 | `5.0` | `5` (1~60) |
| `GMAIL_TIMEOUT_SECONDS` | 토큰/발신 HTTP 각각의 제한(초) | 선택 | `15.0` | `15` (0 초과~60) |
| `GMAIL_MAX_MESSAGES_24H` | 최근24시간 발송 작업 예산 | 선택 | `450` | `450` (1~500) |
| `ALERT_SMTP_HOST` | 기존 v1 알림 기록의 LOG/EMAIL 채널 표시 선택. 실제 SMTP·Gmail 발송을 켜지 않음 | 선택 | 없음 | `smtp.example.test` |
| `ALERT_SMTP_PORT` | SMTP 예약 포트. 현재 발송에 사용하지 않음 | 선택 | `587` | `587` |
| `ALERT_SMTP_USER` | SMTP 예약 사용자. 현재 발송에 사용하지 않음 | 선택 | 없음 | `<비공개 사용자>` |
| `ALERT_SMTP_PASSWORD` | SMTP 예약 비밀번호. 현재 발송에 사용하지 않음 | 선택 | 없음 | `<비공개 비밀번호>` |
| `ALERT_RECIPIENT_EMAIL` | 기존 수신 주소 예약 설정. 현재 수신자 DB를 대체하지 않음 | 선택 | 없음 | `recipient@example.test` |

Gmail은 파일 경로와 발신 주소를 함께 요구한다. 실제 파일과 비밀값은 이미지·Git·공개 asset에
포함하지 않고 읽기 전용 파일로 주입한다. 승인 계정·발송 권한·유효성·재승인 주체와 수신 정책은
배포 담당자가 관리한다. [Gmail 운영 계약](GMAIL_EMAIL_DELIVERY.md)을 함께 따른다.

## 앱 이외의 실행 입력

다음은 `Settings` 항목이 아니며 배포 도구 또는 게이트웨이가 해석한다.
이 구분은 앱이 임의 포트·이미지·DB 계정을 생성해 준다는 의미가 아니다.

| 항목 | 의미·필수 여부 | 기본값 | 형식 예시 |
| --- | --- | --- | --- |
| `MEDIA_PROXY_UPSTREAM` | 선택적 기존 미디어 게이트웨이의 상위 host:port, 사용 시 필수 | 없음 | `media-service:8080` |
| `MEDIA_PROXY_PORT` | 해당 게이트웨이의 호스트 게시 포트 | `8080` | `8080` |
| `BACKEND_IMAGE` | PostgreSQL 실행 템플릿의 검증한 이미지, 사용 시 필수 | 없음 | `ghcr.io/ajin-scrap-monitoring/scrap-monitoring-backend@sha256:<64자리 hex>` |
| `POSTGRES_IMAGE` | 검증한 PostgreSQL17 이미지, 사용 시 필수 | 없음 | `postgres@sha256:<64자리 hex>` |
| `BACKEND_ENV_FILE` | 비공개 앱 설정 파일의 호스트 절대 경로, 템플릿 사용 시 필수 | 없음 | `/absolute/private/backend.env` |
| `BACKEND_PORT` | PostgreSQL 템플릿의 loopback 게시 API 포트 | `8000` | `8000` |
| `DB_ADMIN_SCRAM` | 새 DB 관리자 계정의 SCRAM 검증값 | 없음 | `<비공개 SCRAM-SHA-256 검증값>` |
| `DB_OWNER_SCRAM` | 새 DB 객체 소유 계정의 SCRAM 검증값 | 없음 | `<비공개 SCRAM-SHA-256 검증값>` |
| `DB_APP_SCRAM` | 새 앱 계정의 SCRAM 검증값 | 없음 | `<비공개 SCRAM-SHA-256 검증값>` |
| `DB_BACKUP_SCRAM` | 새 백업 계정의 SCRAM 검증값 | 없음 | `<비공개 SCRAM-SHA-256 검증값>` |
| `DB_OWNER_PASSWORD_URLENCODED` | migration/관리용 실제 비밀번호, 해당 템플릿 사용 시 필요 | 없음 | `<URL 인코딩 비밀번호>` |
| `DB_APP_PASSWORD_URLENCODED` | 앱 DB 연결용 실제 비밀번호, 해당 템플릿 사용 시 필요 | 없음 | `<URL 인코딩 비밀번호>` |

SCRAM 검증값과 실제 접속 비밀번호는 다르다. URL 인코딩도 암호화가 아니다.
초기 계정 입력·기존 볼륨의 취급·실행 역할은 [PostgreSQL 구성](POSTGRESQL_DEPLOYMENT.md)을 따른다.
정기 백업의 추가 도구 입력은 [백업 실행 문서](SCHEDULED_BACKUPS.md)가 정본이다.

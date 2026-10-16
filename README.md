# scrap-monitoring-backend

아진산업 스크랩 적재 모니터링 시스템의 핵심 백엔드 서비스로서, 센서 엣지 장비(LiDAR)로부터 산출된 높이 및 적재율 데이터를 수신하고 시계열 측정치와 운영 이력을 영속 저장소에 관리하는 중앙 허브 역할을 담당한다.

웹 대시보드 브라우저를 대상으로 저지연 실시간 상태 푸시 스트림(WebSocket 및 Server-Sent Events)과 통계 분석 API를 제공하며, 설정된 수거 임계치에 도달했을 때 채터링 방지 및 쿨다운 정책을 적용한 비동기 알림 이벤트를 관리자 및 외부 채널로 전달한다.

## 주요 기능

* **센서 메트릭 인입 및 무결성 검증**: LiDAR 엣지 프로세스가 전달하는 적재 높이(cm), 적재율(%), 센서 진단 데이터 수신 및 인증/살균 처리
* **실시간 상태 브로드캐스팅**: WebSocket 및 Server-Sent Events(SSE) 파이프라인을 통한 대시보드 클라이언트 실시간 상태 동기화
* **데이터 영속화**: SQLAlchemy 2.0 Asyncio 기반 4대 도메인 모델(시계열 메트릭, 구덩이 설정 마스터, 운영/알림 로그, 미디어 색인 메타데이터) 저장
* **상태 판정 및 알림 엔진**: 3단계 상태 전이(NORMAL / WARNING / CRITICAL), 30분 쿨다운 및 센서 노이즈 채터링 방지(Hysteresis) 제어
* **현장 수거 및 운영 제어**: 작업자 수거 완료 확인에 따른 상태 리셋 및 과거 수거 이력 관리 API 제공

## 빠른 시작

### 사전 조건

* Python 3.13 이상
* [uv](https://github.com/astral-sh/uv) 패키지 관리자

### 설치 및 첫 실행

최초 실행 전에 비공개 `.env`에 `ADMIN_PASSWORD`를 12~1024자로 설정한다. 조회 계정이 필요하면 `OPERATOR_PASSWORD`도 별도로 설정한다. 로컬 실행에서는 `.env.example`의 로컬용 `DATABASE_URL` 예시를 사용한다. 초기화·재설정 방법은 [AUTH_STORAGE.md](docs/AUTH_STORAGE.md)를 참고한다.

```bash
# 1. 의존성 동기화 및 가상환경 생성
uv sync

# 2. 서버 실행
uv run uvicorn src.app.main:app --host 0.0.0.0 --port 8000
```

실행 후 브라우저 또는 터미널에서 `http://localhost:8000/healthz` 및 Swagger API 문서 `http://localhost:8000/docs`를 확인합니다.

## 설정

환경 설정은 `.env` 파일을 통해 주입받으며, `.env.example`을 복사하여 사용할 수 있습니다.

| 환경 변수 | 기본값 | 설명 |
| --- | --- | --- |
| `APP_ENV` | `development` | 실행 환경 (`development`, `production`, `test`) |
| `PORT` | `8000` | 서버 바인드 포트 |
| `DATABASE_URL` | `sqlite+aiosqlite:///./test_scrap_monitoring.db` | 비동기 DB 접속 URL (운영 시 PostgreSQL asyncpg 권장) |
| `EDGE_API_KEY` | `dev-edge-secret-key-12345` | 엣지 센서 데이터 인입 시 검증할 비밀키 |
| `DEFAULT_WARNING_THRESHOLD` | `75.0` | 스크랩 경고 기본 임계율 (%) |
| `DEFAULT_CRITICAL_THRESHOLD` | `85.0` | 스크랩 수거 요청 기본 임계율 (%) |
| `ALERT_COOLDOWN_SECONDS` | `1800` | 알림 엔진 쿨다운 시간 (초, 기본 30분) |

## 개발 및 검증

```bash
# 코드 스타일 및 린트 검사
uv run ruff check .

# 코드 포맷 검사
uv run ruff format --check .

# 정적 타입 분석
uv run mypy src

# 단위, 통합 및 센서 리플레이 테스트 실행
uv run pytest -v
```

## 배포 및 Docker Compose 실행

Docker Compose를 사용하여 백엔드 서비스를 컨테이너로 손쉽게 실행할 수 있습니다. 수집된 측정치와 데이터베이스는 `./data` 디렉토리에 안전하게 영구 보존됩니다.

```bash
# 1. 환경 설정 파일 준비
cp .env.example .env

# 2. 컨테이너 빌드 및 백그라운드 실행
docker compose up -d --build

# 3. 서비스 상태 및 로그 확인
docker compose ps
docker compose logs -f

# 4. 서비스 종료
docker compose down
```

## 문서

* [docs/COMPACT_LIDAR_STORAGE.md](docs/COMPACT_LIDAR_STORAGE.md): 최신 라이다 관측·중앙값·분 요약·상태 사건과 활성화 설정
* [docs/LIDAR_HEIGHT_PROFILES.md](docs/LIDAR_HEIGHT_PROFILES.md): 검증 좌표·바닥 기준을 사용하는 높이 프로필과 단면 기준 계산
* [docs/HEIGHT_FILL_PIPELINE.md](docs/HEIGHT_FILL_PIPELINE.md): 선택적인 단면 입력·시간 중앙값·분 이력·수거 필요 판정과 출처
* [docs/HEIGHT_TEMPORAL_MEDIAN.md](docs/HEIGHT_TEMPORAL_MEDIAN.md): 높이(cm) 시간 중앙값과 기존 높이 필드·단위 유지

* [docs/SCHEDULED_BACKUPS.md](docs/SCHEDULED_BACKUPS.md): 정기 백업·성공본 보관·실패 상태 확인과 SQLite WAL 백업
* [docs/POSTGRESQL_DEPLOYMENT.md](docs/POSTGRESQL_DEPLOYMENT.md): PostgreSQL 전용 계정·권한·볼륨·내부 연결과 배포 검증
* [docs/DB_BACKUP_RECOVERY.md](docs/DB_BACKUP_RECOVERY.md): PostgreSQL 백업·격리 복원 검증과 운영 복구 절차
* [docs/DB_TRANSFER.md](docs/DB_TRANSFER.md): SQLite→PostgreSQL 시험 이전·적용·전체 비교
* [docs/DB_MIGRATIONS.md](docs/DB_MIGRATIONS.md): DB 버전 관리·기존 DB 등록·적용 명령·시작 전 검사
* [docs/POSTGRESQL_TESTING.md](docs/POSTGRESQL_TESTING.md): 실제 PostgreSQL에서 API 저장·조회 검증
* [docs/NOTIFICATION_READS.md](docs/NOTIFICATION_READS.md): 계정별 알림 읽음·미확인 개수·로그인 및 CSRF 검사
* [docs/ALERT_POLICY_STORAGE.md](docs/ALERT_POLICY_STORAGE.md): 알림 정책 영속 저장·재시작 유지·ETag 동시 변경 검사
* [docs/ADMIN_ACCESS.md](docs/ADMIN_ACCESS.md): 관리자 설정 API의 로그인·역할·CSRF 검사와 프론트엔드 요청 방법
* [docs/AUTH_STORAGE.md](docs/AUTH_STORAGE.md): 로그인 계정·세션 DB 저장, 최초 비밀번호 설정과 재설정

* [docs/EDGE_V2_INTEGRATION.md](docs/EDGE_V2_INTEGRATION.md): 엣지 v2 수신 API, 장치별 인증 및 저장 ACK 계약
* [docs/MONITORING_V2.md](docs/MONITORING_V2.md): v2 판정·장비 상태 조회, 피트 연결 설정 및 대시보드 SSE 계약
* [docs/V2_LOAD_HISTORY.md](docs/V2_LOAD_HISTORY.md): 최근 24시간 적재율 추이, 시간 구간과 판정 불가·누락 처리
* [docs/V2_OPERATIONS.md](docs/V2_OPERATIONS.md): v2 경보 기록, 영속 쿨다운 및 수거 사건 개정 이력
* [docs/DAILY_RECORDINGS.md](docs/DAILY_RECORDINGS.md): 날짜별 상시 녹화 조회·HLS 재생·하루 MP4 다운로드 연동
* [docs/MEDIA_INTEGRATION.md](docs/MEDIA_INTEGRATION.md): 카메라 미디어 API, 이벤트 녹화 및 인증된 영상 접근 경로 설정
* [docs/GLOBAL_CONSTRAINTS.md](docs/GLOBAL_CONSTRAINTS.md): 백엔드 개발 시 반드시 준수해야 하는 전역 제약조건 정의서

## 이용 조건

이 Repository는 코드 검토와 참고를 위해 Public으로 제공하며 프로젝트 소스 코드에 별도 라이선스를 부여하지 않는다.

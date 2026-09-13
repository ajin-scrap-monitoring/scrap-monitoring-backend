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

## 배포

GitHub Container Registry (GHCR)의 Public OCI 컨테이너 이미지로 배포합니다.

* 이미지 경로: `ghcr.io/ajin-scrap-monitoring/monitoring-backend-service`
* 컨테이너 빌드:

```bash
docker build -t monitoring-backend-service:local .
```

## 문서

* [docs/GLOBAL_CONSTRAINTS.md](docs/GLOBAL_CONSTRAINTS.md): 백엔드 개발 시 반드시 준수해야 하는 전역 제약조건 정의서

## 이용 조건

이 Repository는 코드 검토와 참고를 위해 Public으로 제공하며 프로젝트 소스 코드에 별도 라이선스를 부여하지 않는다.

# 배포 시 v2 경보 활성화 방법

수정할 파일은 **실행 중인 백엔드 Compose가 읽는 환경 설정 파일**이다. 소스 코드와 `.env.example`은 수정하지 않는다. 아래 절차는 #31 코드가 반영된 백엔드에서 적용한다. 이 문서는 설정 방법이며 서버에 자동 적용하지 않는다.

## 1. 실행 방식에 맞는 파일 선택

명령은 서버의 프로젝트 루트에서 실행한다.

| 실행 방식 | 수정할 파일 |
| --- | --- |
| 기본 `docker-compose.yml`로 배포한 백엔드 | 프로젝트 루트의 `.env` |
| 별도로 만든 `scrap-backend-check` 테스트 백엔드 | `data/backend-check/backend.env` |

기본 배포에서 `.env`가 아직 없으면 `.env.example`을 복사한 뒤 인증정보·DB 등 필요한 값을 먼저 설정한다. 이미 설정 파일이 있으면 복사로 덮어쓰지 않고 해당 항목을 수정한다.

```bash
# 기본 배포에서 .env가 없을 때만 생성
test -f .env || cp .env.example .env
```

두 실행 방식의 파일은 서로 다르다. 테스트 백엔드를 실행 중일 때 프로젝트 루트 `.env`만 바꾸면 테스트 백엔드에는 반영되지 않는다.

## 2. 환경 설정 수정

선택한 파일에서 다음 항목을 추가하거나 기존 값을 변경한다. 같은 항목을 여러 줄에 중복해서 넣지 않는다.

```dotenv
EDGE_V2_OPERATIONS_ENABLED=true
EDGE_V2_CRITICAL_THRESHOLD=90
ALERT_COOLDOWN_SECONDS=1800
```

또한 `EDGE_V2_DEVICES`에 실제 장치·센서 ID와 장치 토큰이, `EDGE_V2_MONITORING_SOURCES`에 해당 장치와 피트의 연결이 설정되어 있어야 한다. 형식과 합성 예시는 [MONITORING_V2.md](MONITORING_V2.md)와 [EDGE_V2_INTEGRATION.md](EDGE_V2_INTEGRATION.md)를 참고한다. 실제 ID·토큰이 담긴 파일은 Git에 올리지 않는다.

기본 위험 진입은 90%, 위험 상태 복귀는 85% 이하, 알림 쿨다운은 30분이다. DB에 피트별 임계치가 설정되어 있으면 그 값이 우선한다. 이 경우 복귀 기준은 위험 임계치에서 5%p를 뺀 값이다.

## 3. 실행 방식에 맞춰 적용

환경변수를 다시 읽도록 **백엔드 컨테이너를 재생성**한다. `docker compose restart`만으로는 변경한 환경변수가 반영되지 않는다.

기본 배포는 #31 코드가 포함된 이미지가 서버에 준비된 상태에서 다음을 실행한다.

```bash
sudo docker compose config --quiet
sudo docker compose up -d --no-build --pull never --force-recreate backend
sudo docker compose ps backend
```

현재 별도 테스트 백엔드를 사용하는 경우에는 위 명령 대신 아래를 실행한다. 이 테스트 구성은 프로젝트의 `src`를 읽기 전용으로 연결하므로, #31 병합 후 서버의 해당 소스도 최신으로 갱신되어 있어야 한다. 테스트 이미지의 런타임 의존성이 소스의 잠금 파일과 일치하는지도 확인한다.

```bash
sudo docker compose -p scrap-backend-check -f data/backend-check/compose.yml config --quiet
sudo docker compose -p scrap-backend-check -f data/backend-check/compose.yml up -d --no-build --pull never --force-recreate backend
sudo docker compose -p scrap-backend-check -f data/backend-check/compose.yml ps backend
```

두 구성 중 실제 사용 중인 것 하나에만 적용한다. 위 명령은 백엔드 서비스만 대상으로 하며 서버에서 이미지 빌드나 다운로드를 수행하지 않는다. 운영 이미지는 맥북 또는 CI에서 빌드·검증하고 배포한다. `src` 연결 방식의 테스트 구성과 운영 이미지 배포는 구분한다.

## 4. 적용 확인과 다시 끄기

백엔드가 healthy인지 확인하고, 브라우저 세션으로 로그인한 뒤 `GET /api/v2/monitoring/snapshot?pit_id=<설정한 피트 ID>`를 조회한다.

- 활성화되면 `operations`가 객체로 반환된다. 최초 판정 전에는 `lastKnownState: null`, `effectiveState: UNKNOWN`일 수 있다.
- 비활성화 상태에서는 `operations: null`이다.
- 설정하지 않은 피트를 조회하면 `404 MONITORING_SOURCE_NOT_CONFIGURED`가 반환된다.
- 경보는 신선하고 검증된 판정이 실제로 도착해야 기록된다. 활성화만으로 시험 경보가 생성되지는 않는다.

다시 끄려면 같은 파일에서 `EDGE_V2_OPERATIONS_ENABLED=false`로 바꾸고 3번의 해당 재생성 명령을 다시 실행한다. 기존 DB의 경보 이력과 쿨다운은 보존한다.

이 설정은 **DB 경보 기록과 알림함 표시**를 활성화한다. 문자·이메일 발송이나 자동 카메라 녹화를 활성화하는 설정은 아니다. 자세한 동작은 [V2_OPERATIONS.md](V2_OPERATIONS.md)를 참고한다.

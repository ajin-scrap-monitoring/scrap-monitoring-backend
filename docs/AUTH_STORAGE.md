# 로그인 계정과 세션 DB 저장

로그인 API와 관리자·조회 사용자 역할을 유지하면서 계정과 로그인 세션을 `DATABASE_URL`의 DB에 저장한다. 회원가입이나 계정 관리 화면은 추가하지 않는다. 관리자 화면의 알림 수신 대상은 로그인 계정과 별도 데이터다.

## 최초 배포 설정

실행할 Compose가 읽는 비공개 환경 설정 파일에 초기 비밀번호를 설정한다. 기본 배포는 프로젝트 루트 `.env`, 별도 테스트 백엔드는 `data/backend-check/backend.env`다.

```dotenv
ADMIN_PASSWORD=<직접 생성한 12~1024자 비밀번호>
OPERATOR_PASSWORD=<조회 계정이 필요하면 별도의 12~1024자 비밀번호>
```

위 값은 입력 위치를 설명하는 자리표시자이며 그대로 사용하지 않는다. 실제 비밀번호는 Git·이슈·로그에 기록하지 않는다.

처음 시작할 때 `auth_users`가 비어 있으면 `admin` 관리자 계정을 만든다. 초기 관리자 비밀번호가 없거나 길이가 맞지 않으면 시작을 중단한다. `OPERATOR_PASSWORD`가 있으면 `operator` 조회 계정도 함께 만든다. 비어 있으면 조회 계정은 생성하지 않는다. 기본 비밀번호는 없다.

계정이 한 개라도 존재하면 환경변수로 기존 비밀번호·권한을 덮어쓰거나 계정을 추가하지 않는다. 이후 인증 기준은 DB다. 환경변수 비밀번호를 바꾸고 재시작하는 것으로 기존 계정의 비밀번호가 변경되지 않는다. 초기화가 끝나면 환경 설정에서 초기 비밀번호를 제거해도 재시작할 수 있다.

이번 변경은 `argon2-cffi` 의존성을 추가한다. **새 이미지가 필요하며 기존 이미지에 src만 연결하는 것으로 배포할 수 없다.** 맥북 또는 CI에서 대상 서버 아키텍처에 맞춰 이미지를 빌드하고 준비된 새 이미지로 백엔드를 재생성한다. 환경 설정 변경도 `restart`가 아닌 컨테이너 재생성으로 적용한다. 기존 DB 볼륨은 유지한다.

이전 메모리 세션은 DB로 옮길 수 없으므로 최초 전환 시 사용자는 다시 로그인한다. 전환 후 발급한 유효 세션은 재시작해도 유지된다.

## 저장 내용

| 테이블 | 저장 정보 |
| --- | --- |
| `auth_users` | 아이디, 표시 이름, 역할, Argon2id 비밀번호 해시, 활성 여부, 비밀번호 버전 |
| `auth_sessions` | 쿠키 토큰의 SHA-256 해시, 사용자 ID, 비밀번호 버전, CSRF 토큰, 만료 시각 |

계정과 세션은 두 테이블에 저장한다. 스키마 생성·기존 DB 등록은 [DB 마이그레이션 절차](DB_MIGRATIONS.md)를 먼저 따른다. SQLite에서는 기존 수집 데이터와 같은 DB 파일에 저장된다. 기본 Compose 설정은 호스트의 `data/scrap_monitoring.db`를, 별도 테스트 구성은 `data/backend-check/db/receiver-check.db`를 사용한다. 실제 위치는 해당 실행 환경의 `DATABASE_URL`과 볼륨 매핑이 기준이다. 운영 모드의 PostgreSQL 요구사항은 기존 설정 검증을 따른다.

비밀번호는 평문이나 복호화 가능한 암호문으로 저장하지 않는다. Argon2id는 19 MiB 메모리, 반복 2회, 병렬도 1을 사용한다. CPU 작업은 이벤트 루프 밖의 전용 작업 스레드 하나에서 처리해 동시에 실행하는 해시 계산을 제한한다. 알 수 없는 계정도 더미 해시를 검증하며 손상된 해시는 인증 실패로 처리한다.

브라우저 쿠키에는 무작위 토큰이 있고 DB에는 그 해시만 보관한다. CSRF 토큰은 기존 응답을 재현하기 위해 세션 행에 저장하며, 이 토큰만으로 로그인할 수 없다. 비밀번호나 세션 쿠키 값은 API 오류에 포함하지 않는다.

## API 동작

- `POST /api/v1/session`: 기존 `username`, `password`, `persistent` 입력과 `user`, `expiresAt`, `csrfToken` 성공 응답을 유지한다. 저장 커밋 완료 후 쿠키를 발급한다.
- `GET /api/v1/session`: DB의 활성 여부·현재 역할·비밀번호 버전·만료 시각을 확인한다. DB에서 비활성화된 계정은 세션 사용과 새 로그인을 거부한다.
- `DELETE /api/v1/session`: DB 세션을 삭제해 다른 프로세스에서도 해당 쿠키를 사용할 수 없게 한다. 기존 선택적 CSRF 헤더 검증은 유지한다.
- 일반 로그인은 24시간, `persistent: true`는 30일이다. DB 만료는 절대 시각 기준이며 조회·재시작으로 연장하지 않는다. 새 로그인 시 만료된 세션 행을 정리한다.
- 쿠키 이름 `scrap_session`, HttpOnly, SameSite=Lax, 기존 Max-Age를 유지하고 production에서는 Secure를 설정한다. 세션 API 응답은 캐시하지 않는다.
- 로그인 실패는 401, 잘못된 입력은 비밀번호를 노출하지 않는 422, 인증 DB 장애는 안전한 503으로 처리한다.

미디어 접근 검사와 v2 조회 API도 같은 DB 세션을 사용한다. v2 SSE는 매 확인 시 DB에서 유효성을 확인하므로 로그아웃·만료·비밀번호 재설정 후 `session.expired`를 전송하고 종료한다. 인증용 읽기 트랜잭션은 미디어 요청과 SSE 대기 전에 끝낸다.

## 비밀번호 재설정

DB에 등록된 기존 계정의 비밀번호를 바꿀 때는 관리 명령을 사용한다. 올바른 DB를 가리키는 환경에서 실행하며 새 비밀번호는 명령행 인자가 아닌 숨김 입력으로 받는다.

```bash
# uv로 실행하는 환경
uv run python -m src.app.cli.auth reset-password admin

# 기본 Compose의 새 런타임 이미지 안에서 실행
sudo docker compose exec backend python -m src.app.cli.auth reset-password admin

# 별도 테스트 Compose가 새 이미지로 갱신된 경우
sudo docker compose -p scrap-backend-check -f data/backend-check/compose.yml exec backend python -m src.app.cli.auth reset-password admin
```

비밀번호를 두 번 입력하면 새 해시와 버전을 저장하고 해당 사용자의 모든 세션을 같은 트랜잭션에서 폐기한다. 실패하면 함께 롤백한다. `operator`도 같은 명령에서 아이디를 바꿔 실행한다. 이 명령은 신규 계정을 만들지 않는다.

## 범위와 검증

사용자 등록·계정 관리 화면, 외부 로그인 연동, 운영 DB 배포 자체는 이번 변경 범위가 아니다. DB 세션은 여러 프로세스에서 조회할 수 있지만 미디어·경보 작업자에는 분산 작업 선점이 없어 배포는 계속 단일 Uvicorn worker를 사용한다.

관리자 설정·알림 수신 대상·테스트 알림 및 피트 설정 API에도 DB 세션 기반 접근 검사를 적용한다. 관리자 정보 조회는 관리자만 가능하고, 피트 설정 조회는 로그인 사용자에게 허용한다. 변경 요청에는 관리자 권한과 CSRF 토큰이 필요하다. 자세한 경로와 프론트엔드 요청 방법은 [관리자 API 권한 안내](ADMIN_ACCESS.md)를 따른다.

개인 알림함의 읽음 상태와 미확인 개수는 로그인 계정별로 관리한다. 알림 조회에는 로그인이, 읽음 변경에는 같은 세션의 CSRF 토큰이 필요하다. [사용자별 알림 읽음 안내](NOTIFICATION_READS.md)를 따른다.

기존 API 회귀와 신규 합성 테스트로 해시 저장·만료·역할·비활성화·재설정·트랜잭션 실패를 검증한다. 실제 이미지에서 재시작 후 기존 쿠키와 CSRF 유지, 로그아웃 후 재시작해도 세션이 복구되지 않는지, 영상 게이트웨이 인증을 확인한다.

근거: [OWASP 비밀번호 저장 권고](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html), [argon2-cffi 공식 API](https://argon2-cffi.readthedocs.io/en/stable/api.html).

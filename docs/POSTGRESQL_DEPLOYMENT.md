# PostgreSQL 배포 구성

`docker-compose.postgresql.yml`은 기존 SQLite 개발 Compose와 분리한 PostgreSQL 17 배포 구성이다. 검토한 이미지와 비공개 설정을 외부에서 주입한 뒤 사용한다. 저장소 기본 Compose를 실행하거나 소스를 pull하는 것만으로 이 구성이 적용되지는 않는다.

## 구성과 저장 경계

| 서비스/계정 | 역할 |
| --- | --- |
| `postgres` / `postgres` | DB 서비스와 초기 관리 계정. 서버에는 SCRAM 검증값만 전달 |
| `manage` / `scrap_owner` | 일회성 Alembic·데이터 이전 관리 작업. DB·public 스키마와 업무 객체 소유 |
| `backend` / `scrap_app` | 단일 worker 앱. 업무 DML·시퀀스 USAGE/SELECT, 구조/역할 변경·시퀀스 setval 불가 |
| `scrap_backup` | 별도 백업 작업에서 사용할 조회 계정. 테이블·시퀀스 SELECT만 허용 |

DB 포트는 호스트에 게시하지 않는다. `postgres`는 프로젝트의 `internal` DB 네트워크에만 연결한다. backend/manage는 DB 네트워크와 별도 application 네트워크에 연결하므로 앱의 외부 미디어 JSON 요청은 가능하다. API는 호스트 loopback에만 게시하며 실제 배포의 HTTPS 게이트웨이가 전달한다. 다른 Compose의 게이트웨이를 연결해야 하면 해당 네트워크와 서비스 주소를 별도 검토한다.

DB는 프로젝트 이름에 종속된 `postgres_data` named volume의 `/var/lib/postgresql/data/pgdata`에 저장한다. 프로젝트 이름을 바꾸면 다른 볼륨이 선택된다. 임의 `down --volumes`는 데이터를 지우므로 운영에서 사용하지 않는다. DB 메모리 512MiB·CPU 1개·최대 연결 40개는 초기 설정이며 실제 데이터와 동시 요청 기준으로 조정해야 한다. 백엔드도 CPU 1개·512MiB, 읽기 전용 파일시스템과 임시 `/tmp`를 사용하고 로그 크기를 제한한다.

초기 볼륨에서는 peer 로컬 인증·SCRAM TCP 인증·데이터 checksum을 설정하고 계정을 생성한다. 역할 생성이 끝나야 초기화 완료 파일을 만든다. 최초 초기화가 실패해 이미지가 재시작한 경우에도 이 파일이 없으면 healthy가 되지 않는다. 실패 원인과 볼륨 상태를 확인하며 데이터를 자동 삭제해서 해결하지 않는다.

이미 사용한 볼륨에서는 init 스크립트를 재실행하지 않고 환경 설정 변경만으로 DB 비밀번호를 재설정하지 않는다. 공식 이미지가 관리하는 데이터 디렉토리 소유권을 임의 UID/GID로 덮어쓰지 않는다. 최초 적용 때 mount·용량·권한과 이미지 major를 확인한다. PostgreSQL 18의 데이터 디렉토리 규칙으로 이 구성을 그대로 전환하지 않는다.

## 외부에서 준비할 설정

아래 이름은 Compose 실행 환경에 주입한다. 비밀값을 명령행 인자·Git·로그에 붙이지 않고, 승인된 비공개 설정 관리 방식으로 제공한다. `.env`를 자동 선택하지 않도록 예시 명령은 `--env-file /dev/null`을 사용한다. `BACKEND_ENV_FILE`은 이와 별개로 앱에만 읽히는 파일이다.

| 이름 | 내용 |
| --- | --- |
| `BACKEND_IMAGE` | 검토한 백엔드 이미지 참조. 운영은 `repository@sha256:...` digest 사용, 서버 CPU 아키텍처 확인 |
| `POSTGRES_IMAGE` | 검토한 PostgreSQL **17** 이미지 digest. 로컬 검증은 `postgres:17-alpine` 사용 |
| `BACKEND_ENV_FILE` | 비공개 앱 설정 파일의 절대 경로. 장치 인증·피트 연결·CORS·미디어 연결 등 기존 설정 유지 |
| `DB_ADMIN_SCRAM` | 초기 postgres 관리 계정의 SCRAM-SHA-256 검증값 |
| `DB_OWNER_SCRAM` | 초기 scrap_owner 계정의 SCRAM 검증값 |
| `DB_APP_SCRAM` | 초기 scrap_app 계정의 SCRAM 검증값 |
| `DB_BACKUP_SCRAM` | 초기 scrap_backup 계정의 SCRAM 검증값 |
| `DB_OWNER_PASSWORD_URLENCODED` | scrap_owner의 실제 비밀번호를 URL 인코딩한 값. 위 검증값과 같은 비밀번호 사용 |
| `DB_APP_PASSWORD_URLENCODED` | scrap_app의 실제 비밀번호를 URL 인코딩한 값. 위 검증값과 같은 비밀번호 사용 |
| `ADMIN_PASSWORD` | 계정 테이블이 빈 새 설치의 초기 관리자 비밀번호. 기존 계정 이전 시에는 불필요 |
| `BACKEND_PORT` | loopback API 포트, 기본 8000. 기존 공유 서버 포트와 충돌하지 않도록 선택 |

URL 인코딩은 암호화나 해시가 아니다. owner/app 비밀번호는 클라이언트 접속에 필요하므로 프로세스 환경과 Docker 관리 권한을 보호해야 한다. 서버 저장용 검증값과 접속용 비밀번호를 혼동하지 않는다. 네 계정에 서로 다른 강한 비밀번호를 사용하고, 검증값 생성과 입력도 비공개 관리 절차에서 수행한다. 로컬 검증은 비밀번호를 메모리에서만 생성하고 파일로 저장하지 않는다.

Compose가 `APP_ENV=production`, `DEBUG=false`, 역할별 `DATABASE_URL`을 고정한다. 비공개 앱 파일의 기존 SQLite URL은 이 설정으로 덮어쓴다. 기존 장치/미디어 설정까지 개발 예시로 교체하지 않는다. 새 설치의 `ADMIN_PASSWORD`는 앱 파일 대신 실행 환경에서 제공하며, 기존 계정 비밀번호 변경은 [계정 CLI](AUTH_STORAGE.md)를 사용한다. 외부 gateway의 TLS와 비공개 Docker 네트워크를 전제로 하며 DB를 다른 호스트에 공개하는 구성의 TLS를 대신하지 않는다.

자격 증명이 포함된 `docker compose config`, `docker inspect` 전체 출력과 셸 `set -x`를 로그에 남기지 않는다. 설정 검사에는 `config --quiet`를 사용한다. Docker 관리자는 컨테이너 환경을 조회할 수 있으므로 Docker 권한도 비밀 관리 범위다.

## 적용 순서

다음은 배포 구성을 선택하는 명령이다. 실제 공유 서버에서는 먼저 대상 컨테이너·Compose·볼륨·환경 파일·포트와 백업을 확인한다. 이미지 빌드와 무거운 검증은 로컬에서 마치고, 새 프로젝트 이름은 비공개 배포 설정에 고정한다. DB 초기화 후 바로 backend를 시작하지 않는다.

```bash
# 필요한 이미지·설정·자격 증명 변수가 주입된 관리 환경에서 실행한다.
docker compose --env-file /dev/null -p scrap-postgresql -f docker-compose.postgresql.yml config --quiet
docker compose --env-file /dev/null -p scrap-postgresql -f docker-compose.postgresql.yml up -d --wait postgres

# 스키마만 준비한다. 앱/계정 초기화는 아직 실행하지 않는다.
docker compose --env-file /dev/null -p scrap-postgresql -f docker-compose.postgresql.yml \
  run --rm manage -m src.app.cli.db upgrade
```

SQLite 이전 설치는 원본 보존 백업과 별도 이전용 복사본을 준비하고 [DB_TRANSFER.md](DB_TRANSFER.md)의 check/apply/verify를 `manage`로 실행한다. 관리 컨테이너에 이전용 디렉토리만 read-only로 추가하고, `--source /migration/source.db`를 사용한다. 실제 경로는 확인한 복사본으로 선택한다. 운영 SQLite 원본을 바로 migration하거나 영상 디렉토리 전체를 mount하지 않는다.

```bash
# SQLITE_MIGRATION_DIR은 검증한 이전용 복사본 디렉토리의 절대 경로다.
docker compose --env-file /dev/null -p scrap-postgresql -f docker-compose.postgresql.yml \
  run --rm --volume "$SQLITE_MIGRATION_DIR:/migration:ro" manage \
  -m src.app.cli.transfer check --source /migration/source.db
# 시험 통과 뒤 같은 구성으로 apply, 이어서 verify를 실행한다.
```

스키마 준비·데이터 이전 또는 복원 후에는 아래 권한 적용을 완료하고 앱을 시작한다. 새로 생성한 객체에는 owner의 default privileges가 적용되며 이 명령은 기존/복원 객체에도 권한을 맞춘다. `alembic_version`은 앱이 읽기만 가능하도록 별도로 제한한다. migration 이후에도 이 순서를 반복한다.

```bash
docker compose --env-file /dev/null -p scrap-postgresql -f docker-compose.postgresql.yml \
  exec -T --user postgres postgres psql --no-password --dbname=scrap_monitoring \
  --file=/opt/scrap-postgresql/runtime-grants.sql
docker compose --env-file /dev/null -p scrap-postgresql -f docker-compose.postgresql.yml \
  run --rm manage -m src.app.cli.db check
docker compose --env-file /dev/null -p scrap-postgresql -f docker-compose.postgresql.yml \
  up -d --wait backend
```

새 빈 설치만 명시적 `ADMIN_PASSWORD`로 최초 계정을 만든다. 이전 설치는 기존 계정/해시를 유지한다. readyz, 계정·설정·이력·수신·영상 gateway를 실제 연결로 확인한다. 운영 세션 쿠키는 Secure이므로 사용자 로그인 검증은 HTTPS gateway에서 수행한다. liveness만으로 DB 연결이 정상이라고 판단하지 않는다.

볼륨 보존 컨테이너 교체는 백업과 대상 확인 후 수행한다. 이미지 변경·계정 교체와 실제 데이터 이전은 서로 다른 절차다. 이번 구성에는 정기 백업 스케줄·보관·외부 저장소·실패 알림을 포함하지 않는다. `scrap_backup`의 실제 pg_dump 접속 권한은 검증하며 자동 백업은 후속 작업이다. 복구는 [DB_BACKUP_RECOVERY.md](DB_BACKUP_RECOVERY.md)를 따른다.

## 로컬 검증

```bash
docker build -t scrap-backend-postgresql-check:local .
uv run python -m tests.integration.verify_postgresql_deployment --image scrap-backend-postgresql-check:local
```

새 소스로 빌드한 런타임 이미지와 위 배포 Compose를 그대로 사용한다. 무작위 프로젝트·임시 합성 설정 파일·메모리 비밀번호로 실행하며 운영 서버나 기존 볼륨을 받지 않는다. 별도 DB 네트워크·포트 비공개·SCRAM 저장, 스키마 적용 전 앱 시작 거절, migration 성공, 앱/백업 계정 권한과 잘못된 비밀번호 거절, 실제 pg_dump, production 앱 기동과 볼륨 보존 재생성 뒤 데이터/계정/세션 유지, 실패 초기화의 healthy 차단을 확인한다. 종료 때 해당 시험 프로젝트와 볼륨만 제거한다. GitHub CI도 이미지 빌드 후 같은 명령을 실행한다.

근거: [PostgreSQL 공식 이미지](https://hub.docker.com/_/postgres), [Compose internal 네트워크](https://docs.docker.com/reference/compose-file/networks/), [PostgreSQL default privileges](https://www.postgresql.org/docs/17/sql-alterdefaultprivileges.html), [SCRAM 인증](https://www.postgresql.org/docs/17/auth-password.html).

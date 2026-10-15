# DB 백업·복구

PostgreSQL의 업무 데이터·스키마·자동 ID를 백업하고, 새 DB에 복원한 뒤 확인하는 절차다. 백업은 계정 해시·세션·연락처·장치 원문도 포함하므로 비공개로 보관한다. 영상과 날짜별 녹화 카탈로그는 미디어 서비스의 별도 백업 대상이다.

## 로컬 복원 검증

Docker와 Python 3.13/uv 환경에서 다음 명령을 실행한다. GitHub CI도 같은 명령을 실행한다.

```bash
uv run python -m tests.integration.verify_db_backup
```

검증은 합성 데이터만 사용하며 운영 주소나 백업 파일을 입력받지 않는다.

1. Alembic으로 임시 PostgreSQL 17을 준비하고 합성 SQLite의 업무 테이블 16개를 이전한다.
2. `pg_dump --format=custom`으로 호스트의 권한 0600 파일에 백업한다. 성공 전 파일은 `.partial`로만 유지한다.
3. 원본 PostgreSQL 컨테이너와 볼륨을 제거한다. 별도 새 컨테이너의 빈 DB를 복원 대상으로 사용한다.
4. 일부가 잘린 백업의 복원을 먼저 시도한다. 실패 코드와 트랜잭션 롤백으로 부분 테이블이 남지 않는지 확인한다.
5. 정상 백업을 복원하고 Alembic 버전·현재 모델 구조·16개 테이블의 모든 컬럼을 비교한다. PK 정렬 후 행 수와 정규화 SHA-256을 메모리에서 비교하고 데이터나 해시를 출력하지 않는다.
6. 각 시퀀스의 `last_value/is_called`와 다음 ID를 확인한다. 삭제·롤백으로 생길 수 있는 번호 간격도 보존해야 하므로 `max(id)+1`만 비교하지 않는다.
7. 복원 전용 소유자와 일반 실행 계정을 별도로 만들고 필요한 권한을 다시 부여한다. 일반 계정으로 로그인·기존 세션·개인 읽음·새 기록 저장을 확인하고, FK 위반과 테이블 생성은 거절되는지 확인한다.
8. 데이터가 있는 대상에 재복원하면 실패하고 기존 값이 바뀌지 않는지 확인한다. 컨테이너·볼륨·합성 백업은 종료 시 제거한다.

JSON/SQL NULL·큰 정수·UTC·정책 버전·계정/세션 해시·설정·이력·관계는 전체 컬럼 비교에 포함된다. 테스트 비밀번호는 메모리에만 유지하고 Docker/SQL에는 SCRAM 검증값만 전달한다. 포트는 loopback 임의 포트, 컨테이너는 CPU 1개·메모리 512MiB로 제한하며 한 번에 하나씩 사용한다.

이 시험은 소량 합성 데이터와 로컬 Docker의 논리 백업 검증이다. 운영 용량의 복구 시간, 정기 스케줄·보관·외부 저장소·실패 알림, 실제 운영 계정 설정과 서버 배포를 완료했다는 뜻은 아니다. SIGKILL·호스트 장애 때 남은 시험 리소스는 생성된 이름과 위치를 확인한 뒤 정리한다.

## 운영 배포 전 준비

실제 배포에서는 다음 항목을 비공개 배포 설정에 확정한다. 기존 기본 Compose는 SQLite 개발 구성이며 이 문서가 PostgreSQL 서비스를 자동 배포하지 않는다.

| 항목 | 준비 기준 |
| --- | --- |
| 버전·이미지 | PostgreSQL 17과 같은 major의 dump/restore 도구, 검증한 이미지 digest·CPU 아키텍처 기록 |
| 네트워크 | DB 포트를 외부에 공개하지 않고 백엔드·관리 작업만 연결, 운영 환경의 TLS/인증 정책 적용 |
| 계정 | 소유/migration 계정과 실행 계정 분리, 실행 계정은 필요한 DML·스키마 USAGE·시퀀스 USAGE/SELECT만 부여 |
| 백업 계정 | 모든 백업 대상의 읽기 권한·시퀀스 조회 권한 확보, 운영 중 명령·DDL 권한을 실행 계정에 추가하지 않기 |
| 영속 저장 | PostgreSQL 17의 실제 PGDATA mount·UID/GID·쓰기 권한 확인, 컨테이너 교체 뒤 데이터 유지 시험 |
| 비밀 설정 | 환경·장치 키·DB 자격 증명을 비공개로 주입, URL/비밀번호를 명령행·Git·로그에 쓰지 않기 |
| 저장 예산 | DB·WAL·백업·격리 복원 DB를 함께 둘 여유 공간 확인, 무제한 보관 금지 |
| 백업 보관 | 로컬 디스크 외의 암호화된 별도 저장소, 보관 기간·접근 권한·복원용 키 보관 위치 확정 |
| 자동 실행 | 성공 시각·크기·체크섬·복원 검사 결과 기록, 겹친 실행 방지·실패 알림·오래된 백업 감시 구현 |
| 복구 목표 | 허용할 데이터 손실 시간과 서비스 복구 시간을 정하고 실제 크기로 소요 시간 측정 |

일반 계정에 테이블/시퀀스 권한을 부여할 때 `alembic_version` 쓰기는 제외할 수 있다. 여기의 합성 시험은 스키마 전체 DML을 부여하여 기능 복원을 검사하며, 최종 운영 권한 정책을 대신하지 않는다. 다음 migration으로 새 객체가 생길 때도 권한을 갱신하도록 소유자별 default privileges 또는 배포 절차를 준비한다.

## PostgreSQL 백업 만들기

PostgreSQL 17 `pg_dump`, `pg_restore`, `psql`이 있는 관리 환경에서 실행한다. 아래 명령은 libpq의 `PGHOST/PGPORT/PGDATABASE/PGUSER` 또는 service 설정이 **확인한 백업 원본**을 가리킨다는 전제다. 애플리케이션의 asyncpg `DATABASE_URL`을 그대로 libpq에 전달하지 않는다. 자격 증명은 승인된 비공개 관리 환경에서 제공하고 새 평문 비밀번호 파일을 만들지 않는다. `--no-password`는 비밀번호 없이 인증을 허용하는 옵션이 아니라 대화형 입력을 막는 옵션이다.

원격 Docker의 `exec`를 사용할 때는 확인한 DB 컨테이너의 도구로 실행하고, 아카이브 스트림에 TTY(`-t`)를 붙이지 않는다. 런타임 백엔드 이미지에는 PostgreSQL 도구가 없다. 명령의 stdout은 바이너리 백업이고 stderr는 오류이므로 둘을 한 파일에 합치지 않는다.

다음은 저장소 루트에서 실행하는 합성 경로 예시다. 운영 저장 경로와 별도 저장소는 배포 때 결정한다. `mkdir`가 새 폴더 생성에 실패하면 중단하여 같은 초의 백업도 덮어쓰지 않는다.

```bash
set -eu
umask 077
mkdir -p ./data/backups
BACKUP_SET="./data/backups/$(date -u +%Y%m%dT%H%M%SZ)"
mkdir "$BACKUP_SET"
pg_dump --no-password --format=custom --file="$BACKUP_SET/database.dump.partial"
pg_restore --list "$BACKUP_SET/database.dump.partial" > /dev/null
mv "$BACKUP_SET/database.dump.partial" "$BACKUP_SET/database.dump"
(cd "$BACKUP_SET" && sha256sum database.dump > database.dump.sha256)
```

`pg_dump`가 실패하면 `.partial`은 성공 백업이 아니다. stderr의 경고도 확인한다. checksum이나 `pg_restore --list` 성공은 실제 복원 검증을 대신하지 않는다. macOS에서는 `sha256sum` 대신 `shasum -a 256`을 사용할 수 있다. 별도 저장소 전송 후에도 체크섬을 확인하고, 검증 전에는 직전 정상 백업을 삭제하지 않는다. 압축은 암호화가 아니다.

`pg_dump`는 쓰기가 진행 중이어도 한 시점의 일관된 백업을 만든다. 백업 후 최신 운영 DB와 행 수가 달라도 곧바로 손상이라고 판단하지 않는다. 전환 전 마지막 백업과 전체 값 비교 때는 해당 DB의 쓰기 생산자를 중지하고 비교가 끝날 때까지 유지한다. 정기 온라인 백업의 시점 이후 기록은 복원 파일에 없다. 더 짧은 손실 시간이 필요하면 WAL 보관과 PITR을 별도 설계·검증한다.

단일 DB dump에는 클러스터 역할·역할 비밀번호·tablespace·서버 설정·환경 파일·이미지·볼륨 구성이 포함되지 않는다. 복원 계정/권한·확장 모듈과 설정을 별도로 재현할 수 있어야 한다. 역할 복구 자료도 비공개로 보관한다. 서버의 실행 중 PGDATA 디렉토리를 단순 복사하는 방법으로 이 절차를 대체하지 않는다.

## 새 DB로 복원

운영 원본과 다른 DB·볼륨·접속 설정을 준비한다. 백업 시점의 코드와 호환되는 이미지, 필요한 확장 모듈·소유자·실행 계정을 먼저 준비한다. 처음부터 최신 migration을 적용해 테이블을 만들지 않는다. dump가 스키마와 버전을 함께 복원한다. 테스트 앱/작업자·외부 발송·미디어 요청도 복구 DB에 자동 실행하지 않는다.

관리자가 빈 DB를 `template0` 기준으로 만들고 encoding/locale·소유자·public 스키마 권한을 확인한다. 아래 `RESTORE_DATABASE`와 `RESTORE_OWNER`는 **새 대상의 비밀값 없는 이름**이다. libpq의 호스트와 관리 계정도 새 대상을 가리키도록 전환한 후 실행한다. 원본 DB 이름이나 접속 설정을 재사용하지 않는다.

```bash
# BACKUP_SET은 검증할 백업 폴더, 아래 변수들은 새 복원 대상을 선택한 값이다.
(cd "$BACKUP_SET" && sha256sum --check database.dump.sha256)
psql --no-password --dbname="$RESTORE_DATABASE" --set=ON_ERROR_STOP=1 \
  --command="SELECT current_database(), current_user;"
psql --no-password --dbname="$RESTORE_DATABASE" --set=ON_ERROR_STOP=1 \
  --command="SELECT schemaname, tablename FROM pg_tables WHERE schemaname NOT IN ('pg_catalog', 'information_schema');"
# 업무·Alembic 테이블이 없는 새 DB임을 확인한 뒤 실행한다.
pg_restore --no-password --dbname="$RESTORE_DATABASE" --role="$RESTORE_OWNER" \
  --single-transaction --exit-on-error --no-owner --no-privileges \
  "$BACKUP_SET/database.dump"
```

복원은 오류를 무시하지 않고 한 트랜잭션으로 실행한다. 실패하면 접속·DB·테이블 상태와 원인을 확인하며, 운영 데이터를 자동 삭제하거나 `--clean/--create`로 덮어쓰지 않는다. 병렬 복원은 이 절차에서 사용하지 않는다.

`--no-owner --no-privileges`는 이전 소유권/GRANT를 재적용하지 않으므로 준비한 복원 소유자에 객체를 만들고 검토한 실행 계정 권한을 다시 부여해야 한다. 권한 복원이 빠지면 데이터가 있어도 앱은 동작하지 않는다. 백업은 신뢰한 원본에서만 복원하고 운영 데이터가 포함된 복구 시험 DB도 외부에 공개하지 않는다.

복원 뒤의 완료 기준:

- 백업 시점의 앱 이미지와 **새 DB**의 비공개 `DATABASE_URL`로 `python -m src.app.cli.db check` 통과. uv 관리 환경이면 앞에 `uv run`을 붙인다. 스키마 확인 전 `upgrade`로 백업 상태를 바꾸지 않는다.
- 업무 테이블 16개와 Alembic 버전, 주요 인덱스·FK·제약 존재 확인. 쓰기를 멈춘 전환 시험이라면 보존한 원본과 전 컬럼 값·행 수 비교도 끝낸다. SQLite 이전 직후의 PG 백업은 [transfer verify](DB_TRANSFER.md)로 이전용 SQLite와 비교할 수 있다.
- 계정/설정/이력/개인 읽음 조회, 기존 세션과 로그인, 일반 실행 계정의 새 기록 저장, 다음 자동 ID 확인. 시험 쓰기는 복원 전용 DB에서 수행한다.
- 격리 환경의 복구 시간·백업 시각·체크섬·결과를 비공개 기록으로 남긴다. 세션·연락처·원문·DB 오류 전문을 공개 CI artifact에 올리지 않는다.

운영 장애 때는 원본 볼륨과 로그를 보존하고, 새 DB 복원이 위 검사를 통과한 뒤 호환 이미지와 연결 설정으로 전환한다. 백업 이후의 기록 손실 범위를 확인한다. 전환 전에 새 DB 쓰기를 시작하지 않으며, 전환 이후 새 기록이 생기면 예전 SQLite/PG 주소로 단순 복귀하지 않는다.

## SQLite 이전 전 원본 보존

SQLite에서 이전할 때는 [DB_TRANSFER.md](DB_TRANSFER.md)의 쓰기 중지·원본 보존을 먼저 수행한다. 백엔드뿐 아니라 같은 파일을 쓰는 모든 작업자를 중지한 다음 SQLite backup API 또는 CLI의 `.backup`으로 일관된 스냅샷을 만든다. WAL 모드의 `.db` 파일만 `cp`하면 WAL에 남은 최신 기록이 빠질 수 있다.

복사본을 별도 경로에 열어 `PRAGMA integrity_check`가 `ok`, `PRAGMA foreign_key_check` 결과가 비어 있는지 확인하고 계정·설정·최신 측정/행 수를 원본과 대조한다. `.backup`은 WAL의 커밋된 내용을 포함하는 DB 스냅샷이므로 이 결과를 복원 시험에 사용한다. 실제 운영 원본은 변경하지 않는다. 최종 스냅샷 이후에는 이전과 확인이 끝날 때까지 원본 쓰기를 다시 시작하지 않는다.

원본 보존 백업과 이전용 복사본을 분리한다. 기존 DB의 Alembic 등록은 이전용 복사본에만 적용하며, 이전 원본/PG dump/환경 설정을 각각 비공개로 보존한다. SQLite 온라인 백업·실제 운영 파일 복원 시험은 이 PostgreSQL 합성 검증과 별개다.

근거: PostgreSQL 17의 [pg_dump](https://www.postgresql.org/docs/17/app-pgdump.html), [pg_restore](https://www.postgresql.org/docs/17/app-pgrestore.html), [SQL dump 백업](https://www.postgresql.org/docs/17/backup-dump.html), SQLite의 [온라인 백업 API](https://www.sqlite.org/backup.html).

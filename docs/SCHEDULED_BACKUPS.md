# 정기 백업과 SQLite 원본 보존

PostgreSQL 정기 백업은 `docker-compose.postgresql.yml`에 `docker-compose.backup.yml`을 함께 사용한다. 백업 전용 조회 계정과 별도로 준비한 저장 볼륨을 사용하며 웹 API나 백엔드 작업자에서 실행하지 않는다. SQLite 원본은 별도의 관리 CLI로 백업한다.

## PostgreSQL 정기 백업

백업 서비스는 시작 직후 한 번 실행하고, 각 실행이 끝난 뒤 설정한 간격만큼 기다린다. 기본 간격은 86400초이며 매일 특정 UTC 시각에 실행하는 cron은 아니다. 호스트가 꺼져 있으면 실행되지 않고 다시 시작할 때 즉시 백업한다.

| 설정 | 기본값/준비 내용 |
| --- | --- |
| `DB_BACKUP_PASSWORD` | scrap_backup의 실제 비밀번호. DB_BACKUP_SCRAM 생성 때 사용한 값과 동일하게 비공개 실행 환경에 주입 |
| `POSTGRES_BACKUP_VOLUME` | 미리 준비한 외부 Docker 볼륨 이름. 자동 생성하지 않음 |
| `BACKUP_INTERVAL_SECONDS` | 86400, 범위 1..604800초. 짧은 값은 로컬 시험용 |
| `BACKUP_RETAIN_COUNT` | 최근 성공본 7개, 범위 2..365개. 보관 일수가 아니라 개수 |
| `BACKUP_TIMEOUT_SECONDS` | 백업 명령 최대 300초, 범위 1..3600초 |

나머지 이미지·관리/실행 자격 증명·앱 파일은 [POSTGRESQL_DEPLOYMENT.md](POSTGRESQL_DEPLOYMENT.md)와 동일하다. backup 컨테이너에는 조회 계정의 접속 비밀번호만 전달하며 DB 관리자/owner 자격 증명이나 Docker 소켓을 주지 않는다. 클라이언트 비밀번호는 환경에서 읽고 명령행 인자·새 평문 파일·로그에 쓰지 않는다. Docker 관리자가 환경을 조회할 수 있으므로 관리 권한을 제한한다.

백업 볼륨은 운영 DB와 별도로 보관하고 실제 mount·용량·접근 권한·암호화를 확인한다. 별도 물리 저장소나 외부 저장소를 쓰려면 해당 드라이버/호스트 mount를 먼저 구성하고 그 볼륨 이름을 선택한다. 아래 `docker volume create`의 기본 local 볼륨은 로컬 시험용이며 운영 외부 저장소 설정을 대신하지 않는다. 실제 저장소 선택·원격 전송·암호화 키 보관은 배포 환경에서 확정한다.

```bash
# 로컬 시험용 저장소. 이름은 별도의 비공개 실행 설정에서 선택한다.
docker volume create "$POSTGRES_BACKUP_VOLUME"

# 기존 배포와 같은 프로젝트 이름·이미지·비공개 설정을 사용한다.
docker compose --env-file /dev/null -p scrap-postgresql \
  -f docker-compose.postgresql.yml -f docker-compose.backup.yml config --quiet
docker compose --env-file /dev/null -p scrap-postgresql \
  -f docker-compose.postgresql.yml -f docker-compose.backup.yml up -d --wait backup
```

DB 구조·권한 적용을 먼저 완료한다. 백업 작업은 Alembic 버전 테이블의 등록을 검사하여 구조 적용 전의 빈 DB를 거절한다. backup은 별도 profile이므로 기본 PostgreSQL Compose만 실행해도 자동 활성화되지 않는다. 한 개의 scheduler를 사용한다. 컨테이너는 CPU 0.5개·메모리 256MiB와 읽기 전용 파일시스템을 사용한다. 진단 통로용 `/tmp`는 16MiB, 데이터는 `/backups`에만 쓴다.

처음에 비어 있는 저장 볼륨만 초기화하여 postgres 사용자 소유·0700으로 만들고, 이후 백업은 그 사용자로 실행한다. 초기화 표시가 없는 비어 있지 않은 볼륨은 거절한다. 기존 파일 전체의 소유권을 자동 변경하지 않는다. 운영 볼륨의 데이터를 자동 삭제해서 초기화를 재시도하지 않는다.

## 성공·실패와 보관

각 실행은 다음 순서로 처리한다.

1. 볼륨의 `flock` 잠금을 얻는다. 다른 실행이 있으면 종료 코드 75와 `BACKUP_SKIPPED`를 남기고 기존 상태를 변경하지 않는다. 저장소가 flock을 지원해야 하며 실제 mount에서도 확인한다.
2. `.partial.*` 디렉토리에 custom-format `pg_dump`를 만든다. 명령 실패·시간 초과와 stderr 진단/경고가 있으면 성공 처리하지 않는다. 진단 원문은 FIFO로 읽어 버리고 로그에는 고정된 실패 단계만 남긴다.
3. `pg_restore --list`와 SHA-256 검사를 수행하고 디스크 동기화 후 완료 디렉토리로 옮긴다. 파일 권한은 0600, 디렉토리는 0700이다.
4. 볼륨에 저장한 단조 증가 번호로 성공본을 정렬해 최근 설정 개수만 보관한다. 같은 초의 실행이나 시계 변경 때문에 방금 만든 백업이 먼저 삭제되지 않는다. 정해진 이름과 형식 표시가 있는 완료 디렉토리만 정리한다.
5. 마지막 성공 시각과 상태를 기록한다. 실패 실행에서는 이전 성공본을 정리하지 않는다.

완료 디렉토리에는 `database.dump`, `database.dump.sha256`, `completed-at`, `format`이 있다. dump에는 계정 해시·세션·연락처·장치 데이터가 있으므로 Git·공개 CI artifact에 올리지 않는다. 압축된 dump를 암호화된 파일로 취급하지 않는다. 실패/중단된 `.partial.*`는 복구용 성공본이 아니다. SIGKILL·호스트 장애로 남은 partial은 대상 확인 후 별도로 정리한다.

`BACKUP_OK`는 archive 생성·목록·checksum 검사의 성공이다. 운영 데이터의 전체 복원 시험은 [DB_BACKUP_RECOVERY.md](DB_BACKUP_RECOVERY.md)에 따라 별도로 주기 실행하고 실제 복구 시간을 측정한다. 실패 후 무조건 원본 DB를 지우거나 backup을 원본에 덮어쓰지 않는다.

```bash
# 수동 한 번 실행. scheduler와 겹치면 잠금으로 차단된다.
docker compose --env-file /dev/null -p scrap-postgresql \
  -f docker-compose.postgresql.yml -f docker-compose.backup.yml run --rm backup once

# 실패 상태·너무 오래된 성공본이면 비정상 종료한다.
docker compose --env-file /dev/null -p scrap-postgresql \
  -f docker-compose.postgresql.yml -f docker-compose.backup.yml run --rm backup health
```

Docker healthcheck도 같은 검사를 수행한다. 실패 상태, 최초 성공 없음, 마지막 성공이 간격+timeout+60초보다 오래됨, 실행 상태가 timeout+30초를 넘으면 비정상이다. 정상 실행 중에는 직전 성공의 유효 기간 안에서 상태를 유지한다. 오류는 `BACKUP_FAILED stage=...`로 기록하고 다음 성공 시 상태를 복구한다. 실제 이메일/SMS 발송은 포함하지 않으므로 운영에서는 unhealthy·실패 로그·디스크 부족을 외부 감시에 연결해야 한다. 이 감시의 실제 알림 도착도 배포 때 확인한다.

## SQLite 원본 백업

최종 이전에서는 원본 DB에 쓰는 백엔드와 모든 작업자를 먼저 정지한다. 원본 보존 백업을 만든 뒤 별도의 이전용 복사본을 사용하고, 그 복사본에만 Alembic 등록/이전을 수행한다. 아래 CLI는 실행 중 DB도 일관된 snapshot으로 복사하지만 최종 이전의 쓰기 중지 절차를 대신하지 않는다.

```bash
# 부모 디렉토리는 먼저 비공개로 준비한다. 기존 output은 덮어쓰지 않는다.
uv run python -m src.app.cli.backup \
  --source ./data/original.db --output ./data/backups/original-preserved.db
```

런타임 이미지에서는 `python -m src.app.cli.backup`을 사용한다. 관리 컨테이너에 원본 읽기 경로와 별도 출력 경로만 연결하고 실행 UID가 읽기/쓰기 가능한지 확인한다. WAL 읽기에 필요한 디렉토리와 기존 WAL/SHM 접근도 함께 확인하며 임의 `immutable=1`로 이를 우회하지 않는다. 생성한 비밀번호나 DB URL을 파일 접근 문제 해결용 임시 파일에 쓰지 않는다.

CLI는 Python SQLite backup API로 커밋된 WAL 내용까지 복사하고, integrity_check·foreign_key_check 후 standalone DB를 0600으로 공개한다. source는 읽기 전용으로 열며, 대상이 이미 있으면 거절한다. 부모 폴더와 같은 파일시스템에서 하드 링크로 덮어쓰기 없이 공개하므로 해당 저장소의 hard-link 지원이 필요하다. 기본 제한은 30초이며 `--timeout-seconds`로 최대 3600초까지 조정한다. 큰 DB에서는 시간/공간 예산을 먼저 확인한다.

버전 없는 legacy DB도 그대로 백업하며 스키마를 자동 등록하지 않는다. 손상/FK 오류/시간 초과 때 성공 파일을 공개하지 않고 원본은 보존한다. 파일 공개 직후 프로세스나 저장소 오류로 성공 여부가 불확실하면 이미 만들어진 파일을 검사한 후 다시 시도한다. 기존 파일을 자동 삭제하지 않는다. 복원은 원본과 다른 경로에서 먼저 검사하고 원본 데이터와 계정·설정·최근 기록을 확인한다.

## 로컬 검증

```bash
uv run pytest -v
docker build -t scrap-backend-backup-check:local .
uv run python -m tests.integration.verify_scheduled_backups --image scrap-backend-backup-check:local
```

SQLite 시험은 열린 WAL의 최신 커밋 보존·원본 바이트 불변·단독 파일 복원·덮어쓰기 차단·손상/FK 오류·잠금 시간 제한·legacy 보존을 확인한다. 실제 Docker 시험은 읽기 전용 계정 백업, 성공본 보관 개수, 중복 실행, 실패/경고 시 정상본 보존·health 실패와 복구, scheduler 반복 실행, 실제 dump의 별도 DB 복원 후 16개 테이블 전 컬럼 비교까지 수행한다. 테스트 비밀번호는 메모리에만 유지하며 임시 프로젝트·볼륨은 제거한다.

근거: [PostgreSQL pg_dump](https://www.postgresql.org/docs/17/app-pgdump.html), [SQLite backup API](https://www.sqlite.org/backup.html), [Python 3.13 Connection.backup](https://docs.python.org/3.13/library/sqlite3.html#sqlite3.Connection.backup).

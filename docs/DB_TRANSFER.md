# SQLite 데이터의 PostgreSQL 이전

백엔드의 계정·설정·라이다 원문·알림·수거·이벤트 클립 작업 기록 등 업무 테이블 16개의 데이터를 빈 PostgreSQL로 옮기는 일회성 관리 명령이다. 영상 파일과 날짜별 녹화 카탈로그는 미디어 서비스 소유이므로 이 도구로 옮기지 않는다. 기존 PostgreSQL에 데이터를 합치거나 덮어쓰는 기능은 없다.

이전 명령은 웹 API로 제공하지 않는다. 대상 DB 주소는 비공개 환경 설정의 `DATABASE_URL`에서만 읽으며 비밀번호나 URL을 명령행 인자로 받지 않는다. 명령 출력은 테이블별 행 수와 결과이고 원문·비밀번호 해시·세션 값·연락처는 출력하지 않는다.

## 사전 준비

1. 백엔드와 해당 DB에 쓰는 모든 작업자를 중지한다. 최종 백업 이후에는 이전 완료까지 원본에 새 데이터를 받지 않는다.
2. SQLite 백업 API 또는 검증된 백업 절차로 일관된 백업을 만든다. WAL 모드에서 실행 중인 `.db` 파일만 복사하면 최신 기록이 빠질 수 있으므로 단순 파일 복사로 대체하지 않는다. 원본과 복원용 백업은 보존하고, 아래 작업에는 별도 이전용 복사본을 사용한다.
3. 이전용 SQLite 복사본과 대상 PostgreSQL 모두 현재 Alembic 버전이어야 한다. [DB_MIGRATIONS.md](DB_MIGRATIONS.md)에 따라 먼저 구조를 준비한다. 이전 명령이 원본 구조를 자동 수정하거나 legacy DB를 등록하지는 않는다.
4. PostgreSQL은 백엔드 전용 DB로 준비하고, 애플리케이션 최초 실행·계정 초기화를 하지 않은 빈 상태를 유지한다. 기존 기본 계정이나 피트 설정이 한 행이라도 있으면 이전은 거절된다. `alembic_version` 행은 허용한다.
5. 이전용 계정은 테이블 조회·삽입·잠금과 시퀀스 변경 권한이 필요하다. 일반 애플리케이션 계정에 상시 관리 권한을 추가하는 방식으로 해결하지 않는다. 기존 DB와 새 DB의 파일·볼륨·주소를 혼동하지 않도록 확인한다.

버전 없는 SQLite 복사본은 기존 원본을 보존한 상태에서 다음처럼 등록한다. 아래 경로는 합성 예시이며 실제로 준비한 복사본 경로를 사용한다. 이 작업은 복사본의 구조를 변경하므로 원본에 실행하지 않는다.

```bash
APP_ENV=development DATABASE_URL=sqlite+aiosqlite:///./data/migration/source.db \
  uv run python -m src.app.cli.db upgrade --adopt-legacy
APP_ENV=development DATABASE_URL=sqlite+aiosqlite:///./data/migration/source.db \
  uv run python -m src.app.cli.db check
```

현재 구조와 다른 컬럼·키·제약은 별도의 검토된 migration이 필요하다. 과거 공통 알림 읽음 값에는 사용자가 없으므로 개인별 읽음으로 임의 복제하지 않는다. 기존 정책이 없던 피트의 기본 정책 생성은 Alembic 등록 단계의 동작이며 이전 도구가 사용자 설정을 새로 추정하는 것이 아니다.

이후 환경 설정은 **대상 PostgreSQL**을 가리키도록 준비하고 빈 DB 구조만 적용한다. 비밀번호가 들어간 URL을 터미널 명령이나 문서에 붙이지 않는다.

```bash
uv run python -m src.app.cli.db upgrade
uv run python -m src.app.cli.db check
```

## 시험 이전·저장·확인

먼저 시험 이전한다. 대상 DB에 실제 삽입해 PostgreSQL 제약과 비교 검사를 거친 뒤 전체 롤백한다. `check`를 실행할 때도 대상 서비스는 정지 상태여야 한다.

```bash
uv run python -m src.app.cli.transfer check --source ./data/migration/source.db
```

문제가 없으면 명시적으로 저장한다.

```bash
uv run python -m src.app.cli.transfer apply --source ./data/migration/source.db
uv run python -m src.app.cli.transfer verify --source ./data/migration/source.db
```

`apply`는 모든 테이블을 비교하고 시퀀스를 조정한 뒤 한 번만 커밋한다. 성공 후 다시 `apply`하거나 `check`하면 대상이 비어 있지 않아 거절된다. 재확인은 읽기만 하는 `verify`를 사용한다. 실패한 데이터를 무시하거나 중복 삽입을 건너뛰는 옵션은 없다.

기본 배치는 50행이고 `--batch-size 1..500`으로 조정할 수 있다. 큰 라이다 JSON이 많은 환경에서는 작은 배치를 사용한다. 전체 DB를 Python 리스트 하나에 담지 않지만 단일 행의 JSON 크기와 DB 정렬·검사 비용은 남는다. 시험 이전에서도 전체 데이터를 읽고 써 보므로 가벼운 연결 확인 명령이 아니다.

원본은 URI `mode=ro`로 열고 명시적 read transaction으로 동일한 스냅샷을 유지한다. SQLite 구조·quick_check·외래 키를 검사하고, 대상은 구조 확인 후 전체 업무 테이블과 버전 테이블을 한 트랜잭션 동안 잠근다. 잠금을 5초 안에 얻지 못하면 실패한다. 잠금이 작업 중인 원본의 최종 백업·쓰기 중지 절차를 대신하지 않는다.

## 보존·비교 기준

- 모든 컬럼을 복사하며 ID, 외래 키, 계정 해시·세션 토큰 해시·CSRF·만료, 정책 버전과 개인별 읽음을 유지한다. 비밀번호를 다시 해시하거나 세션을 재발급하지 않는다.
- 라이다 JSON·큰 정수·단위·판정값·결측과 기존 v1 기록을 그대로 옮긴다. 점 배열에서 새 적재율을 계산하지 않는다.
- SQLite의 애플리케이션 UTC datetime은 UTC로 해석하고 PostgreSQL timezone-aware 값으로 저장한다. SQLite에서 tzinfo가 사라진 시각을 로컬 시간으로 해석하지 않는다. UTC가 아닌 의미로 저장된 외부 데이터는 사전에 별도 정리해야 한다.
- PK로 정렬한 모든 행·컬럼을 정규화한 SHA-256과 행 수를 테이블마다 비교한다. 문자열 PK는 두 DB의 정렬 차이를 피하도록 명시적인 이진/C 정렬을 사용한다. JSON 객체의 키 순서는 무시하고 배열 순서와 값은 유지한다.
- nullable JSON의 SQL NULL과 JSON literal null을 구분해 보존한다. 유한하지 않은 수치와 비교할 수 없는 값은 거절한다.
- 모든 비교가 끝나면 자동 ID 시퀀스를 현재 최대 ID 다음 값으로 바꾼다. 트랜잭션과 함께 롤백되는 `ALTER SEQUENCE ... RESTART`를 사용한다. ID 범위를 초과하는 데이터 등으로 변경이 실패하면 이전도 실패한다.

이 도구는 현장 데이터의 의미나 모든 API enum을 재판정하는 정제 도구가 아니다. 현재 구조·외래 키·PostgreSQL 제약과 보존 비교를 검증하며, 기존 데이터의 업무 의미 검토는 별도다. JSON은 의미와 값의 보존 기준이며 공백·키 순서와 같은 직렬화 문자열을 보존하는 것은 아니다.

## 실패·재시도와 전환

입력·삽입·제약·비교 실패 시 대상의 부분 데이터와 시퀀스 변경을 롤백한다. 원본 SQLite는 수정하지 않는다. 오류 응답에는 SQL 파라미터나 자격 증명을 포함하지 않는다. 원본 FK가 잘못된 경우 행을 삭제해 통과시키지 말고 원인을 검토한다.

연결이 끊기거나 프로세스가 커밋 직후 종료되면 성공 여부가 불확실할 수 있다. 이때 즉시 대상 데이터를 지우거나 재적용하지 않는다. 대상 구조와 행 수를 확인하고, 아직 백엔드를 시작하지 않은 상태에서 `verify`로 원본과 일치하는지 확인한다. 비어 있는 대상에만 재적용한다. 원인 확인 없이 테이블을 비우는 자동 복구는 제공하지 않는다.

최종 `verify` 이후 PostgreSQL을 사용하는 새 백엔드를 시작하고 계정·설정·이력·수신을 확인한다. 백엔드를 시작하면 세션 발급·정책 초기화·새 측정 저장 등으로 데이터가 변하므로 이후에는 원본과 전체 일치한다고 기대하지 않는다. 전환 후 PostgreSQL에 새 기록이 생긴 상태에서 이전 SQLite로 돌아가면 새 기록이 사라질 수 있으므로 단순 주소 변경을 복구 절차로 삼지 않는다.

이 문서와 도구만으로 운영 이전이 완료되지는 않는다. PostgreSQL의 로컬 백업·복원 검증과 운영 복구 절차는 [DB_BACKUP_RECOVERY.md](DB_BACKUP_RECOVERY.md)를 따른다. 실제 서버 데이터 이전·복원 시험, 정기 백업, 데이터 보관·용량, 권한·볼륨·배포 적용은 별도 검증 대상이다.

## 검증

```bash
uv run pytest -v
uv run python -m tests.integration.verify_db_transfer
```

Docker 검증은 로컬의 임시 PostgreSQL 17과 합성 SQLite만 사용한다. 16개 테이블·모든 필드 비교, 시험 롤백, 성공 저장, 재적용 거부, 같은 행 수의 값 변경 감지, 계정 로그인·기존 세션·읽음 유지, 다음 ID, 뒤쪽 테이블 삽입 실패·조용한 값 변경·시퀀스 변경 후 실패의 전체 롤백, 원본 FK 오류·버전 미등록 거절과 원본 파일 불변을 확인한다. 임시 컨테이너·볼륨·소스 파일은 종료 시 제거한다. GitHub CI에서도 같은 명령을 실행한다.

근거: [SQLite URI 읽기 전용](https://www.sqlite.org/uri.html), [SQLite read transaction](https://www.sqlite.org/lang_transaction.html), [PostgreSQL ALTER SEQUENCE의 트랜잭션 동작](https://www.postgresql.org/docs/17/sql-altersequence.html).

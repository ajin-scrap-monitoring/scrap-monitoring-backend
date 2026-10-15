# DB 마이그레이션

DB 구조를 Alembic 버전으로 관리한다. 초기 버전은 `0001_initial`이며 현재 16개 테이블의 고정 스키마를 담는다. 버전은 DB의 alembic_version에 기록한다. 이후 모델이 바뀌면 새 변경 파일을 추가한다. 이미 적용된 초기 버전과 baseline.py를 최신 모델에 맞춰 다시 쓰지 않는다.

이번 작업은 스키마 버전 관리다. SQLite 데이터를 PostgreSQL로 옮기는 도구·운영 백업 스케줄·전체 현장 배포를 완료한 것은 아니다.

## 실행 명령

올바른 비공개 환경 설정의 DATABASE_URL을 사용한다. 비밀번호가 포함된 URL을 명령행 인자로 전달하지 않는다.

```bash
# 현재 버전 조회 / 최신 버전과 실제 테이블 구조 확인
uv run python -m src.app.cli.db current
uv run python -m src.app.cli.db check

# 새 DB 또는 이미 버전이 있는 DB에 최신 변경 적용
uv run python -m src.app.cli.db upgrade
```

Docker 런타임에는 uv 대신 이미지 안의 Python을 사용한다. 운영 DB가 준비되지 않아 백엔드가 아직 실행되지 않는 경우 일회성 컨테이너로 먼저 적용한다.

```bash
sudo docker compose run --rm --entrypoint python backend -m src.app.cli.db upgrade
sudo docker compose run --rm --entrypoint python backend -m src.app.cli.db check
```

실행 중인 테스트/운영 DB를 변경할 때는 해당 DB를 사용하는 백엔드와 쓰기 작업자를 먼저 중지한다. 마이그레이션 실행자는 하나만 사용한다. 공용 서버의 다른 서비스까지 중지하지 않는다. 실제 배포 디렉토리와 Compose 파일·환경 파일이 다르면 해당 배포 구성을 사용한다.

## 기존 DB의 최초 등록

create_all로 생성된 기존 DB에는 Alembic 버전이 없다. 자동으로 정상 구조라고 가정하거나 번호만 붙이지 않는다.

1. 해당 DB의 쓰기 작업을 중지하고 백업한다. 백업의 복구 가능 여부를 확인한다.
2. DB URL과 볼륨이 의도한 기존 DB를 가리키는지 확인한다.
3. 다음 명령으로 최초 등록과 부족한 테이블 추가를 함께 수행한다.
4. check를 통과한 뒤 백엔드를 실행하고 기존 기록·설정·계정을 조회한다.

```bash
uv run python -m src.app.cli.db upgrade --adopt-legacy
uv run python -m src.app.cli.db check

# Docker 배포의 기존 DB
sudo docker compose run --rm --entrypoint python backend -m src.app.cli.db upgrade --adopt-legacy
```

--adopt-legacy가 없으면 기존 미등록 DB의 변경을 거절한다. 이 옵션은 백업 자체를 만들지 않는다. 새 테이블이 누락된 기존 구조는 등록할 수 있지만 이미 있는 테이블의 컬럼·논리적 타입/문자열 길이·nullable·PK·고유 제약·FK·이름 있는 CHECK·인덱스 정의를 검사한다. 누락된 일반 인덱스는 추가한다. 모르는 테이블, 컬럼 차이, 고유 제약/FK 누락 등은 중단한다. 이름 있는 CHECK의 존재를 확인하는 것으로 임의 변경된 CHECK 식의 의미까지 검증하는 것은 아니다.

구조가 다른 DB를 강제로 stamp하거나 컬럼을 자동 삭제하지 않는다. 오류가 나면 실제 구조를 확인하고 필요한 버전별 변경을 검토한다. 기존 행의 업무 의미나 모든 참조 데이터의 완전성은 별도 데이터 이전 검증 대상이다.

초기 버전은 기존 피트의 임계치·깊이·이름과 모든 업무 행을 보존한다. 정책 행이 없는 피트에는 기존 계약의 기본 정책만 추가하며, 이미 저장된 알림 정책과 개인 읽음 기록은 유지한다. 과거의 공통 읽음 기록을 특정 사용자에게 옮기지 않는다. 같은 명령을 다시 실행해도 데이터를 초기화하지 않는다.

## 앱 시작 동작

- development/test의 완전히 빈 DB만 최초 스키마를 자동 생성한다.
- production은 새 DB라도 명시적 upgrade가 먼저 필요하다.
- 어떤 모드에서도 기존 미등록 DB를 앱 시작만으로 등록하지 않는다.
- 버전이 오래됐거나 알 수 없는 버전, 테이블/필수 인덱스 등이 부족한 경우 시작을 중단한다. 최신 이미지로 upgrade/check를 수행한 뒤 실행한다.
- 최초 관리자 계정 생성은 스키마 준비 후 기존 [계정 초기화 절차](AUTH_STORAGE.md)를 따른다. DB 변경 명령 자체는 사용자를 생성하거나 비밀번호를 바꾸지 않는다.

이 변경을 반영한 이미지는 기존 미등록 DB에 바로 시작시키면 실패한다. 이미지·환경 설정·백업을 준비하고 일회성 DB 등록 명령을 먼저 실행해야 한다. 서버에 새 이미지를 빌드하거나 배포하는 작업은 이번 구현에 포함하지 않는다.

## 다음 스키마 변경

모델 수정 후 격리된 개발 DB를 현재 버전으로 맞추고 아래 명령으로 초안을 만든다.

```bash
uv run alembic revision --autogenerate -m "describe schema change"
uv run python -m src.app.cli.db upgrade
uv run alembic check
```

자동 생성 파일은 검토 전 적용하지 않는다. 컬럼 추가/이름 변경·기존 값 채우기·고유 제약·FK·인덱스·기존 데이터 영향은 직접 확인한다. DB 구조 변경을 위해 과거 migration 파일을 고치거나 create_all로 대체하지 않는다. 온라인 DB 검사만 지원하며, 초기 등록용 오프라인 SQL 생성은 지원하지 않는다.

SQLite와 PostgreSQL의 DDL 트랜잭션 동작은 다르므로 실패 시 모든 테이블 변경이 자동 복구됐다고 가정하지 않는다. 실패 후 버전·구조를 확인하고 재실행 또는 백업 복원을 결정한다. 초기 버전의 자동 downgrade는 업무 테이블 삭제를 막기 위해 거절한다. 되돌리기는 검증한 백업과 호환되는 이미지로 복구하는 절차를 따른다.

## 검증

```bash
uv run pytest -v
# Docker 필요. 임시 PostgreSQL 17 DB 두 개를 사용하고 종료 시 제거한다.
uv run python -m tests.integration.verify_db_migrations
# 현재 소스로 빌드한 이미지에서 DB 버전·재시작·영상 경로도 확인한다.
uv run python tests/integration/verify_media_gateway.py --image <local-test-image>
```

단위/통합 테스트는 새 생성·구조 검사·재적용·기존 14/16개 테이블 등록, 라이다 원문·계정/세션·정책·읽음 보존, 다음 SQLite ID, 구조 불일치·알 수 없는 버전·파괴적 downgrade 차단, CLI 동작과 비밀값 비노출을 검증한다. 실제 PostgreSQL에서는 새 DB와 기존 구조의 등록·JSONB 정책 초기화·모델 일치를 확인한다. 테스트용 DB 비밀번호는 메모리에만 유지하고 파일에 저장하지 않는다. Docker에는 PostgreSQL용 SCRAM-SHA-256 검증값만 전달하며, DB의 해시 저장과 잘못된 비밀번호 접속 거부도 확인한다.

구현 방식은 Alembic 공식 [Asyncio 연결 공유 안내](https://alembic.sqlalchemy.org/en/latest/cookbook.html#using-asyncio-with-alembic), [자동 생성과 검토 안내](https://alembic.sqlalchemy.org/en/latest/autogenerate.html)를 따른다.

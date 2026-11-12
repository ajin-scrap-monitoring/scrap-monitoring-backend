# 이미지 Release와 실행 안내

이 Repository는 Backend OCI 이미지와 설정 명세를 제공한다. 서버·HTTPS·네트워크·실제 설정값·DB
백업/이전·Gmail 파일 등록·전환 일정은 배포 Repository가 관리한다. GitHub Release는 서버 교체를 실행하지 않는다.

## 이미지와 공개 asset

| 항목 | 계약 |
| --- | --- |
| Registry | Public GHCR `ghcr.io/ajin-scrap-monitoring/scrap-monitoring-backend` |
| 플랫폼 | `linux/amd64` |
| 버전 | Git `vMAJOR.MINOR.PATCH`에서 `MAJOR.MINOR.PATCH` 이미지 tag 생성 |
| 소스 | `sha-<full-git-sha>` 이미지 tag와 OCI revision/source/version label |
| 배포 참조 | `image-reference.txt`의 `ghcr.io/...@sha256:<digest>` 사용 |
| mutable 기본 tag | `latest` 이미지는 게시하지 않음 |
| 검증 범위 | 동일 소스 main CI 성공, 이미지 격리 시작·readiness·로그인·인증 요구 확인, 런타임 의존성 고지 수집 |

Release asset은 `env.example`, `CONFIGURATION.md`, `RELEASING.md`, `settings.schema.json`,
`image-reference.txt`, `runtime-dependencies.json`, `SHA256SUMS`다. 실제 `.env`, OAuth 인증 파일,
현장 데이터, 서버별 Compose와 비공개 파일은 포함하지 않는다. 전체 설정과 조건부 필수 항목은
[CONFIGURATION.md](CONFIGURATION.md)에 있다. `runtime-dependencies.json`에는 이미지에 설치된
직접·전이 Python 패키지의 버전·라이선스 메타데이터, 보존된 wheel LICENSE/NOTICE와 base-image 저작권 고지가 있다.
원래 고지 파일도 이미지 내부에 유지한다. 프로젝트 자체에는 별도 라이선스를 부여하지 않는다.

## 게시 순서

저장소의 `.env.example`은 Release에서 `env.example`로 제공한다. 내려받은 예시는 배포 담당자의
비공개 설정 경로로 복사하여 사용한다. 숨김 파일명은 GitHub 업로드 중 바뀔 수 있으므로 asset과
체크섬 목록에는 동일한 공개 파일명 `env.example`을 사용한다.

1. 변경은 PR로 main에 병합한다. 코드·문서·검증을 완료하고 `pyproject.toml`의 `project.version`을
   게시할 버전으로 정한다. 버전 변경 시 `uv lock`으로 lock의 프로젝트 메타데이터도 맞춘다.
2. 대상 커밋의 main CI 성공과 CodeQL/보호 규칙을 확인한다. tag는 원격 main 이력 안의 검증한 커밋에만 만든다.
3. 다음 명령의 자리표시를 실제 확정 버전·커밋으로 바꿔 annotated tag를 push한다. 자동으로 main 최신을
   선택하거나 소스에 관계없이 tag만 바꾸지 않는다.

   ```bash
   git tag -a vX.Y.Z <verified-main-commit> -m "Release vX.Y.Z"
   git push origin refs/tags/vX.Y.Z
   ```

4. `.github/workflows/release.yml`이 tag 형식·프로젝트 버전·main 이력·동일 커밋 main CI를 검사한다.
   linux/amd64 이미지를 한 번 빌드하고, 네트워크 없는 임시 컨테이너·합성 계정·임시 SQLite로
   시작/인증/readiness를 확인한 뒤 같은 로컬 이미지를 GHCR에 push한다. 이 시험은 외부 서버·DB·Gmail을 쓰지 않는다.
5. 최초 GHCR Package는 Repository와 별도로 Public 설정이 필요할 수 있다. 관리자가 Backend package의
   Visibility를 Public으로 맞춘다. workflow는 로그인 정보 없는 manifest 조회가 성공해야 GitHub Release를 게시한다.
   이 검사에서 중단됐다면 Public 설정 후 workflow를 재실행한다. 기존에 게시된 GitHub Release가 있으면 재실행은 중단한다.
6. asset을 모두 draft Release에 첨부한 뒤 실제 다운로드 파일의 체크섬을 검사하고 마지막에 게시한다.
   다운로드 이름이나 내용이 목록과 다르면 draft 상태에서 중단한다. workflow summary와 `image-reference.txt`의
   digest 및 두 이미지 tag, Public 접근을 확인한다. 배포 Repository는 digest를 선택한다.

실제 version tag push·이미지 게시·GitHub Release 게시를 해야 공식 산출물 제공이 완료된다.
로컬 이미지 빌드나 main의 CI 통과만을 Release 완료로 기록하지 않는다. 게시된 Release와 tag/asset은
변경·삭제하지 않고 수정이 필요하면 새 버전으로 제공한다. 게시 전 중단된 draft 복구는 유지관리자가 상태를
확인한 뒤 처리하며, 자동화는 기존 Release를 덮어쓰지 않는다.

## 실행 기준

- **Backend 단일 인스턴스·Uvicorn worker 1개**, Python3.13. 중복 작업자 실행을 전제로 한 구성이 아니다.
- 운영 저장소는 PostgreSQL17이며 앱 계정과 migration/관리 계정을 구분한다. 이미지에는 DB·사용자 데이터가 없다.
- 기본 프로세스는 비root `appuser`, `/app` 작업 경로, `0.0.0.0:8000` 리스너다. 포트는 배포 설정에서 연결한다.
- `/healthz`는 프로세스 확인, `/api/v1/readyz`는 서비스 준비 확인이다. 컨테이너의 기본 HEALTHCHECK를 유지한다.
- 이미지의 최소 의존성·Python 앱 실행 소스와 lock은 배포에 필요한 내용이며, 테스트·개발 도구·호스트 `.env`를 복사하지 않는다.
- DB·로그/임시 경로·비밀 파일의 host mount, 권한과 네트워크는 배포 담당자가 정한다. Gmail 파일은
  `GMAIL_CREDENTIALS_FILE`의 컨테이너 경로로 읽기 전용 연결하며 내용은 공개 asset에 넣지 않는다.

새 설치·PostgreSQL 역할과 실행 템플릿은 [POSTGRESQL_DEPLOYMENT.md](POSTGRESQL_DEPLOYMENT.md),
계정 생성은 [AUTH_STORAGE.md](AUTH_STORAGE.md), 백업·복원은 [DB_BACKUP_RECOVERY.md](DB_BACKUP_RECOVERY.md)를 따른다.
개발/시연 시뮬레이션 기준을 `production`에 넣어 실행하지 않는다. 실제 실행 환경에 맞는 설정은 배포 담당자가 주입한다.

## 이미지 갱신과 DB migration

1. 선택한 digest와 호스트 아키텍처를 확인하고 현재 이미지·설정·secret mount·볼륨 참조와 복원 기준을 보관한다.
2. 기존 DB를 백업하고 선택한 이미지의 schema 요구사항을 확인한다. 기존 계정·피트·알림·발송/이벤트 이력을 보존한다.
3. 앱 시작 전에 같은 이미지의 관리용 일회성 컨테이너에서 **관리 계정**으로 실행한다.

   ```bash
   python -m src.app.cli.db current
   python -m src.app.cli.db upgrade
   python -m src.app.cli.db check
   ```

   기존 migration이 없는 DB는 원본 백업과 [DB_MIGRATIONS.md](DB_MIGRATIONS.md)의
   `--adopt-legacy` 검사 절차를 먼저 따른다. 일반 이미지 갱신에 이를 매번 적용하지 않는다.
4. migration/복원 후 PostgreSQL 템플릿의 runtime grants를 적용하고 앱은 **앱 역할 계정**으로 실행한다.
   관리 DB URL과 실행 시크릿은 비공개로 주입한다. 전체 환경·Compose·credential을 로그에 출력하지 않는다.
5. Backend 1개만 실행해 readiness·로그인·실제 수신·API·기존 작업자 상태를 확인한다. 브라우저·게이트웨이·미디어
   연결 검증은 각 담당자가 결합한다. 배포 설정이나 비밀 파일을 이미지 안에 넣어 재빌드하지 않는다.
6. 실패 시 schema가 이전 이미지와 호환되는지 먼저 확인한다. 호환되면 보관한 digest·설정으로 복원하고,
   비호환 schema는 검증한 백업/복원 계획을 따른다. DB를 지우거나 무조건 downgrade하여 해결하지 않는다.

이 명령은 이미지 내부 도구의 역할을 설명한다. 어떤 서버·Compose·볼륨·계정으로 실행할지는 배포 Repository가 정한다.

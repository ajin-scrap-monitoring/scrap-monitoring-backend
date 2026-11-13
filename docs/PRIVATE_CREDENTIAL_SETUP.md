# 비공개 Gmail 인증과 관리자 로그인 자료

이 문서의 주소·ID·비밀번호·토큰은 모두 형식을 설명하는 자리표시자다. 실제 값을 공개
Repository, 이미지, Release, 브라우저 정적 파일, 이슈와 로그에 넣지 않는다.
실제 자료는 계정 소유자가 승인한 담당자에게만 비공개 경로로 전달한다.

## 파일별 역할

아래 경로는 배포 담당자가 정할 수 있는 예시다. Backend가 파일명으로 계정을 자동 인식하거나
네 파일을 모두 읽는 것은 아니다. 설정 변수와 mount 경로가 실제 연결을 결정한다.

| 파일 예시 | 역할 | 실행 시 필요 여부 |
| --- | --- | --- |
| `private-access/gmail-oauth/authorized-user.json` | 계정이 승인한 OAuth 인증. client ID·client secret·refresh token 포함 | Gmail 활성 시 이 형식의 파일 필요 |
| `private-access/gmail-oauth/client-secret.json` | OAuth 인증 발급에 사용하는 클라이언트 정보 | 발급 담당자가 보관. Backend에 mount하지 않음 |
| `private-access/gmail-oauth/settings.env` | 발급·배포 담당자가 관리하는 비공개 Gmail 설정 | 담당 도구가 사용하는 경우에만 필요. Backend가 자동으로 읽지 않음 |
| `private-access/gmail-alert.env` | 컨테이너에 주입할 Gmail 실행 설정 | 파일명은 선택. 환경변수 주입 필요 |
| `private-access/admin-login.json`, `private-access/admin-login.txt` | 관리자 로그인 정보를 보관하는 인계 메모 | Backend 실행에 필요하지 않음 |

Gmail의 실제 인증·설정 파일은 **실제 Google 계정의 발송 권한과 비공개 계정 정보가 담긴
중요 자료**다. 파일 이름이 예시와 같더라도 내용은 샘플과 구분해 비공개로 관리한다.
관련 자료가 함께 있는 디렉토리를 통째로 전달하거나 배포하지 않는다.

## 실행용 OAuth JSON

Backend는 다음 필드를 읽는다. `token_uri`와 `scopes`는 아래 값만 허용한다.
scopes에 읽기·수정 권한 등을 추가한 인증파일도 거절한다.

```json
{
  "client_id": "example-client.apps.googleusercontent.com",
  "client_secret": "<OAUTH_CLIENT_SECRET>",
  "refresh_token": "<ACCOUNT_REFRESH_TOKEN>",
  "token_uri": "https://oauth2.googleapis.com/token",
  "scopes": ["https://www.googleapis.com/auth/gmail.send"]
}
```

다른 계정이 사용을 승인한 유효한 인증정보가 필요하다. Google API 키나 클라이언트 정보만으로
대체할 수 없고, 파일명만 같게 바꿔도 계정 승인이 생기지 않는다. 실제 발급·재승인과
Google 계정/OAuth 프로젝트 관리는 인증 담당자가 수행한다. 게시 상태와 계정 정책의
운영 조건은 [Gmail 운영 문서](GMAIL_EMAIL_DELIVERY.md)를 따른다.

다음은 인증 발급용 Desktop 클라이언트 파일의 형식 예시다. 이 파일 자체는 위 실행용
authorized-user 인증파일이 아니며, Backend에 직접 연결하지 않는다.

```json
{
  "installed": {
    "client_id": "example-client.apps.googleusercontent.com",
    "project_id": "example-project",
    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
    "token_uri": "https://oauth2.googleapis.com/token",
    "client_secret": "<OAUTH_CLIENT_SECRET>",
    "redirect_uris": ["http://localhost"]
  }
}
```

## 외부 설정과 서버 등록

`gmail-alert.env` 예시는 다음과 같다. 실제 계정 주소와 파일 경로는 배포 환경에서 지정한다.
`settings.env`에 같은 설정을 보관한다면 실제로 주입할 파일 하나를 정하고 중복 관리하지 않는다.

```dotenv
GMAIL_EMAIL_ENABLED=true
GMAIL_CREDENTIALS_FILE=/run/secrets/gmail-authorized-user.json
GMAIL_SENDER_EMAIL=sender@example.test
```

1. 계정 소유자와 기존 개인 계정 사용 또는 운영용 계정 전환을 결정한다.
2. 새 서버의 비공개 디렉토리에 필요한 실행용 인증파일만 등록한다. 기존 파일이 있으면
   교체 대상과 소유자를 먼저 확인하고 덮어쓰지 않는다. 디렉토리는 예를 들어 `0700`으로 관리한다.
3. 인증파일은 예를 들어 `0400` 또는 `0600`으로 제한한다. 그룹·다른 사용자 권한이 있으면
   Backend가 인증을 거절한다. 컨테이너의 실제 실행 UID가 파일을 읽을 수 있어야 하며,
   읽기 문제를 해결하려고 모든 사용자에게 권한을 열지 않는다.
4. 호스트 파일을 컨테이너에 읽기 전용으로 mount하고 컨테이너 안 경로를
   `GMAIL_CREDENTIALS_FILE`에 지정한다. 클라이언트 발급 파일과 인계 디렉토리 전체는 mount하지 않는다.
5. 발신 주소를 승인한 계정에 맞춰 `GMAIL_SENDER_EMAIL`로 주입한다. 기존 이미지와
   나머지 DB·앱 설정을 유지하며 적용할 환경변수와 mount를 검토한다.
6. Docker 환경변수·mount를 바꿨다면 변경된 설정으로 컨테이너를 재생성한다.
   `docker restart`만으로 새 환경변수나 mount가 반영되지는 않는다.
   인증파일 내용만 교체해도 메모리에 남은 접근 토큰이 있으므로 프로세스를 다시 시작한다.
   계정 교체를 위한 코드 수정이나 이미지 재빌드는 필요하지 않다.
7. 관리자 테스트 메일을 명시적으로 요청하고 전달 결과 API와 수신 측을 확인한다.
   Google 접수 결과 `SENT`와 받은편지함 도착 확인을 구분한다.

새 계정으로 변경하면 실제 발신 주소는 그 계정으로 바뀌며, 표시 이름은 `Ajin_scrap_Team`을
유지한다. 서버에 파일이 존재하고 읽을 수 있다는 사실만으로 토큰 유효성·발송 성공을 확정하지 않는다.
기존 인증의 회수·파일 정리는 대체 인증의 연결과 발송을 확인한 다음 소유자가 결정한다.

## 관리자 계정과 인계 메모

실제 로그인 기준은 DB에 저장된 계정과 Argon2id 비밀번호 해시다. 인계 메모를 복사하거나
그 안의 비밀번호를 수정해도 기존 계정의 비밀번호가 바뀌거나 새 계정이 생성되지 않는다.
계정 초기화·재설정은 [계정 저장 문서](AUTH_STORAGE.md)의 절차를 따른다.
이미 계정이 있으면 `ADMIN_PASSWORD` 환경변수를 바꾸는 것으로 덮어쓰지 않는다.

인계 메모가 필요한 경우에만 다음과 같은 형식을 사용할 수 있다. 이 형식은 Backend가
입력받는 설정 규격이 아니며 **파일 내용은 암호화되지 않은 평문**이다.

```json
{
  "username": "example-admin",
  "password": "<ADMIN_LOGIN_PASSWORD>"
}
```

```text
username: example-admin
password: <ADMIN_LOGIN_PASSWORD>
```

가능하면 암호화된 비밀번호 관리자로 보관하고 평문 사본을 만들지 않는다. 서버 인계에
평문 메모가 꼭 필요하다면 소유자 승인 후 서버의 비공개 디렉토리에 필요한 파일만
등록하고 예를 들어 `0400`으로 제한한다. `0400`·`0600`·`0700`은 접근 권한이며 암호화가 아니다.
정상 로그인 여부를 확인하기 전에는 기존 암호를 교체하거나 유일한 보관 자료를 삭제하지 않는다.
이 메모를 정리하는 데 Backend 코드 수정·이미지 재빌드·서버 재배포는 필요하지 않다.

## 완료 확인과 기록

파일 등록 완료, 설정 적용, 메일의 실제 발송·수신 확인을 각각 기록한다. 점검 결과에는
파일의 존재 여부·권한·읽기 전용 연결·서비스 상태와 비밀값 없는 성공/실패 결과만 남긴다.
개인 이메일 주소·비밀번호·OAuth 파일 본문·토큰·실제 서버 접속 정보는 기록하지 않는다.

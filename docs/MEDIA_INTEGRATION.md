# 카메라 미디어 API 연동

미디어 서비스 v1의 JSON API로 녹화 설정과 이벤트 클립을 제어한다. 영상 수신·버퍼·인코딩·파일 저장은 미디어 서비스가 담당한다. FastAPI는 영상 바이트를 받거나 전달하지 않는다.

## 기존 영상 연결과의 관계

기존 `/api/v1/webrtc/*`와 Mock SDP Fallback은 유지한다. 카메라 v1 문서는 MJPEG를 요구하지만 기존 개발 규칙은 WHEP 유지를 요구하므로, 이 변경에서 WHEP를 제거하지 않는다. MJPEG 접근 경로는 별도 Nginx 게이트웨이의 선택 구성으로 제공한다. 실제 배포에서는 미디어 담당자와 사용할 영상 방식을 확정하고 WHEP 또는 게이트웨이를 연결한다.

문서는 API 계약이며 실제 미디어 서비스의 구현·배포 완료를 보장하지 않는다. 배포 전 담당자와 제공되는 API 및 실시간 영상 방식을 확인한다.

## 환경 설정

`.env.example`을 복사한 뒤 다음 값을 비공개 `.env`에 설정한다.

| 변수 | 의미 |
| --- | --- |
| `MEDIA_SERVICE_URL` | 백엔드에서 접근할 JSON API 기본 주소. `/api/v1` 포함 |
| `MEDIA_API_TOKEN` | 백엔드와 미디어 서비스 사이의 Bearer 토큰 |
| `MEDIA_CAMERA_ID` | 현재 기본값 `camera-main` |
| `MEDIA_PIT_ID` | 카메라가 관측하는 피트. 기본값 `pit-01` |
| `MEDIA_REQUEST_TIMEOUT_SECONDS` | 요청별 타임아웃. 기본 3초 |
| `MEDIA_POLL_INTERVAL_SECONDS` | 상태 조회 간격. 기본 2초 |

주소와 토큰은 함께 설정한다. 둘 다 비워두면 연동과 자동 녹화 요청이 비활성화된다. 실제 토큰·내부 주소를 코드나 이슈에 기록하지 않는다. 컨테이너 안의 `127.0.0.1`은 다른 컨테이너나 호스트를 뜻하지 않는다.

## 백엔드 API

모든 신규 API는 `/api/v1` 아래에 있다. 대시보드의 기존 세션 쿠키로 인증한다. 변경 API는 관리자 권한과 로그인 응답의 `csrfToken`을 담은 `X-CSRF-Token` 헤더가 필요하다. 서비스 토큰은 브라우저에 전달하지 않는다.

| 메서드 | 경로 | 기능 |
| --- | --- | --- |
| GET / PUT | `/media/cameras/{camera_id}/recording-policy` | 녹화 정책 조회·변경 |
| POST | `/media/clips` | 기존 백엔드 이벤트의 클립 요청·재시도 |
| GET | `/media/clips` | 미디어 서비스의 필터·커서 기반 목록 |
| GET | `/media/clips/{clip_id}` | 최신 상세 및 상태 조회 |
| DELETE | `/media/clips/{clip_id}` | 클립 삭제 |
| GET | `/media/clip-jobs` | 로컬 전송 대기·실패 및 마지막 원격 상태 조회 |
| GET | `/recordings`, `/recordings/{recordingId}` | 대시보드용 녹화 목록·상세 |
| GET | `/media/access` | 게이트웨이의 세션 확인용 API |

녹화 정책은 `pre_roll_seconds`(0~300), `post_roll_seconds`(0~300), `retention_days`(1~365) 세 필드를 모두 전달한다. 전후 시간 합계는 1초 이상이어야 한다. 미디어 서비스 기본값은 30초·30초·30일이며 백엔드가 초기 실행 시 덮어쓰지 않는다.

클립 요청 본문은 다음 형태다. 이벤트 ID는 기존 `/events` 응답에 있는 `evt-alt-{id}` 또는 `evt-col-{id}`를 사용한다. 등록되지 않은 이벤트나 다른 피트의 이벤트는 거부한다.

```json
{"event_id":"evt-alt-1","camera_id":"camera-main"}
```

신규 접수 시 `202`, 기존 접수 시 `200` 및 클립 ID를 반환한다. 저장 완료를 뜻하지 않는다. 완료 상태는 `GET /media/clips/{clip_id}`에서 확인한다. 클립 목록의 `from`은 최초 접수 시각 이상, `to`는 미만이며 UTC 시각을 사용한다. `cursor`는 해석하거나 수정하지 않고 그대로 전달한다.

원격 상세의 `content_path`는 미디어 서비스 기준의 상대 경로다. 브라우저는 이를 백엔드 API 주소에 붙이지 않고, `/recordings`가 반환하는 게이트웨이용 `contentPath`·`downloadPath`를 사용한다.

## 자동 녹화와 장애 처리

- 쿨다운을 통과해 DB에 기록되는 수거 필요 경보와 작업자의 수거 완료 확인을 녹화 대상으로 삼는다. 카메라와 연결된 피트만 처리한다. 연동이 켜진 상태의 수거 완료 확인은 관리자 세션과 CSRF 토큰이 필요하다.
- 이벤트와 녹화 요청을 같은 DB 트랜잭션에 저장한다. 네트워크 호출은 별도 비동기 작업자가 수행하므로 센서 수신은 미디어 응답을 기다리지 않는다.
- 요청 대기 행은 새 `media_clip_jobs` 테이블에 저장한다. 기존 테이블은 변경하지 않으며 앱의 DB 초기화 시 새 테이블을 만든다. 영상 파일이나 내부 파일 경로를 저장하지 않는다.
- 타임아웃·네트워크 장애·408·429·5xx는 같은 이벤트 ID로 재시도한다. 재시도 간격은 최대 30초까지 늘린다. 인증·규격 오류는 `blocked`로 보관한다. 원인을 수정한 뒤 같은 이벤트 ID로 POST하여 재개할 수 있다.
- 재시작 후 DB의 대기 작업을 복구한다. 미디어 서비스가 접수했지만 응답이 유실된 경우에도 같은 ID를 사용한다. API·작업자 간 처리는 프로세스 내 잠금으로 조정하며 **현재 배포는 Uvicorn 단일 worker를 전제로 한다**. 사용자 세션은 DB에서 공유하지만 다중 worker·복제 배포 전에는 DB 기반 작업 선점이 필요하다.
- `collecting`·`processing`은 약 2초마다 조회한다. `ready`·`failed`·`deleted`·`expired`에서 완료 대기를 종료한다. 완료 클립 상세 조회는 외부 삭제·만료 상태도 갱신한다.
- 녹화 구간은 **미디어 서비스 최초 접수 시각** 기준이다. 요청이 지연되면 실제 사건 당시 영상이 범위 밖일 수 있다. 재시도로 과거 사건 시각을 복원하지 않는다.

## 대시보드 녹화 정보

연동 활성화 시 `/recordings`는 백엔드가 요청한 이벤트 클립을 로컬 DB에서 페이지로 나누어 반환한다. 미디어 서비스의 전체 클립 목록과 원격 커서는 `/media/clips`로 조회한다. 기존 웹훅으로 저장한 색인은 연동 비활성화 모드에서만 기존 방식으로 제공하며, 활성화 시 완료 웹훅은 `410`을 반환한다.

`/events`의 `recordingId`는 실제 이벤트 ID와 연결된 클립 ID다. 시간 구간이 겹친다는 이유로 연결하지 않는다. 로컬 요청 대기는 클립 ID가 없으므로 `/media/clip-jobs`에서 확인한다.

기존 `status`는 화면 호환을 위해 `available`·`processing`·`unavailable`·`expired`로 유지한다. 정확한 원격/전송 상태는 `mediaStatus`, 부분 저장 여부는 `coverage`, 누락 구간은 `gaps`, 실패 코드는 `errorCode`로 제공한다. 파일 생성 완료 전에 보관 만료일을 만들지 않으며 규격에 없는 해상도·FPS·파일 크기·썸네일은 `null`이다. 신규 클립에서는 `durationSeconds`, `sizeBytes`, `widthPixels`, `heightPixels`, `frameRate`, `retainedFrom`, `retainedUntil`의 null을 처리해야 한다.

목록은 마지막으로 확인한 상태를 표시한다. 완료 시각으로 계산된 만료일이 지나면 파일 경로를 숨긴다. 재생 전 상세 조회로 외부 삭제 여부도 확인한다. 파일 접근의 최종 상태는 미디어 서비스가 판단한다.

## 선택 구성: 인증된 영상 게이트웨이

`docker-compose.media.yml`과 `deploy/media-gateway*.conf*`는 별도 Nginx 프로세스로 영상 바이트를 전달한다. `MEDIA_PROXY_UPSTREAM`에 미디어 서비스의 `호스트:포트`를 설정한다. JSON API 주소와 같은 서비스여야 한다. 토큰에 공백·줄바꿈·따옴표 등의 설정 구문 문자를 사용하지 않고 안전한 영숫자 또는 URL-safe 토큰을 사용한다.

```bash
docker compose -f docker-compose.yml -f docker-compose.media.yml up -d --build
```

게이트웨이는 기본적으로 로컬 호스트의 8080 포트에만 노출한다. 로그인과 API를 게이트웨이의 `/api/v1`로 요청하면 같은 호스트의 세션 쿠키를 사용한다. 대시보드와 아래 경로를 같은 HTTPS 출처에 배치한다. 운영 환경의 Secure 쿠키에는 HTTPS가 필요하다. 외부 TLS 프록시도 영상 경로의 응답 버퍼링을 끄고 브라우저 연결 종료를 전달해야 한다.

- 실시간 MJPEG: `/media/cameras/{camera_id}/live`
- MP4 재생: `/media/clips/{clip_id}/content`
- 다운로드: `/media/clips/{clip_id}/content?download=true`

게이트웨이는 백엔드의 세션 확인 후 서버 내부에서 서비스 토큰을 추가한다. 미디어 서비스로 사용자 쿠키를 전달하지 않는다. 영상 GET만 허용하며 녹화 제어 경로는 노출하지 않는다. 응답을 캐시·전체 버퍼링하지 않고 Range, Content-Range, Accept-Ranges, Content-Length, Content-Type, Content-Disposition 및 상태 코드를 보존한다. 처리 중 파일의 `409`, 삭제·만료 파일의 `410`, 잘못된 범위의 `416`도 전달한다.

세션은 새 요청 시 확인한다. 이미 시작된 영상 전송은 로그아웃 직후에도 유지될 수 있다. 미디어 서비스의 직접 접근 포트는 내부 네트워크에 제한한다. 렌더링된 Nginx 설정에는 서비스 토큰이 있으므로 커밋하거나 `nginx -T` 출력으로 공유하지 않는다. 릴리스 배포 이미지는 조직 정책에 맞춰 검증된 digest로 고정한다.

## 검증

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest -v
docker build -t scrap-backend-issue25:local .
uv run python tests/integration/verify_media_gateway.py
```

자동 테스트는 합성 데이터와 모의 미디어 서비스만 사용한다. 마지막 명령은 Docker가 필요하며 실제 Nginx 컨테이너에서 세션 인증, MJPEG 전달, 파일 Range·다운로드 및 오류 상태 전달을 검증한다. 실제 카메라의 영상 품질·부분 저장·재시작 복구·보관 만료는 배포 환경에서 별도 통합 시험한다.

Nginx 설정 근거: [auth_request](https://nginx.org/en/docs/http/ngx_http_auth_request_module.html), [proxy module](https://nginx.org/en/docs/http/ngx_http_proxy_module.html).

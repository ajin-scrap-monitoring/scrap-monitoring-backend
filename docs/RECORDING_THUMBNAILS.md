# 녹화 썸네일

기존 ready 클립·요청 구간 MP4와 완료 날짜의 ready MP4에서 미디어 서비스가 준비한 실제 이미지를 사용한다.
기존 media-info.thumbnail은 unsupported/null 계약을 그대로 유지한다. 이미지 생성·저장·교체·삭제/만료는 미디어가 담당한다.
백엔드는 JSON과 권한을 검증하고 Nginx에 고정 내부 경로를 전달한다. FastAPI는 이미지 바이트를 받거나 저장·변환·중계하지 않는다.

## 공개 API

| 대상 | 정보 GET | 준비 POST | 이미지 GET/HEAD |
| --- | --- | --- | --- |
| 클립 | `/api/v1/media/clips/{clip_id}/thumbnail-info` | `/api/v1/media/clips/{clip_id}/thumbnail` | `/media/clips/{clip_id}/thumbnail` |
| 날짜 | `/api/v2/recordings/daily/{recording_id}/thumbnail-info` | `/api/v2/recordings/daily/{recording_id}/thumbnail` | `/media/daily-recordings/{recording_id}/thumbnail` |

모든 경로에 로그인 쿠키가 필요하다. 정보·이미지 조회는 기존 관리자/조회 사용자에게 허용한다.
준비 POST는 자원을 사용하는 작업이므로 **관리자와 X-CSRF-Token**을 요구한다. 조회 사용자에게는 준비 권한을 추가하지 않는다.
기존 날짜 영상 다운로드 준비의 조회 사용자 권한과 다르다. 조회 화면은 미준비 상태를 안내하고 자동 POST를 실행하지 않는다.
POST 본문은 `{}` 또는 `{"retry_failed":true}`이며 다른 필드/문자열 bool은422다.
canonical 소문자 하이픈 UUID만 허용하고 쿼리는 받지 않는다. 이벤트 ID와 클립 ID를 혼용하지 않는다.

신규 접수202/pending, 이미 pending/ready 또는 준비 불가 등200. Location은 같은 공개 정보 GET 경로다.
GET·목록·재생은 준비 POST나 영상 export를 유발하지 않는다. 백엔드는 자동 재시도/전체 과거 변환/폴링 작업자를 만들지 않는다.
준비 상태가 pending일 때 프론트가 제한적으로 정보 GET을 재조회하며 failed에는 명시적 재시도를 사용한다.

## 응답·시각

응답은 clip_id 또는 recording_id, camera_id, status, representative_at, time_basis, content_path,
width, height, size_bytes, reason_code, generated_at을 포함한다.

- ready: 실제 이미지 경로·양의 치수·바이트·UTC 생성 시각 필수. 최대640×360·512KiB.
- 대표 시각이 입증되면 representative_at과 time_basis=server_received_at을 제공한다.
- 시각 근거 없는 ready도 정상 이미지다. representative_at/time_basis=null, reason_code=TIMELINE_UNAVAILABLE이다.
  generated_at은 이미지 생성 시각이며 원본 촬영 시각으로 바꾸지 않는다.
- unsupported/pending: 이미지·시각·사유 모두 null. unavailable/failed: 이미지·시각 null, 안전한 reason_code 필수.
- 정보 응답의 내부 ID·카메라와 부모 상세를 대조하고 미디어의 정확한 상대 경로만 공개 이미지 경로로 바꾼다.
  외부 URL·다른 자원·경로 이동·임의 예외 본문은 제공하지 않는다.
- 구버전에서 부모는 확인되지만 전용 정보 경로가404/알 수 없는 코드이면 unsupported로 안내한다.
  알려진 자원 오류·삭제/만료·저장소/통신 오류를 미지원으로 바꾸지 않는다. 준비 POST는 구버전404 오류를 그대로 처리한다.

## 이미지 전달·오류

같은 출처 Nginx에서 이미지 GET/HEAD마다 로그인→부모 ID/카메라/삭제/만료→썸네일 JSON을 확인한다.
이후 X-Accel-Redirect로 접근 제한된 고정 `/_thumbnail/` 경로에 전달한다. 브라우저 직접 내부 경로 접근은404다.
이미지 바이트는 Nginx와 미디어 사이에서 흐른다. 쿠키·Authorization·Range·조건부 캐시 헤더를 미디어에 전달하지 않는다.
미디어의 리다이렉트는502로 차단하고 upstream X-Accel-Redirect를 실행하지 않는다.
미디어는 image/jpeg·실제 Content-Length·HEAD 빈 본문을 책임진다. JPEG 디코딩 검사는 미디어 범위이며 백엔드가 바이트를 받아 다시 검사하지 않는다.
JSON·이미지에는 no-store/nosniff를 적용한다. 로그아웃 후 새 접근은401이고 이미 표시한 픽셀을 회수하지는 않는다.

| 조건 | 응답 |
| --- | --- |
| 비로그인/잘못된 UUID·쿼리 | 401 / 422 |
| 조회 사용자의 준비/CSRF 누락 | 403 |
| 대상 카메라 불일치 | 404 |
| 부모 삭제/만료 또는 확인된 만료 시각 경과 | 410 |
| 이미지 unsupported/unavailable/pending/failed | 404 / 404 / 409 / 503 |
| 미디어 응답·ID·경로 불일치 | 502 |
| 통신 실패/시간 초과 | 503 / 504 |

THUMBNAIL_CAPACITY_EXCEEDED·THUMBNAIL_STORAGE_UNAVAILABLE 등 미디어의 안전한 오류 코드만 전달한다.
미디어 v1/v2 썸네일과 해당 부모 JSON은 기존 내부망/loopback 경계를 사용하며 토큰을 요구하지 않는다.
기존 다른 v1 기능의 토큰 정책·설정은 바꾸지 않는다. 내부 미디어 API를 인터넷에 공개하지 않는다.
부모 상세와 전용 응답을 합친 백엔드 요청은 설정된 MEDIA_REQUEST_TIMEOUT_SECONDS의 작업 예산을 사용한다.
미디어 기본 실행15초·동시1개·실행 포함4개 제한은 별개이며 큐 대기를 포함한 완료15초 보장이 아니다.

## 검증과 연결

`uv run pytest -v tests/test_thumbnails.py`와 일반 전체 검증을 실행한다.
이미지 빌드 후 `uv run python -m tests.integration.verify_media_gateway --image <image>`는 실제 Nginx의 세션·GET/HEAD·고정 내부 경로·삭제/만료 접근과 기존 영상 기능을 합성 환경에서 검증한다.
이 시험의 이미지 본문은 전달 경로 검사용 합성 바이트다. 실제 JPEG 생성/재시작 재사용은 미디어 검증과 구분한다.
프론트는 백엔드·게이트웨이 호환 버전 적용 후 선택 영상의 content_path를 사용하고 미준비/실패여도 목록·재생을 유지한다.
목록 전체에 상세 정보 요청을 일괄 추가하지 않으며 화면에 보이는 항목을 제한적으로 조회한다. 기존 영상 삭제·새 추출을 실서버 검증 목적으로 실행하지 않는다.

# 날짜별 상시 녹화 연동

카메라 파트의 `DAILY_RECORDING_API_GUIDE_CAMERA.md` 확정 규격에 맞춘 백엔드 API다. 한국 날짜별 한 건을 관리하며, 상시 녹화·파일 저장·HLS 생성·MP4 생성·보관 만료는 미디어 서비스가 담당한다. 백엔드는 JSON과 사용자 인증을 담당하고 영상 바이트는 Nginx 게이트웨이가 전달한다. 기존 이벤트 클립과 실시간 MJPEG는 그대로 v1을 사용한다.

## 연결 설정

- 기존 `MEDIA_SERVICE_URL=http://<media-host>:8889/api/v1` 설정이 있으면 같은 호스트의 `/api/v2`를 사용한다.
- 날짜 기능만 연결하거나 별도 주소를 지정하려면 `MEDIA_DAILY_SERVICE_URL=http://<media-host>:8889/api/v2`를 설정한다. 이 값이 우선한다.
- v2 내부 JSON·영상 요청에는 Bearer 토큰이나 브라우저 쿠키를 전달하지 않는다. 기존 v1 토큰 설정은 그대로 유지한다.
- `MEDIA_CAMERA_ID`로 지정한 카메라를 조회한다. 브라우저가 임의의 내부 주소나 카메라 ID를 지정하지 않는다.
- 미디어 서비스에는 `DAILY_RECORDING_ENABLED=true`와 유한한 `MAX_DAILY_STORAGE_BYTES`가 필요하다. 활성화 전에는 v2 경로가 없을 수 있다. 보관 30일 설정이 실제 30일 저장 용량을 보장하지 않는다.
- 기존 `docker-compose.media.yml` 게이트웨이의 `MEDIA_PROXY_UPSTREAM`은 날짜 API와 영상도 제공하는 같은 미디어 서비스를 가리켜야 한다. JSON 주소만 다른 서비스로 바꾸면 영상 경로가 맞지 않는다.
- 이 변경은 백엔드와 Nginx 템플릿을 함께 배포해야 적용된다. 기존 공용 서버의 별도 게이트웨이 구성에도 같은 경로·인증·HLS URI 변환을 반영해야 한다. 이번 작업에서 현장 서버를 재배포하지 않는다.

내부 미디어 API 포트를 외부 브라우저에 직접 공개하지 않는다. 프론트엔드는 백엔드 API와 영상 게이트웨이를 동일 출처에서 이용한다.

## 화면용 API

기본 경로는 `/api/v2/recordings/daily`다. 모든 경로에 로그인 쿠키 `scrap_session`이 필요하다.

| 메서드·경로 | 권한 | 기능 |
| --- | --- | --- |
| `GET /api/v2/recordings/daily` | 로그인 사용자 | 날짜별 목록 |
| `GET /api/v2/recordings/daily/{recording_id}` | 로그인 사용자 | 상세·누락·준비 상태 |
| `POST /api/v2/recordings/daily/{recording_id}/exports` | 로그인 사용자 + CSRF | 지난 날짜 MP4 준비 |
| `GET /api/v2/recordings/daily/policy` | 로그인 사용자 | 날짜 녹화 정책 조회 |
| `PUT /api/v2/recordings/daily/policy` | 관리자 + CSRF | 날짜 녹화 정책 변경 |
| `DELETE /api/v2/recordings/daily/{recording_id}` | 관리자 + CSRF | 지난 날짜 녹화 삭제 |

변경 요청은 로그인 응답의 `csrfToken`을 `X-CSRF-Token` 헤더로 전달한다. 다운로드 준비는 일반 조회 사용자도 할 수 있지만, 보관 정책 변경과 삭제는 관리자만 가능하다. 미디어 서비스가 실제 상태를 판정하며 오늘 준비·진행 중 삭제 등은 409로 거절한다. 파일 정리가 완료된 항목의 재삭제는 204를 반환한다.

논리 삭제와 파일 정리 결과, 실패 후 재시도 및 삭제 이후 필드 해석은
[파일 정리 상태](RECORDING_CLEANUP.md)를 따른다. 503 정리 실패를 성공으로 바꾸지 않는다.

정책 PUT 본문은 `{"enabled": true, "retention_days": 30}`이며 두 필드 모두 필요하다. `enabled`는 boolean, `retention_days`는 1~365 정수다. 다른 필드와 잘못된 타입은 422다. 정책 변경의 적용 시점과 기존 날짜 보관 정책은 미디어 서비스 규칙을 따른다.

목록 쿼리:

| 필드 | 의미 |
| --- | --- |
| `from_date` | 한국 날짜 `YYYY-MM-DD`, 포함 |
| `to_date` | 한국 날짜 `YYYY-MM-DD`, 제외 |
| `limit` | 기본 20, 1~100 |
| `cursor` | 응답 `next_cursor`를 그대로 전달 |
| `status` | recording / finalizing / closed / failed / deleted / expired |

예: `?from_date=2026-10-01&to_date=2026-10-15&limit=20`은 한국 10월 14일까지다. 다음 페이지는 같은 날짜 필터·상태·limit을 유지한다. 커서는 미디어 서비스 기준 24시간 유효하며 만료·필터 변경은 `400 INVALID_CURSOR`다. 목록은 `items`, `next_cursor`이고 날짜 내림차순이다. 기본 목록은 deleted/expired를 제외하며 영상 없는 closed/none 날짜는 포함한다.

## 응답과 재생 시간축

응답은 카메라 계약과 같은 snake_case 필드를 사용한다. 날짜 `recording_date`는 한국 날짜를 유지하고 모든 시각은 UTC ISO 8601로 제공한다. `revision` 등 계약에 추가될 수 있는 관리 필드는 현재 화면 응답에 포함하지 않는다.

- `window_start_at/end_at`: 한국 자정부터 다음 자정 미만의 관리 시간창.
- `status`와 `coverage`: 녹화 중은 recording/null, 마감 후 complete/partial/none. closed는 MP4 준비 완료와 별개다.
- `coverage_as_of_at`: 저장·누락 확인이 완료된 시각. 미래 시간이나 아직 처리 중인 꼬리를 누락으로 만들지 않는다.
- `has_gaps`, `gaps`: 현재 확인된 누락 구간과 원인. 정상 연결 상태에서도 프레임 정지 등으로 누락될 수 있다.
- `recorded_seconds`: 실제 확보한 영상 길이이며 항상 86,400초인 것은 아니다.
- `available_intervals`: 이어 붙인 재생 위치와 실제 날짜·시각의 대응. 재생 초를 자정에 단순히 더하면 누락 이후 시각이 틀어진다.
- `playable_until_at`: 완료 조각의 마지막 실제 시각이며 그 이전에도 누락이 있을 수 있다.
- `playback_state`, `export_state`, `size_bytes`, `expires_at`, `export_expires_at`: 재생·다운로드 준비와 원본·MP4 캐시 만료를 각각 표시한다.

백엔드는 `playback_path`를 `/media/daily-recordings/{recording_id}/hls/index.m3u8`로 바꾼다. `download_path`는 준비된 경우에만 `/media/daily-recordings/{recording_id}/content?download=true`로 제공한다. 링크가 null이면 버튼을 활성화하지 않는다. 다른 날짜 ID의 링크·외부 URL·일치하지 않는 카메라 응답은 502로 거절한다. 오류 상세 메시지는 내부 정보를 숨기고 코드만 화면 판단에 활용한다.

오늘은 완료된 10초 조각까지 HLS EVENT 목록으로 재생하고 지난 날짜는 HLS VOD로 재생한다. 인코딩 시간에 따른 지연이 있다. HLS 플레이어는 브라우저별 지원에 맞춰 프론트엔드에서 연결한다. `EXT-X-DISCONTINUITY`만으로 누락을 판단하지 않고 JSON의 gaps를 사용한다. 프론트엔드는 00~24시 시간축에 누락·미래·처리 중 구간을 구분하고, 누락 선택 시 해당 시간 영상이 없음을 알린다.

## MP4 다운로드 흐름

1. 지난 날짜가 closed이고 `export_allowed=true`이면 POST exports를 호출한다. 본문은 필요 없다.
2. 최초·진행 중은 202, 이미 준비됐으면 200이며 `Location`은 백엔드 상세 경로다. 요청이 중복돼도 미디어 서비스의 같은 날짜 작업을 사용한다.
3. 상세를 약 2초 간격으로 조회하되 긴 작업은 간격을 늘린다.
4. `export_state=ready`와 `download_path`가 있으면 해당 게이트웨이 경로로 다운로드한다.
5. 실패하면 error.code를 표시한다. 원인을 해결한 후 같은 날짜 ID로 준비를 재요청한다. MP4 캐시만 만료된 경우도 다시 준비할 수 있다.

MP4는 확보된 구간만 포함하는 무음 영상이다. 부분 저장 여부와 실제 길이를 함께 표시한다. 전체 파일을 FastAPI 메모리에 읽거나 응답하지 않는다. 날짜가 마감되기 전에는 하루 다운로드를 제공하지 않는다.

## 영상 게이트웨이

다음 세 경로의 GET/HEAD만 허용하며 각 요청에 현재 DB 세션을 검사한다.

```text
/media/daily-recordings/{recording_id}/hls/index.m3u8
/media/daily-recordings/{recording_id}/media/{segment_id}.ts
/media/daily-recordings/{recording_id}/content
```

게이트웨이는 미디어 서비스 `/api/v2/daily-recordings/...`로 전달한다. m3u8 안의 `/api/v2/daily-recordings/` URI도 `/media/daily-recordings/`로 변환하므로 영상 조각까지 인증 경로를 유지한다. 영상 조각·MP4의 Range/If-Range, 206과 Content-Range, 다운로드 Content-Disposition을 전달하고 캐시·버퍼링을 끈다. 로그아웃·세션 만료 후 새 목록·조각·다운로드 요청은 401이다. 이미 전송 중인 응답이나 브라우저에 받은 영상까지 회수하는 의미는 아니다.

## 오류와 검증 범위

입력·권한 오류는 422/401/403이다. 미디어 API의 400/404/409/410/415/416/429/503은 안전한 계약 코드로 전달한다. 예: DAY_NOT_CLOSED, DAY_FINALIZING, NO_RECORDING_DATA, EXPORT_IN_PROGRESS, DAILY_RECORDING_EXPIRED, INSUFFICIENT_STORAGE. 잘못된 상위 응답은 502, 연결 실패는 503, 요청 시간 초과는 504다. export_state=failed는 날짜 원본의 coverage=partial과 구분한다.

실제 카메라 메타데이터에는 시작·끝 시각이 같은 구간도 있어 원본 시각과 재생 위치 대응을 유지한다. 역순 구간은 거절하며, 동일 시각 구간을 임의로 늘리거나 삭제하지 않는다.

합성 API 테스트와 실제 Docker/Nginx 테스트로 인증·CSRF·한국 날짜·누락·MP4 준비·HLS URI 변환·영상 조각·HEAD·Range·삭제·로그아웃을 검증한다. 실제 카메라 촬영 품질, 하루 저장량, 보관 기간, 디스크 부족 시 운영 동작 및 프론트엔드 플레이어는 현장 통합 검증 대상이다. 카메라 파트 서버 검증과 백엔드·대시보드 최종 연동 검증은 별개다.

## 상시 영상 공유 방식

추가 클립을 생성하지 않고 기존 날짜 영상의 이벤트 구간을 조회하려면 [이벤트 구간 재생](EVENT_RECORDING_REFERENCES.md)을 따른다. 이 방식은 읽기 전용이며 기존 자동 클립 설정을 켤 필요가 없다.

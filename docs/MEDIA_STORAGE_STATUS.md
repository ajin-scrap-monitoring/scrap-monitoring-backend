# 미디어 저장소 용량 조회

`GET /api/v1/media/storage/status`는 미디어 서버의 관리 파일시스템과 날짜 녹화 영역을
읽기 전용으로 조회한다. 관리자·조회 사용자 모두 로그인해야 하며 CSRF는 필요 없다.
쿼리 인수는 받지 않는다. 응답은 `Cache-Control: no-store`다.

기존 `MEDIA_SERVICE_URL`의 `/storage/status`에 JSON GET 한 번만 보낸다.
미디어의 해당 API는 내부 연결 경계를 사용하므로 별도 Bearer 토큰을 붙이지 않으며,
브라우저 쿠키·인증 헤더를 전달하지 않는다. 백엔드 자체 디스크 용량을 대신 반환하지 않는다.
조회는 영상 생성·검사·인코딩·정리·백엔드 작업 등록·보관 정책 변경을 수행하지 않는다.

## 응답 필드

| 필드 | 의미 |
| --- | --- |
| `status` | `ready` 또는 `unavailable` |
| `storage_type` | `filesystem`. SD/SSD 등 물리 매체를 추정하지 않음 |
| `observation_started_at`, `observed_at` | 원천 관측 시작·완료 UTC 시각. 백엔드 조회 시각으로 대체하지 않음 |
| `filesystem_total_bytes` | 관리 파일시스템 전체 크기 |
| `filesystem_available_bytes` | 서비스 계정 기준 파일시스템 가용량. 새 녹화 작업의 승인 가능량이 아님 |
| `media_limit_bytes` | 설정된 전체 미디어 한도 또는 null |
| `daily_limit_bytes` | 설정된 날짜 영역 한도 또는 null. 전체 한도와 겹침 |
| `daily_usage_bytes` | daily·daily-exports·daily-playback·daily-thumbnails의 파일 길이 합계 |
| `daily_reserved_bytes` | 위 날짜 영역의 미할당 예약량. 범위를 확인할 수 없으면 null |
| `reason_code` | 정상 null, 예약 범위 불명 `RESERVATION_SCOPE_UNKNOWN`, 관측 불가 사유 등 |

바이트는 음이 아닌 정수이며 실제0과 확인 불가null을 구분한다. 사용량은 DB/WAL·clips·원본
JPEG/묶음 버퍼를 포함하지 않고, 정리 전 TS/MP4·임시 출력은 포함할 수 있다.
파일시스템 가용량·정책 한도·사용량·예약량을 합산하거나 단순 차감해 새 작업 허용량을 만들지 않는다.
서비스 전체 관측이며 피트·카메라별 전용 할당량이나30일 보관 보장이 아니다.

미디어는 성공 관측을 최대2초 재사용하고 예약 변경 시 무효화한다. 같은 관측이면 시각도 그대로다.
파일시스템·파일 열거 사이에 시간이 흐르므로 원자적인 스냅샷으로 취급하지 않는다.

## 실패·결측 처리

- HTTP200: 유효한 `ready` 관측. 예약 범위만 불명확한 경우 예약량null·사유코드를 유지한다.
- HTTP503 관측 응답: `status=unavailable`, 관측 시각·파일시스템 값·사용량·예약량null.
  확인 가능한 정책 한도는 유지한다. 사유는 `STORAGE_UNAVAILABLE` 또는 `STORAGE_SNAPSHOT_BUSY`다.
- 연결 실패·미설정도503이지만 기존 `{error: {code, message}}` 오류 형식이다.
  화면은 이전 값이나0을 현재 정상 관측으로 표시하지 않는다. OpenAPI는 두503 형식을 구분한다.
- 원천 응답 형식·시각·바이트·HTTP 상태가 모순되면502 `MEDIA_BAD_RESPONSE`, 시간 초과는504다.
- 로그인 없음401, 지원하지 않는 쿼리는422다. 자동 재시도나 새 백엔드 캐시는 추가하지 않는다.

원천의 내부 경로·장치 ID·임의 오류 문자열은 공개 응답에 포함하지 않는다.
기존 기간·만료 정책 조회 API와 영상 목록의 계약은 유지한다.

계약 근거: [Media 저장 공간 상태 조회](https://github.com/ajin-scrap-monitoring/ajin-camera-media-service/blob/f6acd8f44b664648d9fc30292e29df5a6dfedd3a/docs/BACKEND_API_CONTRACT.md#저장-공간-상태-조회-q8).

# 녹화 영상의 실제 파일 정보

다음 API는 선택한 영상의 코덱·해상도·명목 FPS·파일 크기를 JSON으로 조회한다.
목록이나 재생 상세에 자동으로 파일 검사를 추가하지 않는다.

| API | 용도 |
| --- | --- |
| `GET /api/v1/media/clips/{clip_id}/media-info` | 준비된 이벤트 클립·구간 다운로드 파일 |
| `GET /api/v2/recordings/daily/{recording_id}/media-info` | 날짜 MP4와 해당 날짜 조각의 측정 범위 |

두 API 모두 로그인 필요, 조회 사용자·관리자 모두 허용, CSRF 불필요, 성공 응답 no-store.
UUID를 검증하고 기존 상세에서 카메라 연결을 확인한 뒤 미디어에 요청한다.
클립 v1 요청에는 서버의 미디어 인증만 사용하고, 날짜 v2 요청에는 인증 헤더를 전달하지 않는다.
브라우저 쿠키와 로그인 인증 정보를 미디어 서비스로 전달하지 않는다.
영상 생성·내보내기·삭제·백엔드 작업 등록 없이 정보를 조회한다.
미디어의 파일 검사가 실행될 수 있지만 백엔드는 FFmpeg를 실행하거나 영상 바이트를 받지 않는다.

## 클립 응답

```json
{
  "clip_id": "550e8400-e29b-41d4-a716-446655440000",
  "size_bytes": 123456,
  "media_info": {
    "status": "ready",
    "source": "file_probe",
    "codec": "h264",
    "width": 1920,
    "height": 1080,
    "fps": 29.97002997002997,
    "fps_num": 30000,
    "fps_den": 1001,
    "size_bytes": 123456,
    "reason_code": null
  },
  "timestamp_info": {
    "time_basis": "server_received_at",
    "capture_timestamp_supported": false,
    "capture_timestamp": null,
    "capture_clock_source": null,
    "clock_synchronization": "unavailable"
  },
  "thumbnail": {
    "status": "unsupported",
    "representative_at": null,
    "content_path": null
  }
}
```

해상도는 픽셀, 크기는 바이트, fps는 파일 스트림의 명목 초당 프레임 수다.
원본 카메라 촬영률·평균 수신률·시계 동기화 품질과 구분한다.
`fps_num/fps_den`으로 분수 프레임 속도를 보존하며 설정값으로 대체하지 않는다.
이 파일 정보 검사는 초기 프레임에 대한 것으로 전체 파일 무결성 검사가 아니다.

`status=unknown`이면 codec/width/height/fps/fps_num/fps_den은 모두 null이다.
확인한 size_bytes만 남을 수 있으며, 0이나 임의 기본 해상도를 채우지 않는다.
reason_code는 file_unavailable/probe_failed/probe_timeout/probe_unavailable/
file_changed/metadata_unavailable 중 하나다. unknown은 정상적인200 응답이다.
관측값·크기·분수 FPS가 서로 모순되거나 계약과 다른 응답은502 MEDIA_BAD_RESPONSE다.

## 날짜 응답

`recording_id`, `file`, `segments`, 위와 같은 `timestamp_info`, `thumbnail`을 반환한다.

- `file`: 실제 하루 MP4의 media_info 객체 또는 null. 아직 MP4가 없으면 null이다.
  조각의 형식을 하루 파일의 실측값으로 복사하지 않는다.
- `segments.status`: ready/partial/unknown.
- `segments.total/measured`: 전체 조각 수와 실제 정보를 확인한 조각 수.
- `segments.formats`: codec/width/height/fps/fps_num/fps_den과 segment_count로 묶은 측정 결과.
- `segments.mixed`: 두 형식 이상 확인되면 true. 전부 확인했고 동일하면 false.
  형식 한 종류만 확인했지만 미측정 조각이 남아 있으면 null이다.

현재 녹화 중인 날짜도 조회할 수 있다. 하루 MP4가 ready여도 조각 검사 범위는 partial일 수 있다.
한 조각 표본 검사 결과만으로 전체 날짜의 형식·FPS가 같다고 표시하지 않는다.

촬영 시각과 썸네일은 현재 미지원 상태를 그대로 반환한다.
`server_received_at`을 원본 촬영 시각으로 표시하거나 가짜 이미지 경로를 만들지 않는다.

## 지연과 오류

미디어는 초기 최대3프레임/검사당15초, 동시2개/대기 포함8개, 캐시128개로 제한한다.
백엔드의 메타데이터 HTTP 조회에는 총18초 제한을 적용한다. 먼저 수행하는 기존 상세 조회에는
기존 MEDIA_REQUEST_TIMEOUT_SECONDS를 유지한다. 미디어 대기열까지 오래 걸리면 시간 초과할 수 있다.
다른 API의 시간 제한·정책은 바꾸지 않는다. 자동 재시도나 목록 전체의 일괄 검사는 하지 않는다.
프론트 요청 제한에는 선행 상세 조회 시간과18초 검사 제한을 함께 고려한다. 기본 설정에서는30초 정도의 여유를 둔다.
화면에서는 선택한 영상의 정보 영역만 로딩하고, 실패하면 나머지 재생·목록을 유지한다.

| HTTP | 의미 |
| --- | --- |
| 401 | 로그인 필요 |
| 404 | 파일 식별자·설정된 카메라에 해당하는 영상 없음 |
| 409 | 클립 준비 전(CLIP_NOT_READY) |
| 410 | 클립 또는 날짜 삭제·만료 |
| 422 | UUID 등 요청 형식 오류 |
| 502 | 잘못된 원천 응답·식별자 불일치·서버 간 인증 실패 |
| 503 | METADATA_CAPACITY_EXCEEDED·미설정·미디어 접속/저장소 문제 |
| 504 | MEDIA_TIMEOUT |

미디어 오류 원문과 내부 파일 경로는 노출하지 않는다.
프론트는 명시적 정보 조회를 연결해야 하며, 기존 API 추가만으로 화면 표시가 자동 변경되지는 않는다.
구간 다운로드 후에는 응답의 clipId로 클립 정보 API를 호출한다.

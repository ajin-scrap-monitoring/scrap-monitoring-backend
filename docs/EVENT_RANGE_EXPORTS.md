# 이벤트 구간 MP4 다운로드

이벤트 재생은 날짜 녹화를 공유한다. 목록·상세·HLS 조회는 파일을 만들지 않는다.
사용자가 다운로드를 요청할 때만 미디어 서비스가 별도 MP4를 추출한다.
백엔드는 JSON만 교환하며 파일은 인증된 Nginx 경로로 전달한다.
클립 파일은 추가 저장 공간을 사용하고 미디어 보관 정책에 따라 만료된다.
`MEDIA_V2_EVENT_CLIPS_ENABLED`는 이 명시적 다운로드 요청과 무관하며 기본 false를 유지한다.

## 요청과 조회

1. 이벤트 상세의 `exportPath`를 확인한다. 지원되는 확정 구간이면
   `downloadReasonCode=EVENT_EXPORT_NOT_REQUESTED`다. 상세의 `downloadPath`는 계속 null이다.
2. 로그인한 사용자가 `POST /api/v2/recordings/events/{event_id}/exports`를 호출한다.
   관리자·일반 조회 사용자 모두 가능하며 `X-CSRF-Token`이 필요하다.
3. 응답의 `statusPath`를 조회한다. `status=ready`이고 `downloadPath`가 있을 때 파일을 받는다.
   2~5초 간격으로 조회하고 failed/blocked/deleted/expired에서는 자동 조회를 멈춘다.

```json
{
  "requestId": "550e8400-e29b-41d4-a716-446655440000",
  "beforeSeconds": 30,
  "afterSeconds": 30,
  "retryFailed": false
}
```

`requestId`는 브라우저가 다운로드 동작을 시작할 때 생성한 UUID다. 같은 동작의 재전송에는
같은 UUID와 문맥 길이를 사용한다. 새 파일이 필요하면 새 UUID를 사용한다.
미디어의 event_id는 백엔드가 계정·requestId에서 생성하며 업무 eventId와의 관계를 DB에 저장한다.
사용자는 임의 카메라·시작/종료 시각·미디어 event_id를 지정할 수 없다.

- 수거 사이클: 기록된 수거 시작~종료. 전후 문맥은 구간에 더하지 않는다.
- 알림·오류·수동 수거 확인: 기존 재생과 동일하게 발생 시각의 전후 문맥(기본 각30초).
- 문맥은 각각0~300초, 합계1초 이상. 확정되지 않은 수거는409 EVENT_WINDOW_NOT_CLOSED.
- 구간은0초 초과/최대1시간. 초과·0초는422 EVENT_EXPORT_RANGE_TOO_LONG.
  긴 구간을 몰래 자르거나 하루 전체 다운로드로 대체하지 않는다.
- 내부 요청은 UTC 밀리초의 `[start_at,end_at)`다. 미래 꼬리·누락은 미디어가 확보분과 사유로 반환한다.

신규 접수는202, 기존 작업 확인은200이다. `Location`은 상태 조회 경로와 같다.
응답에는 다음 필드가 있다.

| 필드 | 의미 |
| --- | --- |
| requestId / eventId | 다운로드 요청과 업무 이벤트의 식별자 |
| statusPath | `GET /api/v2/recordings/events/{event_id}/exports/{request_id}` |
| status | pending/processing/ready/failed/deleted/expired/blocked 등 작업 상태 |
| requestedFrom / requestedTo | 최초 요청 때 확정·저장한 구간 |
| clipId / attemptCount | 미디어 접수 후 UUID·미디어 시도 횟수 |
| coverage | complete/partial/none/null. 파일 준비 성공 여부와 별개 |
| actualFrom / actualTo | 실제 추출 구간. stream_copy 절단은 최대1초 확장 가능 |
| availableIntervals / gaps | 실제 확보·누락 구간(start_at/end_at), gaps의 안전한 reason_code |
| sizeBytes / extractionMode | 실제 파일 크기 또는 null, stream_copy 또는 null |
| expiresAt | 미디어 보관 만료 시각 |
| downloadPath | 유효한 ready 파일에만 `/media/clips/{clipId}/content?download=true` |
| errorCode | 안전한 오류 코드. 원천 오류 원문은 전달하지 않음 |

최초202 응답은 접수 영수증이라 coverage·파일 크기·경로가 null일 수 있다.
후속 상태 조회에서 실제 상세 메타데이터를 확인한다. 모든 JSON 응답은 no-store다.
상태 조회는 요청한 계정만 가능하며, 없는 요청·다른 계정·다른 이벤트는404다.
파일 경로는 기존 녹화와 동일한 로그인 기반 읽기 권한을 사용한다.
상태 조회에 실패하면 기존 downloadPath를 확정 정보로 재사용하지 않는다.

## 중복·복구·실패 재시도

백엔드는 미디어 HTTP 호출 전에 요청 관계와 구간을 DB에 저장한다.
응답이 끊겨도 같은 requestId로 재전송하거나 작업자 재시작 후 같은 미디어 키를 사용한다.
다른 이벤트·문맥에 같은 requestId를 재사용하면409 EVENT_EXPORT_REQUEST_CONFLICT다.
미디어 카메라·서버·피트 설정이 달라지면409 MEDIA_CLIP_SOURCE_CHANGED로 막는다.
현재 배포처럼 단일 백엔드 worker를 사용한다.

실패한 파일을 다시 추출하려면 같은 POST 본문에 `retryFailed=true`를 전달한다.
재시도 의도도 DB에 저장하여 응답이 끊겨도 유지한다. 미디어는 failed 구간 작업만 다시 시도하며
clipId는 유지한다. ready/processing/deleted/expired는 기존 작업을 반환한다.
삭제·만료 파일을 자동으로 되살리지 않는다. 새 파일은 새 requestId로 요청한다.
아직 등록하지 않은 requestId의 retryFailed=true는404다.
상태 GET은 새 파일 생성이나 실패 재시도를 요청하지 않는다.

대표 오류: 401 로그인 필요, 403 CSRF 불일치, 404 이벤트/요청/구간 영상 없음,
409 미확정 구간·미디어 처리 중·중복 충돌·출처 변경, 422 본문/길이 오류,
503 저장 공간·작업 한도·미설정/접속 불가, 504 응답 시간 초과.
로컬 검증 오류는 `detail`, 미디어 오류는 `error.code` 구조다.
부분 영상은 partial로 표시하며 누락을 전체 구간 확보로 바꾸지 않는다.

## 화면 연결

기존 상세 응답의 null downloadPath만으로 다운로드 버튼을 영구 비활성화하면 새 기능을 쓸 수 없다.
`exportPath`가 있는 경우 명시 요청 → 상태 조회 → ready 경로 다운로드로 연결한다.
조회에 쓴 before_seconds/after_seconds를 POST의 beforeSeconds/afterSeconds에도 동일하게 전달한다.
다른 사람의 요청 상태를 재사용하지 않고, 새로고침 복구에는 해당 사용자의 requestId/statusPath를 저장한다.
백엔드 추가만으로 기존 프론트 요청 방식이 자동 변경되지는 않는다.

준비된 파일의 코덱·해상도·FPS는 [영상 파일 정보 조회](RECORDING_MEDIA_INFO.md)를 사용한다.

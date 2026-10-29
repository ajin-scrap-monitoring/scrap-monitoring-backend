# 기존 이벤트 시각 기준 과거 영상 요청

관리자는 `POST /api/v1/media/clips`에 `use_event_time:true`를 지정해 저장된 이벤트 시각을
기준으로 기존 상시 녹화에서 클립을 추출하도록 요청할 수 있다. 로그인 관리자와 CSRF가 필요하다.
기존 event_id/camera_id 계약과 기본 접수 시각 방식은 유지한다.

```json
{"event_id":"evt-alt-123","camera_id":"camera-example","use_event_time":true}
```

카메라 ID는 실제 설정값을 사용한다. 위 ID는 합성 예시다. 응답202는 접수이며 재생 완료가 아니다.

## 지원 범위

- 현재 이 제어 API가 지원하는 저장된 적재율 알림(evt-alt)·수동 수거 완료 확인(evt-col)에 적용.
  설정된 피트의 실제 기록만 허용하며 존재하지 않는 ID·다른 피트는404다.
- `use_event_time`은 엄격한 JSON boolean, 기본false. true일 때 서버가 기록의 triggered_at 또는
  completed_at을 선택한다. 클라이언트가 임의 event_at/start_at/end_at을 보내면422다.
- 원본 시각은 DB에서 유지한다. 미디어 요청만 UTC·밀리초3자리·Z로 맞추며1ms 미만 소수는 버린다.
- 미디어에는 camera_id/event_id/event_at만 전송한다. use_event_time이나 내부 요청 포인터를 전송하지 않는다.
- 전후 구간은 미디어의 최초 접수 당시 녹화 정책 pre_roll_seconds/post_roll_seconds다.
  임의 시작~종료 구간, 자동 수거 시작~종료 전체 구간, 오류 이벤트의 새 추출 API는 지원하지 않는다.
  해당 가변 구간 계약은 별도 필요하다. 기존 날짜 영상 공유 재생과 가상 녹화 상세는 유지한다.
- 추출은 별도 MP4 파일과 저장 공간을 사용한다. 관리자 명시 요청에서만 실행하며
  MEDIA_V2_EVENT_CLIPS_ENABLED를 켜거나 모든 과거 이벤트를 자동으로 추출하지 않는다.
- 미래 시간·원본 없음·준비 중·부분 확보는 미디어의 실제 응답으로 판단한다.
  과거 원본이 없다고 현재 시각 촬영으로 바꾸거나 빈 영상을 성공 처리하지 않는다.
- 백엔드는 영상 바이트를 수신·중계하지 않는다. 완료 파일 접근은 기존 인증 게이트웨이를 사용한다.

## 응답·재시도

- 과거 추출 접수/상세에는 event_at이 포함된다. accepted_at(최초 접수 시각)과 구분한다.
  기존 방식의 응답에는 event_at:null을 새로 끼워 넣지 않는다.
- `/api/v1/media/clip-jobs`의 time_basis는 ACCEPTED_AT/EVENT_OCCURRED_AT이다.
  미접수 작업의 clip은null이며 내부 요청 메타데이터를 브라우저에 노출하지 않는다.
- 요청 전에 기존 media_clip_jobs 행에 기준 시각·버전·출처 해시를 보존한다.
  응답 유실·재시도·재시작 후 같은 ID와 시각을 사용한다. 새 테이블·마이그레이션은 없다.
- 같은 카메라/이벤트의 기존 작업은 요청 기준을 바꿀 수 없다. 반대 use_event_time 요청은
  409 CLIP_TIME_BASIS_CONFLICT. 같은 기준으로 재요청해야 하며 기존 파일을 재촬영하거나 덮어쓰지 않는다.
- 카메라/피트/미디어 출처 변경은409 MEDIA_CLIP_SOURCE_CHANGED, 요청 포인터 불일치는
 409 MEDIA_TIME_CONTEXT_INVALID로 차단한다. 과거 요청을 새 출처로 보내지 않는다.
- 접수·상세의 event_at이 저장된 기준과 다르거나 필요한 event_at이 없으면
 502 MEDIA_TIME_BASIS_MISMATCH. 다른 시각의 파일 ID를 해당 이벤트에 붙이지 않는다.
- RECORDING_NOT_FOUND_AT_TIME(404), EVENT_TIME_IN_FUTURE(400)는 그대로 실패로 보존한다.
  RECORDING_NOT_READY(409)는 준비 상태로 재시도하고 RECORDING_NOT_ENABLED(503) 등은 실제 오류로 남긴다.
  오류 코드만 전달하며 원격 오류 문구·내부 주소·토큰을 전달하지 않는다.

## 이전 버전 운용

이 모드로 만든 대기 작업은 내부 요청 메타데이터를 이해하는 버전에서 처리해야 한다.
지원 전 버전으로 내리기 전에 해당 모드의 대기·처리 중 요청을 확인하고 작업자 처리와 함께
조정해야 한다. 구버전 작업자가 새 포인터를 무시하면 접수 시각 방식으로 잘못 요청할 수 있다.
데이터나 영상을 삭제하는 복구 절차가 아니며 이 문서 자체가 실제 복구 실행을 뜻하지 않는다.

현재 운영 검증에서는 실제 클립 생성·영상 삭제·전역 녹화 정책 변경을 실행하지 않는다.
미디어의 배포 계약 확인과 격리 합성 HTTP 검증을 실제 현장 파일 추출 완료로 확대하지 않는다.

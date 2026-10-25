# 상시 녹화의 이벤트 구간 재생

상시 녹화 파일은 한 번만 저장한다. 새 API는 기존 수거·알림·오류의 UTC 시각과 날짜 영상의 시간축을 연결하는 JSON 조회다. 영상 생성·복사·인코딩·MP4 준비 요청이나 새 DB 기록을 만들지 않는다. 영상 바이트는 기존 인증 게이트웨이에서 브라우저로 전달한다.

## API

로그인 세션이 필요하고 모든 응답은 `Cache-Control: no-store`다. 현재 `MEDIA_PIT_ID`와 `MEDIA_CAMERA_ID`의 명시적 연결을 사용한다. 다른 피트는404, 미디어 주소 미설정은503이다. 목록은 미디어 서버에 요청하지 않고 DB 메타데이터만 조회한다.

- `GET /api/v2/recordings/events`: 이벤트 구간 목록.
- `GET /api/v2/recordings/events/{event_id}`: 해당 구간에서 실제 재생 가능한 날짜 영상·재생 위치 조회.

목록 쿼리는 pit_id, category=all/collection/alert/error, from/to(시간대 포함 ISO 8601), page/pageSize다. 기본 최근7일·20건, 최대90일·100건이다. 조회 구간은 시작 포함·종료 제외이며 알려진 영상 참조 구간과 겹치는 이벤트를 반환한다. 동일 시각은 eventId 역순으로 정렬한다. 진행 상태·늦은 기록 반영으로 조회 사이 결과가 바뀔 수 있다.

`before_seconds/after_seconds`는 알림·오류·수동 완료 확인의 문맥 구간이며 기본30초/30초, 각각0~300초, 합계1초 이상이다. 현재 조회의 문맥 길이이며 과거 녹화 정책을 복원한 값이 아니다. 자동 수거에는 적용하지 않고 실제 보관된 시작 감지~종료 감지 시각을 사용한다.

## 이벤트 구간

- `eventId`는 기존 이벤트 식별자이며 새 영상 파일 ID가 아니다.
- 자동 수거는 `evt-auto-col-start-{시작기록ID}`. 같은 피트·현장·장치·detectionId·감지 기준의 종료만 연결한다.
- 종료가 없으면 현재 유효한 감지 상태가 같은 수거를 진행 중으로 확인한 경우에만 windowState=OPEN이다. 단절·기준 변경 등으로 끝을 확인할 수 없으면 UNKNOWN/재생 불가로 남는다. 임의 종료 시각을 만들지 않는다.
- 과거 종료만 있고 시작 기록이 없는 수거는 전체 구간을 복원하지 않는다. 기존 이력의 종료 기록은 그대로 유지한다.
- 알림은 evt-alt, 수동 수거 완료는 evt-col, 기존 측정 오류는 evt-err, 장치·입력 상태 변화는 evt-v2-transition 식별자를 사용한다.
- 오류 관련 목록은 이력의 중단·복구·준비 상태도 원래 stateAtOccurrence와 제목으로 구분한다. 준비나 복구를 새 물리적 고장으로 표시하지 않는다.
- windowKind는 COLLECTION_CYCLE 또는 EVENT_CONTEXT, windowState는 CLOSED/OPEN/UNKNOWN/POINT다. simulated=null은 출처 미보관이며 false로 채우지 않는다. 과거 피트가 미보관된 변화는 현재 장치 연결 기준 CURRENT_ASSIGNMENT를 유지한다.
- 카메라-피트 연결은 현재 설정 기준이다. 카메라 재배치 이전의 물리적 관측 영역까지 검증한 의미가 아니다.

## 재생 응답

`source=DAILY_RECORDING`, event, asOf, effectiveTo와 다음 정보를 제공한다.

| 필드 | 의미 |
| --- | --- |
| state | AVAILABLE: 요청 구간 전체 확인, PARTIAL: 일부 재생 가능, PROCESSING: 확보 여부/미래 구간 대기, UNAVAILABLE: 재생 근거 없음 |
| requestedSeconds | 요청한 실제 시각 구간 길이. 종료 불명은 null |
| coveredSeconds | 재생 근거로 확인한 실제 시각 구간의 합집합 길이 |
| playbackSeconds | 재생할 부분들의 영상 재생 시간 합계. 실제 시각 길이와 다를 수 있음 |
| parts | 실제 날짜 recordingId/recordingDate, 인증된 playbackPath, fromAt/toAt, startSeconds/endSeconds |
| unavailableIntervals | 확인된 GAP·확보 여부 미확인 UNCONFIRMED·FUTURE·MISSING·EXPIRED 구간과 사유 |
| downloadPath | 현재 null. 이벤트 구간 MP4 추출은 미지원 |

각 part는 같은 날짜의 기존 HLS를 사용한다. 재생 위치는 **available_intervals의 대응 관계**로 계산한다. 영상 누락 때문에 자정부터의 초와 이어 붙인 영상 재생 초가 다를 수 있다. 실제 시각을 자정에 단순히 더하거나 빼서 seek하지 않는다. 자정을 넘으면 여러 날짜의 실제 영상에 순서대로 연결한다.

원본 시각이 같은 점 구간은 mapping=POINT_TIMESTAMP로 재생 범위를 보존한다. 실제 시각 길이를 임의로 늘리지 않으며, 해당 점만으로 연속 구간 전체가 확보됐다고 표시하지 않는다. 확인된 누락과 확보 여부가 미확인인 구간, 아직 오지 않은 미래 구간을 구분한다. 메타데이터가 모순되면 해당 부분을 재생 가능으로 확정하지 않는다.

한 이벤트의 상세 조회는 최대7개 한국 날짜, 최대2000개의 재생·미확보 구간으로 제한한다. 초과 시 범위를 잘라 전체처럼 반환하지 않는다. 미디어 오류는 기존 안전한502/503/504 계약을 따르고 내부 주소·오류 원문·토큰을 노출하지 않는다.

## 프론트 연결

녹화 탭의 수거·알림·오류는 새 목록 API를 사용하고, 항목 선택 시 상세를 조회한다. 기존 날짜 HLS 플레이어를 재사용해 part.startSeconds에서 재생하고 endSeconds에서 정지하거나 다음 part로 이동한다. 다운로드 URL이나 전체 수거 영상이 없으면 만들어내지 않는다. 표시 시간대는 Asia/Seoul이며 서버 UTC 원문은 유지한다. 새 API 추가만으로 기존 프론트의 요청·플레이어 선택이 자동으로 바뀌지는 않는다.

기존 `/api/v1/recordings`와 실제 클립의 recordingId 의미는 유지한다. 새 eventId를 실제 미디어 UUID인 것처럼 넣지 않는다. 상시 영상 공유 방식에서는 `MEDIA_V2_EVENT_CLIPS_ENABLED=false`를 유지한다. 이 설정을 켜면 이전 선택 기능이 별도 클립을 만들 수 있다.

## 다운로드와 저장량

이벤트 구간만 MP4로 내보내려면 사용자가 다운로드할 때 미디어 서비스에 구간 추출을 요청하는 기능이 추가로 필요하다. 현재 하루 MP4 준비 API를 이벤트 구간 다운로드로 대신 제공하지 않는다. 응답은 downloadReasonCode=EVENT_RANGE_EXPORT_UNSUPPORTED다. 백엔드가 영상 바이트를 읽어 잘라내거나 상시 영상을 이중 저장하지 않는다.

저장 용량 문제로 애초에 확보하지 못한 영상은 이 연결로 복원되지 않는다. 실제 API의 저장·누락 메타데이터를 그대로 판단하며, 날짜만으로 영상 유무를 단정하지 않는다.

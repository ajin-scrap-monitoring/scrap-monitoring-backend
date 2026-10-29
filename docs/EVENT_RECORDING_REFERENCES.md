# 상시 녹화의 이벤트 구간 재생

상시 녹화 파일은 한 번만 저장한다. 새 API는 기존 수거·알림·오류의 UTC 시각과 날짜 영상의 시간축을 연결하는 JSON 조회다. 영상 생성·복사·인코딩·MP4 준비 요청이나 새 DB 기록을 만들지 않는다. 영상 바이트는 기존 인증 게이트웨이에서 브라우저로 전달한다.

## API

로그인 세션이 필요하고 모든 응답은 `Cache-Control: no-store`다. 현재 `MEDIA_PIT_ID`와 `MEDIA_CAMERA_ID`의 명시적 연결을 사용한다. 다른 피트는404, 미디어 주소 미설정은503이다. 기본 목록은 미디어 서버에 요청하지 않고 DB 메타데이터만 조회한다. 선택적 재생 요약은 아래 계약을 따른다.

- `GET /api/v2/recordings/events`: 이벤트 구간 목록.
- `GET /api/v2/recordings/events/{event_id}`: 해당 구간에서 실제 재생 가능한 날짜 영상·재생 위치 조회.

목록 쿼리는 pit_id, category=all/collection/alert/error, from/to(시간대 포함 ISO 8601), page/pageSize다. 기본 최근7일·20건, 최대90일·100건이다. 조회 구간은 시작 포함·종료 제외이며 알려진 영상 참조 구간과 겹치는 이벤트를 반환한다. 동일 시각은 eventId 역순으로 정렬한다. 진행 상태·늦은 기록 반영으로 조회 사이 결과가 바뀔 수 있다.

마지막 페이지를 넘기거나 조회할 이벤트가 없으면 요청한 페이지 정보와 전체 건수를 유지하고
`items=[]`를 반환한다. 큰 페이지 번호도 같은 규칙을 적용하며, 빈 페이지는 이벤트 행 조회와
선택적 미디어 재생 요약 조회를 생략한다.

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

### 이력에서 영상으로 이동

`GET /api/v1/events`의 각 항목은 nullable `recordingEventId`를 제공한다. 값이 있으면
`GET /api/v2/recordings/events/{recordingEventId}`로 상세를 조회한다. 자동 수거 이력의
`id=evt-auto-col-{종료기록ID}`를 녹화 ID로 사용하거나 숫자로 시작 기록을 추측하지 않는다.
백엔드는 같은 현장·장치·피트·detectionId·scopeToken의 시작 기록과 최초 종료 시각을 대조한다.
시작이 없거나 시작·종료 연결이 모호하면 `recordingEventId=null`이다. 과거 감지 기준을
포함한 이력도 각 기록에 보관된 기준으로 연결하며, 이력 조회 기간 이전의 시작도 찾는다.

알림·수동 완료·오류는 녹화 이벤트 조회에서도 확인되는 경우 이력 ID와 같은 값을 제공한다.
현재 피트에 미디어가 설정되지 않았으면 이력 조회는 정상 응답하고 연결 필드는 null이다.
이력 페이지에 포함된 항목만 DB에서 연결을 확인하며 미디어 조회나 클립 생성은 하지 않는다.

`recordingEventId`는 이벤트 메타데이터의 연결 근거다. 실제 영상의 확보·삭제·만료 여부를
보장하지 않으므로 상세의 `state`, `parts`, `reasonCodes`를 확인한다. 기존 `recordingId`는
별도 클립의 실제 미디어 UUID를 유지하며 두 필드를 서로 대체하지 않는다.

## 재생 응답

`source=DAILY_RECORDING`, event, asOf, effectiveTo와 다음 정보를 제공한다.

| 필드 | 의미 |
| --- | --- |
| state | AVAILABLE: 요청 구간 전체 확인, PARTIAL: 일부 재생 가능, PROCESSING: 확보 여부/미래 구간 대기, UNAVAILABLE: 재생 근거 없음 |
| requestedSeconds | 요청한 실제 시각 구간 길이. 종료 불명은 null |
| coveredSeconds | 재생 근거로 확인한 실제 시각 구간의 합집합 길이 |
| playbackSeconds | 재생할 부분들의 영상 재생 시간 합계. 실제 시각 길이와 다를 수 있음 |
| parts | 실제 날짜 recordingId/recordingDate, 인증된 playbackPath, fromAt/toAt, startSeconds/endSeconds, relativeStartSeconds/relativeEndSeconds |
| unavailableIntervals | 확인된 GAP·확보 여부 미확인 UNCONFIRMED·FUTURE·MISSING·EXPIRED 구간과 사유 |
| downloadPath | 현재 null. 이벤트 구간 MP4 추출은 미지원 |

각 part는 같은 날짜의 기존 HLS를 사용한다. 재생 위치는 **available_intervals의 대응 관계**로 계산한다. 영상 누락 때문에 자정부터의 초와 이어 붙인 영상 재생 초가 다를 수 있다. 실제 시각을 자정에 단순히 더하거나 빼서 seek하지 않는다. 자정을 넘으면 여러 날짜의 실제 영상에 순서대로 연결한다.

원본 시각이 같은 점 구간은 mapping=POINT_TIMESTAMP로 재생 범위를 보존한다. 실제 시각 길이를 임의로 늘리지 않으며, 해당 점만으로 연속 구간 전체가 확보됐다고 표시하지 않는다. 확인된 누락과 확보 여부가 미확인인 구간, 아직 오지 않은 미래 구간을 구분한다. 메타데이터가 모순되면 해당 부분을 재생 가능으로 확정하지 않는다.

한 이벤트의 상세 조회는 최대7개 한국 날짜, 최대2000개의 재생·미확보 구간으로 제한한다. 초과 시 범위를 잘라 전체처럼 반환하지 않는다. 미디어 오류는 기존 안전한502/503/504 계약을 따르고 내부 주소·오류 원문·토큰을 노출하지 않는다.

## 프론트 연결

녹화 탭의 수거·알림·오류는 새 목록 API를 사용하고, 항목 선택 시 상세를 조회한다. 기존 날짜 HLS 플레이어를 재사용해 part.startSeconds에서 재생하고 endSeconds에서 정지하거나 다음 part로 이동한다. 다운로드 URL이나 전체 수거 영상이 없으면 만들어내지 않는다. 표시 시간대는 Asia/Seoul이며 서버 UTC 원문은 유지한다. 새 API 추가만으로 기존 프론트의 요청·플레이어 선택이 자동으로 바뀌지는 않는다.

### 구간 기준 재생바

`startSeconds/endSeconds`는 원본 날짜 HLS에서 이동할 위치다. `relativeStartSeconds/relativeEndSeconds`는 **선택한 이벤트의 확보된 영상만 순서대로 이어 붙인 0초 기준 위치**다. 첫 part는0초로 시작하고 마지막 relativeEndSeconds는 playbackSeconds와 같다. 자정에 날짜 영상이 바뀌어도 이벤트 재생바는 이어진다. 이 필드는 JSON 메타데이터이며 영상 파일을 생성하거나 복사하지 않는다.

합성 예: 오전07:00부터 자정까지17시간 녹화한 영상에서14:13:20~14:13:30을 선택한 경우 다음 재생 정보를 사용한다.

```json
{
  "playbackSeconds": 10,
  "parts": [{
    "startSeconds": 26000,
    "endSeconds": 26010,
    "relativeStartSeconds": 0,
    "relativeEndSeconds": 10
  }]
}
```

위 예의 재생바는 `0:00 / 0:10`이며 촬영 시각은 별도로 표시한다. 원본 video.duration이나 자정부터 경과한 초를 구간 길이로 사용하지 않는다. 날짜 영상을 video 요소에 연결하는 것만으로 기본 재생바가10초로 바뀌지는 않는다. 프론트는 구간 기준 재생바와 탐색 처리를 연결한다.

- 현재 구간 시간: `relativeStartSeconds + (video.currentTime - startSeconds)`. 표시값은 해당 part 범위 안으로 제한한다.
- 탐색: 사용자 위치를 `[0, playbackSeconds]`로 제한하고, 해당 위치를 포함하는 part를 찾는다. 원본 이동 위치는 `startSeconds + (사용자 위치 - relativeStartSeconds)`다. 경계에서는 다음 part를 선택하며, 마지막 끝에서는 정지한다.
- 재생: 날짜 HLS 준비 후 해당 startSeconds로 이동한다. endSeconds에 도달하면 다음 part의 시작으로 이동하거나 마지막이면 일시 정지한다. 재생·탐색·다시 재생 모두 구간 밖으로 이동하지 않도록 처리한다. 실제 화면의 제어 구현이 필요하며 API가 브라우저를 자동으로 정지시키지는 않는다.
- 누락: 총60초 중10초가 없으면 확보된 영상의 재생바는50초다. 실제 요청 길이 requestedSeconds와 누락 표시를 함께 유지한다. 없는10초를 영상으로 채우거나 실제 시각이 연속인 것처럼 표시하지 않는다.
- 진행 중: OPEN은 asOf까지의 임시 구간이며 조회를 갱신하면 길이가 늘어날 수 있다. PARTIAL의 POINT_TIMESTAMP도 원래 재생 길이를 보존하며 연속 촬영 시각으로 늘리지 않는다.

### 100건을 넘는 이벤트

pageSize 최대100은 한 번의 요청 크기이며 전체 이벤트 개수의 상한이 아니다. page를1부터 page.totalPages까지 바꿔 조회한다. 기본20건 또는 화면 크기에 맞는 페이지를 사용하고, 목록 조회 때 모든 상세/HLS를 미리 가져오지 않는다. 사용자가 선택한 eventId의 상세만 요청한다. 동일한 날짜 recordingId를 수백 개 이벤트가 공유할 수 있다.

첫 응답의 from/to와 category·pit_id·문맥 길이·pageSize를 후속 페이지에도 유지한다. 기간이나 필터를 바꾸면1페이지부터 다시 조회한다. 목록은 실시간 DB 조회이므로 늦은 기록 도착과 상태 변경을 포함하는 고정 스냅샷은 아니다. 갱신할 때 eventId로 선택을 유지하고, 단순 누적 목록이면 중복을 제거한 뒤 다시 조회한다. 목록 건수는 영상 전체가 확보된 건수가 아니며 각 항목의 상세 state를 확인한다.

기존 `/api/v1/recordings`와 실제 클립의 recordingId 의미는 유지한다. 새 eventId를 실제 미디어 UUID인 것처럼 넣지 않는다. 상시 영상 공유 방식에서는 `MEDIA_V2_EVENT_CLIPS_ENABLED=false`를 유지한다. 이 설정을 켜면 이전 선택 기능이 별도 클립을 만들 수 있다.

## 다운로드와 저장량

이벤트 구간만 MP4로 내보내려면 사용자가 다운로드할 때 미디어 서비스에 구간 추출을 요청하는 기능이 추가로 필요하다. 현재 하루 MP4 준비 API를 이벤트 구간 다운로드로 대신 제공하지 않는다. 응답은 downloadReasonCode=EVENT_RANGE_EXPORT_UNSUPPORTED다. 백엔드가 영상 바이트를 읽어 잘라내거나 상시 영상을 이중 저장하지 않는다.

저장 용량 문제로 애초에 확보하지 못한 영상은 이 연결로 복원되지 않는다. 실제 API의 저장·누락 메타데이터를 그대로 판단하며, 날짜만으로 영상 유무를 단정하지 않는다.


## 목록 재생 요약

`GET /api/v2/recordings/events?include_playback=true`로 현재 페이지의 각 항목에
`playback`을 함께 요청할 수 있다. 기본값 false에서는 `playback: null`이며 미디어 조회가 없다.
기존 필터·페이지·문맥 길이를 그대로 적용한다. `playback`은 다음 구조다.

```json
{"state": "PARTIAL", "playbackSeconds": 10.0, "reasonCodes": ["COVERAGE_UNCONFIRMED"]}
```

- 정상 계산 시 state는 상세와 같은 AVAILABLE/PARTIAL/PROCESSING/UNAVAILABLE이다.
  playbackSeconds는 확보된 재생 구간의 합이며 실제 촬영 시각 차이나 날짜 전체 길이가 아니다.
  0초와 null을 구분한다. 부분 재생·진행 중·시각이 한 점인 표본도 상세와 같은 규칙을 사용한다.
- 미디어 응답 실패·잘못된 메타데이터·처리 한도로 확인하지 못하면 UNKNOWN,
  playbackSeconds=null 및 reasonCodes를 반환한다. 이를 ‘영상 없음’으로 표시하지 않는다.
  이벤트 목록·건수·페이지는 유지된다. 원격 오류 원문이나 내부 주소는 전달하지 않는다.
- 요청별로 같은 한국 날짜의 메타데이터를 공유한다. 연속 날짜는 하나의 조회 범위로 묶고,
  서로 떨어진 날짜 사이의 전체 기간을 불필요하게 읽지 않는다. 항목마다 상세 API를 호출하지 않는다.
  전역 캐시는 없으므로 삭제·녹화 진행 상태는 다음 요청에서 다시 확인한다.
- 한 페이지에서 최대7개 한국 날짜, 미디어 요청 최대8회(페이지 넘김 포함),
  메타데이터의 가용 구간+누락 구간 합계20000개, 미디어 조회 전체10초를 적용한다.
  날짜는 목록 순서대로 배정한다. 날짜 한도 초과 항목은 PLAYBACK_SUMMARY_DATE_LIMIT,
  요청/메타데이터 한도는 PLAYBACK_SUMMARY_LIMIT, 시간 초과는 PLAYBACK_SUMMARY_TIMEOUT이다.
  이벤트와 겹치는 구간만 비교하며 페이지 전체의 구간 탐색·비교 작업은200000단위로 제한하며 초과 항목은 PLAYBACK_SUMMARY_WORK_LIMIT이다.
  상세의2000구간 한도에 걸린 요약도 UNKNOWN/EVENT_REPLAY_SEGMENT_LIMIT이다.
  불완전한 미디어 페이지에서 누락된 날짜를 영상 없음으로 확정하지 않는다.
- 요약은 해당 목록 응답의 asOf 기준이다. 선택 후 상세에서 최신 parts와 재생 상태를 다시 확인한다.
  목록에는 HLS 경로·parts를 추가하지 않는다. 긴 기간의 여러 날짜가 한 페이지에 섞여 UNKNOWN이
  많으면 날짜 범위를 좁히거나 선택 항목의 상세를 조회한다.

추가 클립·파일·DB 작업을 만들지 않으며 영상 바이트를 중계하지 않는다.

# 카메라 프레임 수신 상태

로그인 세션으로 `GET /api/v2/monitoring/camera-status?pit_id=pit-example`을 조회한다.
`GET /api/v2/monitoring/active-alerts?scope=ALL`에도 카메라 확인 범위와 현재 알림을 포함한다.
두 API는 같은 앱의 짧은 조회 캐시를 사용하며 HTTP 응답은 `Cache-Control: no-store`다.

| state | 의미 |
| --- | --- |
| `HEALTHY` | 현재 연결에서 JPEG 프레이밍 검사를 통과한 프레임의 수신 근거가 유효함 |
| `INTERRUPTED` | 미디어 서비스가 프레임 지연·중단·연결 종료·잘못된 입력을 확인함 |
| `UNKNOWN` | 관측 전/첫 프레임 대기, 원천 오류·만료·시간 불일치 등으로 현재 상태를 확정할 수 없음 |
| `UNSUPPORTED` | 해당 피트에 연결된 카메라 상태 원천이 설정되지 않음 |

`statusBasis=ACCEPTED_JPEG_RECEIPT`는 전체 JPEG 디코딩·화질이나 카메라 하드웨어 자체
진단이 아니다. `lastFrameReceivedAt`은 서버 프레임 수신 시각이며 원본 촬영 시각이 아니다.
`connectionState`는 `CONNECTED/DISCONNECTED/UNKNOWN`이다.

## 유효 시각과 조회 비용

`assessedAt`은 원천의 평가 시각, `computedAt`은 백엔드가 응답을 계산한 시각이다.
`validUntilAt`과 `validForSeconds`는 원천의 유효 범위와 요청 지연을 반영해 백엔드가
보수적으로 제한한 수명이며 최대 5초다. 원천 장애로 확인하지 못하면 null이다.
브라우저는 약 2초 간격으로 조회하고 유효 시각 이후 이전 정상 표시를 계속 사용하지 않는다.

백엔드는 요청이 있을 때만 조회한다. 앱 프로세스별 캐시는 최대 2초이며 원천 유효 시각이
더 짧으면 그 시각까지만 사용한다. 같은 평가 시각을 반복 수신해도 단조 시계 기준 수명을
연장하지 않는다. 벽시계 역행이나 출처 변경 때 이전 정상 응답을 재사용하지 않는다.
오류도 짧게 캐시하고 동시 요청은 한 번의 미디어 조회를 공유한다.

미디어 요청 전체 예산은 1초, 조회 잠금 대기를 포함한 카메라 단계 예산은 1.25초다.
응답 JSON은 최대 16KiB이며 리다이렉트·압축 응답·영상 본문은 수용하지 않는다.
소스는 기존 `MEDIA_SERVICE_URL`을 우선 사용하고, 없으면 `MEDIA_DAILY_SERVICE_URL`의
호스트에서 `/api/v1`을 사용한다. 설정된 `MEDIA_PIT_ID/MEDIA_CAMERA_ID`만 연결하며
브라우저 쿠키나 인증 헤더를 미디어에 전달하지 않는다. 기존 클립 API 인증은 유지한다.

상태 원천의 실패도 200과 `UNKNOWN`으로 제공해 다른 알림 조회를 중단하지 않는다.
인증·피트·쿼리 검증 오류는 기존 401/404/422를 유지한다. `reasonCodes`에는
`CAMERA_STATUS_TIMEOUT/UNAVAILABLE/NOT_FOUND/BAD_RESPONSE/EXPIRED/CLOCK_UNTRUSTED/IDENTITY_MISMATCH/SOURCE_CHANGED/BUSY`
등 안전한 사유를 사용하며 원격 메시지나 내부 주소를 반환하지 않는다.

## 현재 활성 알림

`scope=ALL`의 CAMERA coverage는 유효한 정상·중단 근거가 있으면 `CHECKED`, 현재 판단이
불가능하면 `UNKNOWN`, 미설정이면 `UNSUPPORTED`다. 평가·유효 시각과 확인 가능한 경우
`evidenceSource=MEDIA_CAMERA_STATUS`를 제공한다. `CHECKED`는 정상이라는 뜻이 아니며
함께 반환된 CAMERA 알림을 확인한다.

중단은 error, 미확인은 warning인 `device_error`로 표시한다. 원천 평가를 확인했으면
`evidenceSource=MEDIA_CAMERA_STATUS`, 조회 실패·만료는 `STATUS_FRESHNESS`를 사용한다.
장애 시작 시각은 원천에 없으므로 `startedAt=null/startBasis=UNKNOWN`을 유지한다.
마지막 프레임 시각이나 최초 GET 시각을 장애 시작으로 바꾸지 않는다.

적재율의 `evaluationState/basis`는 기존 적재율 판정을 유지한다. 카메라는 별도로 평가되므로
동일 응답의 적재율 snapshot 시점과 다를 수 있다. 기본 `scope=FILL_OPERATIONS`, snapshot,
SSE, 센서 수신·판정 작업자는 카메라 HTTP 조회를 추가하지 않는다.
새 알림 이력·영상·DB 행을 생성하거나 읽음 상태를 바꾸지 않으며 실제 이메일/SMS도 발송하지 않는다.

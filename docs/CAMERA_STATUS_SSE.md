# SSE로 카메라 상태 받기

`GET /api/v2/monitoring/events?pit_id=pit-demo&include_camera_status=true`

기존 현황 실시간 연결에 카메라 상태 이벤트를 선택적으로 추가한다. 기본값은 false이며,
기존 URL의 snapshot·keepalive·인증·세션 만료 동작은 유지한다.

| 이벤트 | 내용 |
| --- | --- |
| `monitoring.snapshot` | 기존 적재율·판정 snapshot. 연결 직후 먼저 전달 |
| `monitoring.camera-status` | [카메라 상태 GET](CAMERA_STATUS.md)과 같은 `CameraStatusV2` 객체 |
| `session.expired` | 기존 로그인 세션이 끝남. 스트림 종료 |

카메라 정보는 조회 완료 후 전달한다. 카메라 응답 대기 중에도 snapshot과 keepalive를
전송한다. 상태가 같아도 유효 시간을 갱신하도록 새 조회 결과를 전달한다. 카메라 정보는
snapshot 안에 들어가지 않으며 적재율 판정과 같은 측정 시각을 공유하지 않는다.

## 프론트엔드 연결

```javascript
const stream = new EventSource(
  '/api/v2/monitoring/events?pit_id=pit-demo&include_camera_status=true',
  { withCredentials: true },
);
stream.addEventListener('monitoring.snapshot', (event) => renderSnapshot(JSON.parse(event.data)));
stream.addEventListener('monitoring.camera-status', (event) => renderCameraStatus(JSON.parse(event.data)));
stream.addEventListener('session.expired', () => stream.close());
```

위 ID와 함수명은 연결 예다. 기존 카메라 표시는 GET과 같은 필드를 사용한다.
새 이벤트를 연결한 뒤 기존 상시 2초 카메라 GET 타이머를 제거한다. 연결 장애 때 임시 GET
대체 조회를 사용한다면 SSE 복구 후 중단한다. 페이지를 떠날 때 `EventSource.close()`를
호출하고 `session.expired` 때는 재인증한다. 백엔드 추가만으로 화면 연결이 바뀌지는 않는다.

## 유효 시간

`lastFrameReceivedAt`은 서버가 JPEG 프레임을 받은 시각이며 원본 촬영 시각이 아니다.
`validUntilAt`과 최대 5초인 `validForSeconds`를 따른다. 전송 직전 남은 시간을 다시 계산하므로
느린 수신자가 오래된 결과를 받았다고 유효 시간이 연장되지 않는다. 새 이벤트가 없거나
연결이 끊겨 기한이 끝나면 확인 불가로 표시하고, 마지막 HEALTHY를 무기한 유지하지 않는다.

- 전송 전에 만료: `UNKNOWN / CAMERA_STATUS_EXPIRED`.
- 벽시계·단조 시계 역행: `UNKNOWN / CAMERA_STATUS_CLOCK_UNTRUSTED`.
- 설정된 카메라 연결 변경: `UNKNOWN / CAMERA_STATUS_SOURCE_CHANGED`.
- 미디어 오류: 기존 GET의 UNKNOWN 사유. 예상하지 못한 읽기 오류도
  `CAMERA_STATUS_UNAVAILABLE`로 격리하며 적재율 스트림을 종료하지 않는다.
- 카메라 미설정: `UNSUPPORTED`. 미디어 HTTP 요청 없음.

## 조회와 자원 정리

카메라 읽기는 완료 시각부터 최소 2초 뒤 다시 시도하며, 준비된 결과는 기존 약 1초 snapshot
확인 주기에 전달한다. 연결마다 대기 또는 실행 중인 읽기 하나만 유지하고 이벤트를 쌓지 않는다.
같은 프로세스의 GET·활성 알림·여러 SSE는 기존 2초 캐시와 조회 잠금을 공유한다.
현재 단일 worker 기준이며 분산 캐시나 연결과 무관한 상시 조회를 추가하지 않는다.

조회 사용자·관리자 모두 세션 쿠키 인증이 필요하다. 알 수 없는 피트는 404, 잘못된 boolean
옵션은 422, 로그인하지 않았으면 401이다. `no-store`, `X-Accel-Buffering: no`와 주기적 세션
검사를 유지한다. 로그아웃·만료 뒤 카메라 이벤트도 더 전달하지 않는다.

연결 종료·취소·세션 종료 때 해당 읽기를 취소하고 기다려 정리한다. 카메라 HTTP 대기 중에는
DB 트랜잭션을 유지하지 않는다. 기존 카메라 응답 크기·시간·출처 검증을 그대로 사용하며,
브라우저 쿠키나 인증 정보를 미디어에 전달하지 않는다. 영상 바이트·녹화 생성·DB 저장·알림
읽음·외부 발송·스키마 변경은 추가하지 않는다.

# 현재 장비·측정 알림

`GET /api/v2/monitoring/active-alerts?pit_id=pit-example&scope=ALL`은 현재 적재율 경보와
Edge·LiDAR 상태 및 측정 입력 이상을 함께 조회한다. 로그인 필요, 응답은 `Cache-Control: no-store`다.
기본 scope=FILL_OPERATIONS는 기존 적재율 경보만 유지한다. 임의 scope는422, 미설정 피트는404다.
과거 오류 이력·미확인 알림·읽음 상태와 별개이며 GET은 DB·외부 서비스에 변경을 만들지 않는다.

## 응답

기존 pitId/computedAt/evaluationState/items/reasonCodes를 유지한다.
`evaluationState`는 적재율 판정 상태다. 장비 상태 전체가 정상이라는 뜻으로 사용하지 않는다.
scope=ALL에서는 coverage로 확인 범위도 제공한다.

- 적재율 항목은 기존 type=pre_collection_alert/collection_required와 loadPercent 등을 유지.
- 장비·측정 항목은 type=device_error/measurement_error, severity, title, componentKind,
  componentId, state, evaluatedAt, measuredAt, evidenceSource, reasonCodes, simulated를 제공.
  componentKind는 EDGE/LIDAR/MEASUREMENT다. 장비 상태의 simulated는 근거 없으면null.
- evidenceSource=EDGE_STATUS는 최신 장치 보고, STATUS_FRESHNESS는 상태 보고 확인 불가,
  CURRENT_MEASUREMENT는 현재 측정 입력 판정이다. 상태 수신 단절을 물리적 고장으로 해석하지 않는다.
- 새 상태 보고가 없거나 보고 시각이 오래됐으면 과거의 센서 고장·정상 판정을 현재 상태로
  재사용하지 않는다. Edge 확인 불가 항목과 확인하지 못한 구성 요소의 coverage를 반환한다.
- LiDAR는 명시적으로 구성한 센서만 제공. 보고 누락·스캔 시각 누락/미래·오래된 스캔을 구분.
  RECOVERING/INITIALIZING/FILTER_WARMING 등 준비 상태를 새 물리적 고장으로 표시하지 않는다.
- coverage는 componentKind/componentId/state/reasonCodes다. CHECKED는 현재 근거를 확인했다는 뜻이며
  정상이라는 뜻이 아니다. UNKNOWN은 현재 확인 불가, UNSUPPORTED는 현재 판단할 입력 계약이 없다는 뜻.
- 카메라는 신뢰할 수 있는 카메라 상태 입력 계약이 없어 UNSUPPORTED와
  CAMERA_HEALTH_SOURCE_NOT_CONFIGURED를 반환한다. HTTP 서비스 응답 성공이나 영상 목록만 보고
  카메라 정상·고장을 만들지 않는다. 카메라 항목까지 구현 완료한 것으로 해석하지 않는다.

## 시작 시각

모든 알림 항목에 nullable startedAt와 startBasis를 추가한다.

- SERVER_EVALUATION: 기존 백그라운드 판정 작업자가 해당 상태를 처음 확인한 서버 UTC 시각.
  최신 판정 시각 evaluatedAt, 원본 측정/보고 시각 measuredAt, 알림 발송 시각과 구분한다.
  물리적 장비 고장이 실제 발생한 시각이나 이전 배포부터 이어진 전체 고장 기간의 증명은 아니다.
- UNKNOWN: 시작 근거 없음. startedAt=null이며 evaluatedAt로 대신 채우지 않는다.
  배포 전 과거 오류의 시작 시각을 추정하거나 과거 이력을 백필하지 않는다.
- 같은 현재 상태·출처·설정·장치 실행 인스턴스가 연속으로 확인되면 시작을 보존한다.
  복구/알림 상태 변경/설정·출처·실행 인스턴스 변경이나 판정 공백이 있으면 새 확인 구간으로 구분.
  오래된 작업자 포인터·다른 피트 포인터는 시작 근거로 사용하지 않는다.
- 작업자가 비활성화됐거나 아직 해당 상태를 확인하지 않았으면 현재 조회는 가능해도 시작은null일 수 있다.
  적재율 pre_alert_enabled=false는 경고 항목과 시작 추적에서도 반영한다.

포인터는 기존 edge_v2_latest에 소스별 active_alert_starts 한 행으로 저장하며 현재 활성 상태만 담는다.
새 테이블·마이그레이션·과거 상태 전체 스캔은 없다. 작업자의 기존 DB 트랜잭션으로 기록하고
조회는 포인터를 읽기만 한다. 실제 이메일·문자 발송, 클립 생성, 알림 읽음 처리는 수행하지 않는다.

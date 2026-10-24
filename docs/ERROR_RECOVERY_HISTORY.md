# 장치·측정 입력 오류와 복구 이력

기존 `GET /api/v1/events?pit_id=pit-demo&category=error`와 `category=all`에서 저장된 v2 장치 상태 변화와 측정 입력 유효성 변화를 함께 조회한다. 기존 URL·유형·페이지 계약과 UTC 시각을 유지한다. 조회가 새 오류·알림·수거 기록·녹화 작업을 만들지는 않는다.

## 표시 기준

| 원본 변화 | 이력 제목 예 | `stateAtOccurrence` | 의미 |
| --- | --- | --- | --- |
| 장치가 MEASUREMENT_UNAVAILABLE로 변경 | 장치 측정 중단 | UNAVAILABLE | 보고 당시 측정 불가 |
| 장치가 CONFIG_ERROR로 변경 | 장치 설정 오류 | UNAVAILABLE | 보고 당시 설정 오류 |
| DEGRADED와 SCAN_RECOVERING 등 준비 사유 | 장치 측정 재준비 | PREPARING | 다시 측정을 준비하는 상태, 오류 확정과 구분 |
| 다른 DEGRADED 상태 | 장치 성능 저하 | DEGRADED | 보고 당시 일부 기능 저하 |
| HEALTHY로 변경 | 장치 상태 복구 | RECOVERED | 해당 시각의 장치 정상 보고 확인 |
| UNKNOWN으로 변경 | 장치 상태 확인 불가 | UNKNOWN | 고장이 확정됐다는 뜻이 아님 |
| 측정 입력이 무효로 변경 | 측정 입력 사용 불가 | UNAVAILABLE | 입력을 계산에 사용할 수 없음. 물리 센서 고장 확정 아님 |
| 무효 입력의 사유가 FILTER_WARMING 등 | 측정 입력 준비 | PREPARING | 준비 상태를 장애로 단정하지 않음 |
| 측정 입력이 다시 유효로 변경 | 측정 입력 복구 | RECOVERED | 입력 유효성 복구이며 시간 필터 준비 완료를 의미하지 않음 |

준비 여부는 명시된 `SCAN_RECOVERING`, `FILTER_WARMING`, `MEDIAN_WARMING`, `INITIALIZING` 사유로 구분한다. 사유가 없거나 다른 사유이면 준비 상태를 추측하지 않는다. 단순 적재 상태 변경·수거 감지는 오류 목록에 넣지 않는다. 같은 원본에서 장치 보고 변화와 측정 입력 변화가 각각 저장됐다면 두 기록을 유지하며 근거 없이 하나의 장애로 합치지 않는다.

## 응답과 현재 상태의 구분

다음은 기존 Event에 추가되는 선택적 필드다. 기존 수거·경보·측정 테이블 이력의 의미를 이번 변경에서 바꾸지는 않는다.

| 필드 | 의미 |
| --- | --- |
| `eventSource` | 새 연결 항목은 EDGE_V2_TRANSITION |
| `transitionId / transitionKind` | 원본 변화 행 ID와 DEVICE_STATE_CHANGED 또는 MEASUREMENT_VALIDITY_CHANGED |
| `stateAtOccurrence` | 위 표의 기록 당시 상태 |
| `statusBasis` | AT_OCCURRENCE: 현재 활성/해제 상태 조회가 아님 |
| `reasonCodes` | 저장된 원본 사유 코드 |
| `pitAttribution` | RECORDED: 원본에 보관한 피트, CURRENT_ASSIGNMENT: 과거 피트 정보가 없어 현재 장치 연결을 사용 |
| `simulated` | 원본에 명시된 경우만 사용. 과거 미보관·장치 보고의 알 수 없는 출처는 null |

기존 type은 장치 변화에서 device_error, 입력 변화에서 measurement_error를 사용하고 category=error를 유지한다. 제목·severity·stateAtOccurrence로 중단/복구/준비를 구분한다. 준비/복구는 info이고 단순 확인 불가나 입력 불가는 warning이다. 장치가 명시적으로 측정/설정 오류를 보고하면 error다. 새 API 값을 그대로 프론트 제목·상세에 연결할 수 있으며, 시간대·상태 배지 등 화면 표현은 담당자와 맞춘다.

호환 status는 복구 관측 항목에서 resolved, 다른 항목에서 active지만 **기록 당시 상태**로만 해석한다. 복구 항목의 resolvedAt은 그 항목이 관측된 시각이다. 과거 오류 항목에 나중 복구 시각을 붙이거나 두 항목이 같은 장애라고 추정하지 않는다. 현재 장치 상태는 v2 snapshot을 확인한다. 기존 경보의 고정 해제 상태와 실제 경보 수명 기록은 별도 보완 대상이다.

## 범위·보존·연결

현재 피트에 명시적으로 연결된 site/edge의 두 종류 변화만 조회한다. 원본의 pitId가 다른 피트이면 제외하고, 과거 원본에 피트가 없으면 CURRENT_ASSIGNMENT를 표시한다. 과거의 피트 연결을 복원한 것으로 취급하지 않는다. 앞으로 저장하는 두 변화에는 당시 피트를 함께 보존하고, 측정 입력 변화에는 명시된 simulated도 보존한다. 새 테이블·원시 스캔 보관은 추가하지 않는다.

날짜는 기존 from/to 양 끝 포함 규칙으로 필터링하며 미래 변화는 제외한다. 기존 기록과 발생 시각 내림차순으로 합친 뒤 페이지를 계산한다. 원본 변화 ID로 안정적인 이벤트 ID를 만들므로 같은 입력의 재전송이 조회 항목을 늘리지 않는다. 영상은 해당 이벤트 ID에 실제 clip 작업이 연결된 경우만 제공한다.

프론트 담당자에게 이 문서를 LiDAR·예측·평균 주기 등의 연결 안내와 함께 전달한다. 한국시간 표시는 변환하되 서버 UTC 시각을 수정하지 않는다.

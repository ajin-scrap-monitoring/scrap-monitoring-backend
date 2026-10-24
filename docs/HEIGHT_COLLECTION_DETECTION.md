# 높이 감소 기반 수거 감지

중장비가 여러 번 조금씩 덜어가는 수거는 안정된 기준 높이와 현재 높이의 누적 차이로 감지한다. 자동 감지는 **수거 추정**이며 작업자가 확인한 수거 완료와 별도다. 감지 뒤 유효한 추가 감소가 일정 시간 확인되지 않으면 ‘종료 추정’을 기록한다. 장비가 떠났거나 실제 작업이 완료됐다는 확정은 아니다.

## 활성화와 단위

기본적으로 꺼져 있고 감소 기준의 기본값도 없다. 명시적 감소 기준 없이 활성화하면 설정 오류로 시작을 거부한다. 검증된 기준을 준비한 뒤 백엔드 비공개 환경에서 설정한다. 높이 입력·시간 중앙값·v2 작업자가 함께 활성화되어 있어야 한다.

| 환경 변수 | 단위와 기본값 | 의미 |
| --- | --- | --- |
| `EDGE_V2_COLLECTION_DETECTION_ENABLED` | false | 추정 수거 감지 켜기 |
| `EDGE_V2_COLLECTION_DROP_METERS` | m, 기본값 없음 | 기준 높이에서 필요한 누적 감소량 |
| `EDGE_V2_COLLECTION_STABLE_BAND_METERS` | m, 0.2 | 기준 준비 허용 폭 및 수거 중 추가 감소 확인 폭. 감소 기준보다 작아야 함 |
| `EDGE_V2_COLLECTION_BASELINE_SECONDS` | 초, 10 | 기준 높이가 유지되었는지 확인하는 시간 |
| `EDGE_V2_COLLECTION_CONFIRM_SECONDS` | 초, 10 | 기준 이상 감소한 상태가 유지되어야 하는 시간 |
| `EDGE_V2_COLLECTION_SETTLE_SECONDS` | 초, 300 | 실제 측정에서 마지막으로 확인한 추가 감소 이후의 종료 대기 |
| `EDGE_V2_COLLECTION_SIMULATION_SETTLE_SECONDS` | 초, 20 | 명시적으로 구성한 시뮬레이션의 종료 대기 |
| `EDGE_V2_COLLECTION_HEIGHT_BASIS` | `UPPER` | 안정화한 관측 상단 높이. `REPRESENTATIVE`로 기존 단면 대표 높이 선택 가능 |

설정은 m 단위이고 내부 계산과 높이 응답은 기존 cm 계약을 유지한다. 예를 들어 3m는 계산 시 300cm다. **3m는 개발 후보이며 실제 오차·현장 기준으로 검증된 수치가 아니다.** 1m도 검증된 오차 범위나 안전한 감지 기준으로 확정하지 않는다. 시간·안정 폭 기본값 역시 개발 시작점이다.

중앙값과 유지 조건은 순간 흔들림을 줄이지만 지속되는 가림이나 측정 편향을 없애지는 않는다. 기준을 크게 하면 오탐은 줄어들 수 있지만 그보다 적게 덜어간 작업은 감지하지 못한다. 수거가 없는 동안의 시간에 따른 높이 변동과 실제 수거 감소량을 비교해 기준을 정해야 한다. 절대 거리 보정 오차와 시간에 따른 측정 변화는 구분한다. 새 스크랩이 동시에 들어오면 순감소가 작아져 수거를 놓칠 수도 있다.

## 상태 흐름

1. `BASELINING`: 안정 시간 동안 높이 범위가 허용 폭 이내이면 최솟값·최댓값의 중간을 기준 높이로 삼는다. 계속 적재되어 높이가 상승하는 경우에도, 후보 확인 구간의 최고 높이보다 허용 폭을 초과해 낮아지지 않고 기준 확인 시간을 채우면 그 후보 높이를 사용할 수 있다.
2. `IDLE`: 작은 감소를 합산할 수 있도록 기준을 아래로 따라 내리지 않는다. 더 높은 높이가 안정되거나 더 높은 후보 높이가 같은 확인 시간을 채우면 기준을 올린다. 후보는 한 확인 구간이 끝난 뒤 새로 잡으므로 순간 높은 값을 즉시 기준으로 삼지 않는다.
3. `CANDIDATE`: 기준보다 설정한 높이 이상 낮아지면 확인 시간을 센다. 다시 올라오면 후보를 취소한다.
4. `COLLECTING`: 감소가 유지되면 `COLLECTION_DETECTED`를 한 번 기록한다. 이후 여러 번 덜어가도 같은 작업으로 유지한다.
5. 감지 이후 현재 높이가 추가 감소 확인 폭 이상 낮아지고 확인 시간 동안 유지되면, 그 높이를 새 진행 기준으로 삼고 종료 대기를 다시 센다. 기준보다 작은 감소도 진행 기준과 누적 비교한다. 일시적인 튐은 확인 시간을 충족하지 못하면 진행으로 확정하지 않는다.
6. 추가 감소 후보가 없고 종료 대기가 충족되면 `COLLECTION_SETTLED`를 같은 `detectionId`로 기록하고 잔여 높이를 다음 기준으로 삼는다. 다시 `IDLE`로 돌아간다. 높이가 안정적으로 상승하면 진행 기준만 올리고 종료 대기는 초기화하지 않으므로, 재적재 때문에 평평한 높이를 오래 유지하지 못해도 종료를 추정할 수 있다.

종료 추정 시간 이상 쉬었다 다시 작업하면 별도 감지로 나뉠 수 있다. 종료 추정은 실제 수거 완료 시간이나 중장비의 부재를 보증하지 않는다.

### 실제 측정과 시뮬레이션

같은 누적 감소·추가 감소 확인 알고리즘을 사용하되, `SimulationHeightReference`가 명시된 출처만 `SIMULATION`의 짧은 종료 대기를 선택한다. 감소 속도나 단일 메시지의 `simulated` 표기를 보고 빠른 모드로 추측하지 않는다. 실제 측정은 `FIELD`이며 높이 기준의 검증과 기존 FRESH/READY 조건을 그대로 요구한다. 입력 관측의 시뮬레이션 표시와 구성한 출처가 다르면 감지를 진행하지 않는다. production에서 합성 기준을 거부하는 기존 제한도 유지한다.

예를 들어 공개 합성 시험에서는 작은 감소 사이에 긴 유효 관측 구간을 두는 경우와, 짧게 크게 낮아진 뒤 적재가 다시 시작되는 경우를 각각 검증한다. 두 종료 대기는 독립적인 비공개 설정이며 기본값도 검증된 현장 휴지 시간이나 정확도 보장이 아니다. 이력의 `settleSeconds / detectionProfile`과 요약의 `detectionSettleSeconds / detectionProfile`은 실제로 선택한 조건을 제공한다.

평평한 준비 구간과 계속 적재되는 준비 구간을 각각 거친 뒤 85% 또는 90%에서 10초 또는 15초에 걸쳐 바닥까지 감소하는 네 공개 합성 사례도 수신→상단 높이 필터→작업자→시작/종료 각 1건→기존 경과 API로 검증한다. 바닥의 유효한 0%는 결측이 아니다. 감소 확인 시간보다 빨리 바닥에 도달해도 낮아진 높이가 유지되면 감지할 수 있다. 다만 필터 준비와 감소 확인, 종료 대기가 있으므로 0% 도달 시각과 감지 시작·종료 판단 시각은 다르다. 실제 종료 경과는 종료 판단이 저장된 시각부터 계산한다.

## 입력과 기록

기존 FRESH/READY 높이 경로를 통과한 같은 최신 관측을 사용한다. 기본 `UPPER`는 센서별 각 스캔의 유효 최대 높이를 기존의 짧은 높이 창에 숫자 하나로 보관하고, 센서별 시간 중앙값 중 더 높은 값을 사용한다. 원시 거리·각도를 다시 계산하거나 점 원문을 더 저장하지 않는다. 관측된 상단 높이이며 센서가 보지 못한 전체 피트의 절대 최대 높이·부피·수거 중량이 아니다. 단발성 높은 반사는 시간 중앙값으로 완화되지만 지속적인 기계팔 가림과 높은 잔여 조각은 감지를 지연·왜곡할 수 있다.

창의 출처·메시지·시각·센서가 현재 관측과 일치하고 모든 센서의 상단 높이 창이 준비되어야 한다. 기존 창에 상단 숫자가 없으면 새 유효 관측으로 준비하며 대표 높이를 대신 빌려오지 않는다. 개별 높이·평균·적재율·경보용 계산은 바꾸지 않는다. `REPRESENTATIVE`는 기존 단면 높이 비율과 참조 깊이로 cm를 복원하는 경로다. 이미 정제된 최종 높이를 따로 수신하는 계약은 해당 값의 의미·필터 여부를 확인한 뒤 연동하며, 이 기능이 그 전달 완료를 뜻하지 않는다.

PostgreSQL에서는 관측값·필터·상단 높이 창을 짧은 읽기 전용 `REPEATABLE READ` 트랜잭션으로 함께 읽는다. 수신 커밋이 조회 사이에 완료되어 서로 다른 측정의 자료를 섞는 일을 막으며, 수신 행을 잠그거나 유효 시간·공백 기준을 늘리지 않는다. 판정 상태와 이벤트의 저장은 기존 작업자 트랜잭션에서 함께 수행한다.

동일 관측 재전송·이전 생성/관측 시각은 시간을 진행시키지 않는다. 장애·유효 높이 없음·관측 간격 3초 초과는 기준 준비와 후보 시간을 초기화한다. 감지 중 장애가 생기면 같은 작업 ID는 보존하되 추가 감소 후보와 종료 대기는 초기화한다. 자료가 없는 시간을 종료 대기로 세지 않는다. 장비 연결·기준 깊이·계산 방법·감지 설정이 바뀌면 새 기준을 준비한다.

`edge_v2_latest`의 `kind=collection_tracker`에 원시점 없이 소량의 상태·시각·높이 숫자만 저장한다. 상승 중 기준 후보도 후보 높이·구간 최고 높이·시각 세 값만 저장하며 입력 장애 때 초기화한다. 원시 높이 이력을 추가로 쌓지 않는다. `edge_v2_transitions`에 두 종류의 추정 이벤트를 보존하며 시각은 UTC, 높이는 cm다. 재시작 후 같은 관측을 처리해도 중복 기록하지 않고 상태와 이벤트를 한 트랜잭션으로 커밋한다. 현재 배포의 단일 작업자 조건을 따른다.

이벤트에는 `pitId`, `scopeToken`, `detectionId`, `simulated`, `methodVersion`, `observationId`, `timeBasis`, `confirmedCompletion=false`를 제공한다. 감지는 `baselineHeightCm / heightCm / dropCm`, 종료 추정은 `heightCm`를 추가한다. 시뮬레이션 출처를 실제 장비 검증으로 바꾸지 않는다.

추가 근거는 `detectionVersion=UPPER_RISING_BASELINE_V3`, `heightBasis`, `detectionProfile`, `settleSeconds`, `endPolicy=NO_FURTHER_DROP`다. 알고리즘·높이 기준·현재 출처가 사용하는 종료 시간이 scope에 포함된다. 다른 출처에만 적용되는 시간 설정은 현재 scope를 바꾸지 않는다. 버전·기준 변경 전 기록은 보존되지만 현재 통계와 경과 항목에 섞지 않으므로, 변경 뒤 첫 종료 전에는 경과가 없을 수 있다. 감지 이력 API에서 과거 기록을 조회할 수 있다.

기존 사람의 `CollectionLog`, 알림·읽음 기록이나 위험 상태·쿨다운을 변경하지 않는다. 외부 메일·수거업체 연락·영상 요청도 수행하지 않는다. 조회 API가 감지를 실행하지 않으며 기존 백엔드 작업자가 처리한다.

## 대시보드 연결

세션 인증과 `pit_id`는 기존 v2 규칙을 따른다. `GET /api/v2/monitoring/collection-summary?pit_id=pit-demo`에 다음 필드를 추가한다. 기존 완료 통계의 `source=COLLECTION_LOG`, `lastCollectionAt`, `completedCollectionsInWindow`는 사람의 완료 기록 의미를 보존한다.

| 필드 | 표시 용도 |
| --- | --- |
| `lastCollectionActivityAt / secondsSinceLastCollectionActivity` | 마지막 수거 종료·경과 초. 사람의 완료 확인 또는 현재 감지 기준의 종료 판단 중 더 최근 시각 |
| `activitySource` | `CONFIRMED_COMPLETION / HEIGHT_SETTLED / null`. 감지 시작만으로 종료 시간을 갱신하지 않음 |
| `lastSettledCollectionAt / secondsSinceLastSettlement` | 마지막 자동 종료 판단 시각·경과 초 |
| `lastDetectedCollectionAt / secondsSinceLastDetection` | 마지막 자동 감지 시각·경과 초 |
| `detectedCollectionsInWindow` | 기간 내 감지 작업 수. 감지·종료 두 이벤트를 두 작업으로 세지 않음 |
| `averageDetectedCollectionCycleSeconds` | 감지 간격의 평균 초, 최소 2개 필요. 확정 완료 주기와 별도 |
| `detectionEnabled / detectionState` | 기능 상태·처리 단계. 자료 지연·장애는 UNKNOWN |
| `detectionDropMeters / detectionConfirmSeconds / detectionSettleSeconds` | 사용 중인 설정 |
| `detectionSimulated / detectionReasonCodes` | 시뮬레이션 여부·미설정·비활성·자료 부족 구분 |
| `detectionProfile / detectionHeightBasis / detectionEndPolicy` | 선택한 `FIELD|SIMULATION`, `UPPER|REPRESENTATIVE`, `NO_FURTHER_DROP` 조건 |

감지 통계는 현재 장비 연결·높이 방법·감지 설정과 같은 `scopeToken`만 집계한다. 기준이 달라진 기록을 섞어 평균하지 않는다. 다른 기준의 과거 이벤트는 감지 이력에 남는다. 실제 기록이 없으면 시각·경과·평균은 null이다.

`GET /api/v2/monitoring/collection-detections?pit_id=pit-demo&limit=20&offset=0`은 `StateEventPageV2` 형식으로 감지·종료 추정 이벤트를 반환한다. `limit` 1~100, `offset` 0~10000이며 피트·현장·장비 범위로 구분한다. 모든 조회는 `Cache-Control: no-store`다. 새 연결에서는 종료 기준 활동 시각과 `activitySource`를 사용한다. 기존 화면의 경과 시간 항목은 아래 호환 경로를 통해 별도 화면 수정 없이 값을 읽는다.

## 기존 ‘최근 수거 후 경과’ 연결

기존 화면은 `GET /api/v1/events?pit_id=pit-demo&category=collection&page=1&pageSize=1`의 `occurredAt`과 snapshot의 `serverTime` 차이를 시간·분으로 표시한다. 고정된 ‘정보 없음’ 문구가 아니며 조회 결과가 없을 때만 해당 문구를 사용한다.

기존 화면의 수거 기록 재조회 주기는 약 1분이다. 종료 판단이 저장된 뒤 화면에 반영되기까지 이 조회 주기만큼 걸릴 수 있으며, 경과 시간 자체는 화면이 조회한 시점이 아닌 저장된 종료 판단 시각부터 계산한다.

백엔드는 저장된 `COLLECTION_SETTLED`를 이 조회의 수거 종료 항목으로 함께 제공한다. 기존 화면의 검사에 맞춰 `type=collection_completed / category=collection / status=completed`를 유지하되, 자동 항목의 `title=수거 종료 감지`, `collectionSource=HEIGHT_SETTLED`, `confirmedCompletion=false`, `simulated`, `detectionId`로 판단 출처를 명시한다. 이 호환 type/status는 사람이 확인했다는 뜻이 아니다. 사람의 기존 기록에는 `collectionSource=MANUAL_CONFIRMATION / confirmedCompletion=true`를 제공한다.

수거 시작(`COLLECTION_DETECTED`)은 이 목록에 넣지 않는다. 수거 중에는 이전 종료 시각을 유지하고 이번 종료 판단이 저장된 뒤에만 최신 시각이 바뀐다. 첫 종료 전에는 계속 정보 없음이다. 피트·현장·장비·현재 계산 방법과 감지 기준이 일치하는 종료만 포함하며 미래 기록을 제외한다. 다른 종류의 이벤트와 날짜·페이지 필터는 기존 규칙을 따른다. 자동 감지 중지 뒤에도 같은 기준으로 저장한 종료 이력은 조회할 수 있다.

조회 때 사람의 `CollectionLog`를 생성하거나 알림·확인자를 만들어 넣지 않는다. 종료 시각은 추가 감소 없는 대기 조건을 충족해 판단한 관측 시각이며 실제 마지막 중장비 동작 시각을 소급 추정하지 않는다. 작업자가 종료 대기보다 오래 쉬면 작업이 나뉠 수 있다. 현재 연결은 경과 시간용 이벤트 목록에 한정하며 기존 수거 확인 API·사람의 완료 통계·녹화 요청을 자동 생성하지 않는다. 대시보드 저장소 수정은 필요하지 않다.

## 이전 기준의 종료 기록 조회

일반 이벤트 API의 기본값은 위의 현재 기준 경과 계약을 유지한다. 이력 전체 보기에는 `collection_scope=all`을 명시하여 같은 피트/장치의 이전 기준 종료까지 조회한다. 저장된 collectionCriteria와 현재 기준 비교를 반환하며, 이 옵션으로 현황의 최근 경과나 종료 평균의 집계 범위를 바꾸지 않는다. [수거 이력 범위](COLLECTION_HISTORY_SCOPES.md)를 참고한다.

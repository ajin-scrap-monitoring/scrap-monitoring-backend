# 높이 감소 기반 수거 감지

중장비가 여러 번 조금씩 덜어가는 수거는 안정된 기준 높이와 현재 높이의 누적 차이로 감지한다. 자동 감지는 **수거 추정**이며 작업자가 확인한 수거 완료와 별도다. 감지 뒤 낮아진 높이가 안정되면 ‘종료 추정’을 기록한다. 장비가 떠났거나 실제 작업이 완료됐다는 확정은 아니다.

## 활성화와 단위

기본적으로 꺼져 있고 감소 기준의 기본값도 없다. 명시적 감소 기준 없이 활성화하면 설정 오류로 시작을 거부한다. 검증된 기준을 준비한 뒤 백엔드 비공개 환경에서 설정한다. 높이 입력·시간 중앙값·v2 작업자가 함께 활성화되어 있어야 한다.

| 환경 변수 | 단위와 기본값 | 의미 |
| --- | --- | --- |
| `EDGE_V2_COLLECTION_DETECTION_ENABLED` | false | 추정 수거 감지 켜기 |
| `EDGE_V2_COLLECTION_DROP_METERS` | m, 기본값 없음 | 기준 높이에서 필요한 누적 감소량 |
| `EDGE_V2_COLLECTION_STABLE_BAND_METERS` | m, 0.2 | 안정 구간의 최대 높이 변동 폭. 감소 기준보다 작아야 함 |
| `EDGE_V2_COLLECTION_BASELINE_SECONDS` | 초, 10 | 처음 기준 높이를 정하는 안정 시간 |
| `EDGE_V2_COLLECTION_CONFIRM_SECONDS` | 초, 10 | 기준 이상 감소한 상태가 유지되어야 하는 시간 |
| `EDGE_V2_COLLECTION_SETTLE_SECONDS` | 초, 60 | 감지 후 높이가 안정되어 종료 추정하는 시간 |

설정은 m 단위이고 내부 계산과 높이 응답은 기존 cm 계약을 유지한다. 예를 들어 3m는 계산 시 300cm다. **3m는 개발 후보이며 실제 오차·현장 기준으로 검증된 수치가 아니다.** 1m도 검증된 오차 범위나 안전한 감지 기준으로 확정하지 않는다. 시간·안정 폭 기본값 역시 개발 시작점이다.

중앙값과 유지 조건은 순간 흔들림을 줄이지만 지속되는 가림이나 측정 편향을 없애지는 않는다. 기준을 크게 하면 오탐은 줄어들 수 있지만 그보다 적게 덜어간 작업은 감지하지 못한다. 수거가 없는 동안의 시간에 따른 높이 변동과 실제 수거 감소량을 비교해 기준을 정해야 한다. 절대 거리 보정 오차와 시간에 따른 측정 변화는 구분한다. 새 스크랩이 동시에 들어오면 순감소가 작아져 수거를 놓칠 수도 있다.

## 상태 흐름

1. `BASELINING`: 안정 시간 동안 높이 범위가 허용 폭 이내이면 최솟값·최댓값의 중간을 기준 높이로 삼는다.
2. `IDLE`: 작은 감소를 합산할 수 있도록 기준을 아래로 따라 내리지 않는다. 더 높은 높이가 안정되면 기준을 올린다.
3. `CANDIDATE`: 기준보다 설정한 높이 이상 낮아지면 확인 시간을 센다. 다시 올라오면 후보를 취소한다.
4. `COLLECTING`: 감소가 유지되면 `COLLECTION_DETECTED`를 한 번 기록한다. 이후 여러 번 덜어가도 같은 작업으로 유지한다.
5. 감지 뒤 안정 시간이 충족되면 `COLLECTION_SETTLED`를 같은 `detectionId`로 기록하고 잔여 높이를 다음 기준으로 삼는다. 다시 `IDLE`로 돌아간다.

종료 추정 시간 이상 쉬었다 다시 작업하면 별도 감지로 나뉠 수 있다. 종료 추정은 실제 수거 완료 시간이나 중장비의 부재를 보증하지 않는다.

## 입력과 기록

기존 FRESH/READY 높이 경로를 통과한 대표 높이를 사용한다. 이미 구한 단면 높이 비율과 그 기준 깊이로 cm를 복원하며 거리·각도나 중앙값을 다시 계산하지 않는다. 단면 평균 높이 감소이며 전체 피트 부피나 수거 중량이 아니다.

동일 관측 재전송·이전 생성/관측 시각은 시간을 진행시키지 않는다. 장애·유효 높이 없음·관측 간격 3초 초과는 기준 준비와 후보 시간을 초기화한다. 감지 중 장애가 생기면 같은 작업 ID는 보존하되 종료 안정 시간은 초기화한다. 자료가 없는 시간을 안정 시간으로 세지 않는다. 장비 연결·기준 깊이·계산 방법·감지 설정이 바뀌면 새 기준을 준비한다.

`edge_v2_latest`의 `kind=collection_tracker`에 원시점 없이 소량의 상태·시각·높이 숫자만 저장한다. `edge_v2_transitions`에 두 종류의 추정 이벤트를 보존하며 시각은 UTC, 높이는 cm다. 재시작 후 같은 관측을 처리해도 중복 기록하지 않고 상태와 이벤트를 한 트랜잭션으로 커밋한다. 현재 배포의 단일 작업자 조건을 따른다.

이벤트에는 `pitId`, `scopeToken`, `detectionId`, `simulated`, `methodVersion`, `observationId`, `timeBasis`, `confirmedCompletion=false`를 제공한다. 감지는 `baselineHeightCm / heightCm / dropCm`, 종료 추정은 `heightCm`를 추가한다. 시뮬레이션 출처를 실제 장비 검증으로 바꾸지 않는다.

기존 사람의 `CollectionLog`, 알림·읽음 기록이나 위험 상태·쿨다운을 변경하지 않는다. 외부 메일·수거업체 연락·영상 요청도 수행하지 않는다. 조회 API가 감지를 실행하지 않으며 기존 백엔드 작업자가 처리한다.

## 대시보드 연결

세션 인증과 `pit_id`는 기존 v2 규칙을 따른다. `GET /api/v2/monitoring/collection-summary?pit_id=pit-demo`에 다음 필드를 추가한다. 기존 완료 통계의 `source=COLLECTION_LOG`, `lastCollectionAt`, `completedCollectionsInWindow`는 사람의 완료 기록 의미를 보존한다.

| 필드 | 표시 용도 |
| --- | --- |
| `lastCollectionActivityAt / secondsSinceLastCollectionActivity` | 마지막 수거 활동·경과 초. 확인된 완료 또는 현재 감지 기준의 감지 중 더 최근 시각 |
| `activitySource` | `CONFIRMED_COMPLETION / HEIGHT_DETECTION / null`. 감지를 확정 완료로 표시하지 않음 |
| `lastDetectedCollectionAt / secondsSinceLastDetection` | 마지막 자동 감지 시각·경과 초 |
| `detectedCollectionsInWindow` | 기간 내 감지 작업 수. 감지·종료 두 이벤트를 두 작업으로 세지 않음 |
| `averageDetectedCollectionCycleSeconds` | 감지 간격의 평균 초, 최소 2개 필요. 확정 완료 주기와 별도 |
| `detectionEnabled / detectionState` | 기능 상태·처리 단계. 자료 지연·장애는 UNKNOWN |
| `detectionDropMeters / detectionConfirmSeconds / detectionSettleSeconds` | 사용 중인 설정 |
| `detectionSimulated / detectionReasonCodes` | 시뮬레이션 여부·미설정·비활성·자료 부족 구분 |

감지 통계는 현재 장비 연결·높이 방법·감지 설정과 같은 `scopeToken`만 집계한다. 기준이 달라진 기록을 섞어 평균하지 않는다. 다른 기준의 과거 이벤트는 감지 이력에 남는다. 실제 기록이 없으면 시각·경과·평균은 null이다.

`GET /api/v2/monitoring/collection-detections?pit_id=pit-demo&limit=20&offset=0`은 `StateEventPageV2` 형식으로 감지·종료 추정 이벤트를 반환한다. `limit` 1~100, `offset` 0~10000이며 피트·현장·장비 범위로 구분한다. 모든 조회는 `Cache-Control: no-store`다. 화면 담당자는 활동 시각과 `activitySource`를 연결하고 ‘수거 감지’라는 의미를 표시해야 한다. API 추가만으로 고정 화면 문구가 바뀌지는 않는다.

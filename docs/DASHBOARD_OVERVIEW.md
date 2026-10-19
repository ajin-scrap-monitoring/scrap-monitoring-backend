# 현황 화면의 수거 통계와 활성 경보

현황 화면은 최신 적재율, 높이 그래프, 수거 완료 기록과 경보를 각각의 근거에 맞춰 조회한다. 화면에 항목이 비어 있다고 해서 같은 의미는 아니다. API 연결이 빠진 경우, 실제 기록이 없는 경우, 현재 값을 판정할 수 없는 경우를 구분한다.

## 화면 항목별 연결

| 화면 항목 | API와 주요 필드 | 결측·연결 처리 |
| --- | --- | --- |
| 현재 적재율·경고·수거 임계율 | `/api/v2/monitoring/snapshot`의 `summary` | `dataState=FRESH`일 때 현재값 사용, null을 0으로 대체하지 않음 |
| 적재율 경보 | snapshot의 `operations.effectiveState` | UNKNOWN/ERROR와 NORMAL 구분 |
| 최근 24시간·1시간 전 적재율 | `/api/v2/monitoring/load-history` | 유효한 해당 구간을 사용, 현재값으로 과거를 채우지 않음 |
| LiDAR 1·2 높이 | `/api/v2/monitoring/profiles` | 센서 ID로 연결, cm 단위. API가 있는데 화면이 고정 미지원이면 화면 연결 필요 |
| 최근 수거 후 경과·평균 수거 주기 | `/api/v2/monitoring/collection-summary` | 수거 완료 기록만 집계, 부족하면 null과 사유 |
| 현재 활성 적재율 경보 | `/api/v2/monitoring/active-alerts` | 현재 판정 기준이며 과거 알림 목록과 구분 |
| 과거 알림·읽음 상태 | `/api/v2/monitoring/alerts`, `/api/v1/notifications` | 읽지 않은 과거 경보가 현재도 활성이라는 뜻은 아님 |
| 예상 도달·남은 시간 | 예측 API 미구현 | 완료 시각이나 증가 속도를 임의의 고정 값으로 표시하지 않음 |

모든 v2 조회에는 `pit_id`와 기존 로그인 세션 쿠키가 필요하다. 아래 두 API는 조회 사용자와 관리자 모두 이용할 수 있으며 `Cache-Control: no-store`를 반환한다. 기록 작성·알림 읽음 처리·외부 발송은 수행하지 않는다. API 추가만으로 다른 저장소에 있는 화면의 고정 문구가 자동으로 바뀌지는 않는다.

## 수거 완료 통계

`GET /api/v2/monitoring/collection-summary?pit_id=pit-demo&window_days=30`

`window_days`는 기본 30일, 1~365일이다. 모든 시각은 UTC ISO 8601이다.

| 필드 | 의미 |
| --- | --- |
| `source` | `COLLECTION_LOG`: 기존 관리자 수거 완료 확인으로 저장된 기록 |
| `computedAt` | 이번 집계 시각 |
| `windowFrom / windowTo` | 평균에 사용하는 조회 기간, 양 끝 포함 |
| `lastCollectionAt` | 해당 피트의 마지막 수거 완료 시각. 평균 기간보다 오래된 기록일 수도 있음 |
| `secondsSinceLastCollection` | 집계 시각부터 마지막 완료까지 경과한 초. 바로 완료했으면 유효한 0, 기록이 없으면 null |
| `completedCollectionsInWindow` | 기간 안에 있는 완료 기록 수 |
| `cycleCount` | 평균에 사용한 연속 완료 사이 간격 수. 완료 n개이면 max(0, n-1) |
| `averageCollectionCycleSeconds` | 실제 연속 완료 간격의 평균(초), 최소 완료 2개 필요 |
| `reasonCodes` | 기록 없음: `NO_CONFIRMED_COLLECTIONS`, 기간 내 2개 미만: `INSUFFICIENT_COLLECTIONS_IN_WINDOW` |

공개 합성 예로 완료가 10시간 전·7시간 전·1시간 전에 있었다면 간격은 3시간·6시간이며 평균은 4.5시간이다. 마지막 완료는 1시간 전이다. 다른 피트·미래 시각의 기록은 섞지 않는다. 평균은 `(기간 내 마지막 완료 - 첫 완료) / cycleCount`로 SQL 집계하며 원문 메모나 작업자 목록을 불러오지 않는다.

센서 EVENT의 COLLECTION/SUSPECTED·CONFIRMED·RESOLVED·RETRACTED는 기존 작업자의 수거 완료 확인을 대체하지 않는다. 적재율이 하락하거나 경보가 복귀한 것만으로 가짜 수거 완료를 기록하지 않는다. 기존 `POST /api/v1/collections/confirm`의 관리자 확인 기록을 사용하며 이 조회 작업은 수거 기록을 추가하지 않는다.

## 현재 활성 적재율 경보

`GET /api/v2/monitoring/active-alerts?pit_id=pit-demo`

응답의 `scope=FILL_OPERATIONS`는 적재율 경보만 포함한다. 장비·영상 경보 전체를 지원한다는 뜻은 아니다.

- `evaluationState=NORMAL`: `items=[]`, 현재 적재율 경보 없음.
- `WARNING`: 사전 알림 설정이 켜져 있으면 `pre_collection_alert / warning` 항목 하나. 꺼져 있으면 빈 목록과 `PRE_COLLECTION_ALERT_DISABLED`.
- `CRITICAL`: `collection_required / error` 항목 하나. 기존 수거 임계치·지속 확인·히스테리시스 판정을 그대로 따른다.
- `UNKNOWN / ERROR`: 현재 판정을 확정할 수 없음. 빈 목록과 사유를 반환하므로 ‘경보 없음’으로 표시하지 않는다. 판정 기능이 꺼져 있으면 `OPERATIONS_DISABLED`.

항목의 `loadPercent`는 최신 측정 수치이며 JSON에서 소수점 셋째 자리에서 반올림한다. `measuredAt`은 그 측정 시각이다. `evaluatedAt`은 경보 판정을 완료한 관측 시각이며 경보 시작 시각이 아니다. `evaluationSource=RECENT_PERSISTED`인 짧은 처리 대기에는 두 시각이 다를 수 있다. 이때 기존 작업자의 최근 유효 판정을 유지하며 응답 사유에 `OPERATIONS_EVALUATION_PENDING`을 제공한다. 최신 관측까지 평가했으면 `CURRENT_OBSERVATION`이다. 최근 판정 허용 조건과 시간 제한은 [경보 처리 안내](V2_OPERATIONS.md#측정-갱신과-경보-갱신-시각)를 따른다. `simulated=true`인 경우 시뮬레이션임을 표시한다. 현재 단계와 피트로 만든 `id`는 표시용 키이고 DB 알림 ID와 다르다.

`notificationId`는 CRITICAL에서 연결된 최근 내부 기록이 있을 때 제공하며 경보 시작 시각을 의미하지 않는다. WARNING에서는 과거 위험 알림으로 연결하지 않는다. 경보가 해제되어도 과거 알림 기록은 남고, 알림을 읽어도 현재 위험 상태가 해제되지는 않는다. 조회해도 새 DB 알림이나 이메일을 만들지 않는다. 외부 발송 연동은 별도 기능이다.

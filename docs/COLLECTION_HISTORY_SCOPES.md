# 이전 감지 기준을 포함한 수거 이력

현황의 최근 수거 경과는 **현재 감지 기준**으로 판단한 종료 기록을 사용한다. 반면 이력 화면에서는 기준 변경 전 기록도 확인할 필요가 있다. 두 용도를 기존 이벤트 조회의 선택 옵션으로 구분한다.

```http
GET /api/v1/events?pit_id=pit-demo&category=collection&collection_scope=all
```

| `collection_scope` | 자동 수거 종료 기록 범위 | 사용할 화면 |
| --- | --- | --- |
| 생략 또는 `current` | 현재 장치 연결·높이 방법·감지 설정과 같은 기준만 | 현황의 최근 수거 경과 |
| `all` | 같은 피트·현장·장치의 이전 기준까지 포함 | 이력의 전체 수거 기록 |

기본값은 current이므로 기존 최근 수거 경과와 그 API 호출은 유지된다. 이력 담당자는 전체/수거 유형 조회에 collection_scope=all을 추가한다. 다른 저장소의 프론트 요청을 백엔드에서 수정하지 않으며, **옵션을 연결하기 전 기존 화면은 계속 current 범위**다. 이 옵션은 관리자 완료·경보·오류의 조회 범위를 변경하지 않는다. 지원하지 않는 값은422로 거부한다.

## 기존 이벤트 필드와 원래 감지 기준

자동 종료는 기존 ID·발생 시각·detectionId와 `collectionSource=HEIGHT_SETTLED`, `confirmedCompletion=false`, 원본 simulated를 유지한다. `COLLECTION_DETECTED` 시작 기록은 이 목록에 넣지 않는다. 관리자 완료는 MANUAL_CONFIRMATION/true이며 두 종류를 혼합하지 않는다.

자동 종료 항목에 `collectionCriteria`를 추가한다.

| 필드 | 의미 |
| --- | --- |
| `scopeToken` | 저장 당시 감지 기준 식별자. 현재 토큰으로 덮어쓰지 않음 |
| `matchesCurrentScope` | 현재 기준과 같으면 true, 다르면 false. 현재 또는 저장 당시 기준을 확인할 수 없으면 null |
| `detectionVersion / heightBasis` | 저장한 알고리즘 버전/높이 기준 |
| `detectionProfile / settleSeconds / endPolicy` | 저장한 FIELD/SIMULATION 구분, 종료 대기 초, 종료 정책 |

과거에 보관하지 않았거나 올바른 형식이 아닌 필드는 null이다. 현재 설정값을 과거 값처럼 채우지 않는다. 관리자 완료의 collectionCriteria는 null이다. 화면에서는 matchesCurrentScope=false를 “이전 감지 기준”, null을 “기준 비교 불가”로 표시할 수 있다. 자동 판단이라는 출처와 시뮬레이션 여부도 보존한다.

## 필터·보존과 현황 보호

`GET /api/v1/events`와 `GET /api/v1/history/load`는 `from/to`의 시간대를 UTC로
정규화하여 조회한다. 같은 시각을 다른 시간대로 전달해도 같은 결과를 반환하고 응답 시각은
UTC로 표시한다. 시간대가 없는 입력은 기존처럼 UTC로 해석한다. UTC 변환이나 기본 조회
기간 계산이 표현 가능한 날짜 범위를 벗어나면422 `HISTORY_TIME_OUT_OF_RANGE`로 거부한다.

all도 현재 피트에 명시적으로 연결된 site/edge 및 원본 pitId로 제한한다. 다른 피트·장치의 기록은 가져오지 않으며 자동 종료의 미래 기록은 제외한다. from/to 양 끝 포함·UTC 변환·발생 시각 내림차순·페이지 이동·실제 녹화 ID 연결을 기존대로 적용한다.

현재 높이 기준을 제거해 현재 감지 기준을 계산할 수 없더라도 all은 저장된 종료 이력을 조회할 수 있다. 이때 현재 기준과의 비교는 null이며 current는 기존대로 종료 기록을 제공하지 않는다. 현재 장치 연결 자체가 없으면 해당 피트의 v2 종료 이력은 반환하지 않는다. 장치를 다른 피트로 재배치했던 과거 연결을 추정하는 기능은 아니다.

조회는 기록을 생성·변경하지 않는다. collection-summary의 마지막 활동 시각과 자동 종료 평균, 수거 감지·경보·쿨다운에는 이 옵션을 적용하지 않는다. 이전 기준의 종료가 이력에 나타나도 현황의 첫 현재 기준 종료 시각을 대신하지 않는다.

프론트 담당자에게 이 문서를 [오류·복구 이력](ERROR_RECOVERY_HISTORY.md), [LiDAR 등 화면 연결](DASHBOARD_API_GUIDE.md), [예상 도달·평균 주기](FILL_FORECAST.md)와 함께 전달한다.

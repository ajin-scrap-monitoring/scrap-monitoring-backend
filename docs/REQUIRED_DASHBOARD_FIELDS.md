# 활성 알림·측정 표본의 필수 응답 필드

대시보드 추가 요청 A-03·A-04의 응답 계약.

`GET /api/v2/monitoring/active-alerts`의 `coverage`와
`GET /api/v2/monitoring/profiles`의 각 프로필 `samples`는 항상 배열로 제공한다.
누락·null을 정상 응답으로 해석하지 않는다. 최신 OpenAPI로 생성한 응답 타입에 반영한다.

- `scope=ALL`의 coverage는 장비·측정 상태의 확인 범위를 나타낸다.
  각 항목의 CHECKED/UNKNOWN/UNSUPPORTED와 reasonCodes로 판단한다.
  현재 경보가 없어 items가 빈 배열이어도 coverage와 별개다.
- scope 생략 또는 FILL_OPERATIONS에서는 coverage=[]다. 장비 전체를 확인했다거나
  모든 장비가 정상이라는 뜻이 아니다. 전체 범위는 scope=ALL로 요청한다.
- samples=[]는 이번 프로필에 제공할 측정 표본이 없다는 뜻이다. 빈 적재 공간이나
  높이0으로 바꾸지 않고 calibrationState/reasonCodes 등 프로필 상태를 함께 확인한다.
- 표본이 있어도 valid=false 또는 height=null인 점은 유효한0cm와 구분한다.
  표본 수는 모드·수신 데이터에 따라 달라지며 고정하지 않는다.

두 배열은 실제 직렬화 JSON에 항상 포함되므로 응답 OpenAPI에서도 필수로 선언한다.
다른 기본값 필드·표본 내부의 선택 필드는 유지한다. Python 내부 기본값과 요청 필수 조건은
바꾸지 않는다. 상태 판정·측정 계산·기존 JSON 필드나 값은 변경하지 않는다.

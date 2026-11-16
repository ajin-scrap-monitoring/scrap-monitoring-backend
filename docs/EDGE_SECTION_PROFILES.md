# Edge 독립 단면 곡선과 적재율

`EDGE_V2_SECTION_PROFILES_ENABLED`는 기본 `false`다. 활성화하면 최신 STATE의
`section_profiles`와 `fusion_result`를 곡선·현재 적재율·SSE·분 이력의 동일 입력으로 사용한다.
환경 변수 변경 시 컨테이너 재생성이 필요하다. 이미지 반영과 이 선택 경로의 활성화는 별개다.

이 모드는 기존 높이 계산 및 필터 점 그래프 경로보다 우선한다. 비활성 상태에서는 기존
높이·그래프 경로와 응답을 유지한다. 현재는 compact 저장과 명시적인 `EDGE_WORLD` 시뮬레이션
기준을 요구한다. 수거 감지가 켜져 있으면 원시 측정의 기존 높이 시간 창을 독립적으로 유지한다.
면적 기반 종합 비율을 높이로 환산해 기존 감소량에 적용하지 않는다. 기존의 검증된 단면·기준과
구별되는 물리 설치 지원을 완료한 것으로 해석하지 않는다.

## 곡선 변환

- 각 센서의 독립 bin 하나가 sample 하나다. bin 중심은
  `origin_xy_mm + (index + 0.5) × bin_width_mm × axis_xy`다.
- 중심을 cm로 한 번 변환해 명시적인 시뮬레이션 `floor_section`의 A→B 방향에 투영한다.
  0~1 밖의 중심은 제외하고, 남은 samples는 `positionRatio` 오름차순으로 정렬한다.
- 높이는 원본 `height_mm / 10`이다. 원본 `sourceIndex`는 정렬 후에도 bin index를 유지한다.
- `OBSERVED`·`INTERPOLATED`이며 높이가 존재할 때만 `valid=true`다. `UNAVAILABLE`·`MASKED`는
  위치를 남기고 `height=null`, `valid=false`로 제공한다. 실제 0cm는 유효한 값이다.
- Edge가 제공한 보간을 다시 계산하지 않는다. `wall_margin_cm`으로 bin을 다시 잘라내지 않는다.
  서로 다른 높이를 한 위치로 합치거나 임의 점을 선택하지 않는다. 축이 무너져 동일 위치가 되면
  `SECTION_PROFILE_AXIS_DEGENERATE` 또는 `SECTION_PROFILE_POSITION_COLLISION`으로 차단한다.
- 센서별 `sectionFillPercent`는 해당 `area_ratio × 100`이다. 곡선의 단순 평균 높이로 비율을
  다시 계산하지 않는다. `referenceDepthCm`는 활성 bin 용량 높이가 모두 같을 때 그 cm 값이며,
  bin별 용량이 다르면 null이다. 시뮬레이션 전체 깊이로 대신 채우지 않는다.

독립 센서 단면 source는 위 네 종류다. `CONFLICT`는 공유 단면 규격에만 속하며 독립 단면의
수신 스키마를 넓히지 않는다. 다른 센서의 점을 합쳐 새 높이를 만들지 않는다.

## 현재 비율과 최근 5초 중앙값

기존 최근 5초 기준을 유지한다. 화면 표시값을 현재 Edge 종합 비율로 바로 바꾸지 않는다.
예를 들어 최근 실제 비율이 10·20·30·40·50%라면 현재 비율은 50%, 표시 중앙값은 30%다.

| 필드 | 의미 |
| --- | --- |
| `profiles[].sectionFillPercent` | 각 센서의 현재 단면 면적 비율 |
| profiles 페이지 `rawMeanSectionFillPercent` | 구조·기하·시각 검사를 통과한 현재 `fusion_result.ratio × 100`; 없으면 필드 생략 |
| profiles 페이지 `meanSectionFillPercent` | 선택한 종합 비율 입력의 최근 5초 중앙값 |
| snapshot/SSE `summary.rawLoadPercent` | 운영 판단에 사용할 수 있는 현재 종합 비율 |
| snapshot/SSE `summary.loadPercent` | profiles의 mean과 같은 최근 5초 중앙값 |
| 분 이력 | 같은 선택 입력의 유효 중앙값을 저장한 기존 분 집계 |

시간 창은 실제 관측으로 처음 준비한다. 준비된 창에서 한 번의 시간쌍 누락은 현재 값을 null로
처리하되 다음 정상 관측에 불필요한 전체 재준비를 강제하지 않는다. 오래된 관측을 복사하거나
현재 값으로 대체하지 않는다. 창 길이 이상의 공백·센서/보정 오류·출처 변경은 다시 준비한다.
수거 필요 판정의 기존 3초 연속 확인은 별도로 유지하며 공백 뒤 새 확인 시간을 요구한다.

계산 최신성 1초의 Edge 규칙, 화면 유지 3초, 운영 지연 경고 3초, 이 5초 중앙값과 손실 경고의
정상 1시간 복귀는 서로 다르다. Backend의 현재 관측 TTL 설정도 그대로 유지한다. 생성 시
anchor 나이는 등록된 Edge window의 `max_age_ms` 이내여야 하고 현재 시각은 별도 TTL로 검사한다.

## 검증과 제한된 판단

configuration revision·좌표계·센서 ID·보정 버전·센서 배치 및 평면·bin 기하가 명시적 기준과
일치해야 한다. 시각 검증에는 실제 `scan_source` 메타데이터 또는 같은 STATE의 일치하는
cloud anchor를 사용한다. 식별자만 있는 evidence를 수신 시각으로 대신 채우지 않는다.
둘 다 있으면 메타데이터가 같아야 한다. 조회는 센서당 한 번의 고정 인덱스 조회이며 원본
`scan_result` 점 배열을 읽지 않는다. 정보가 없거나 만료 표시만 남으면 값은 null이다.

두 센서가 같은 clock domain일 때 단조 시계 간격을 비교한다. 다른 domain의 단조 시계를
빼거나 UTC 반올림을 이유로 `pair_tolerance_ms`를 늘리지 않는다. fusion 모델 버전·가중치·
등록된 비선형 보정 곡선과 실제 anchor도 일치해야 한다.

| 판단 | 곡선·참고 수치 | 표시 적재율·운영 판단 |
| --- | --- | --- |
| 검증된 `AVAILABLE`·현재 입력 정상 | 현재 bin과 종합 비율 제공 | 실제 창 준비 후 중앙값 제공, 기존 경보 규칙 적용 |
| `DEFAULT_SETTINGS` 또는 `LIMITED` | 유효 기하의 곡선과 명시적인 제한 상태·현재 참고 비율 제공 | `loadPercent`·mean은 null, 정상/경보용 AVAILABLE로 승격하지 않음 |
| 시간쌍 불일치 | 개별 센서의 유효 곡선은 보존, 종합 비율은 null | 현재 값과 운영 판단에 이전 값을 대신 쓰지 않음 |
| 만료·설정/좌표/보정 불일치·센서 이상 | 해당 곡선과 비율을 null/불가로 처리 | 사유와 null 제공, 다른 입력 경로로 대체하지 않음 |

`SECTION_REFERENCE_MISMATCH`, `SECTION_CALIBRATION_UNVERIFIED_OR_MISMATCHED`,
`SECTION_PROFILE_SETTINGS_MISMATCH`, `SECTION_ANCHOR_UNAVAILABLE`,
`SECTION_ANCHOR_REFERENCE_MISMATCH`, `SECTION_PROFILE_STALE_OR_CLOCK_SKEW`,
`SECTION_DEVICE_OR_SENSOR_UNAVAILABLE`, `SECTION_PAIR_UNMATCHED`,
`SECTION_FUSION_CONTEXT_MISMATCH`, `SECTION_FUSION_CONFIGURATION_MISMATCH`,
`SECTION_JUDGMENT_LIMITED` 등이 차단 사유다. 원본 판단 상태와 이유는 계속 보존한다.

## 기존 화면 호환과 적용 확인

새 경로에서는 `sampleSource`를 생략하고 선택 필드 `sectionSource=EDGE_SECTION_PROFILE`과
`sectionJudgmentState`로 출처·판단을 구분한다. 기존 파서가 모르는 새 enum을 `sampleSource`에
추가하거나 기존 MEASUREMENT 값으로 거짓 표시하지 않는다. `samples[].binSource`도 선택 필드다.
기존 `positionRatio`·`height`·`valid` 계약은 유지한다.

기존 화면 파서의 합성 응답 수용은 실제 브라우저·배포 화면 확인과 다르다. 현재 Edge 설정이
미검증 기본값이라면 이 모드를 켠 뒤 운영 게이지가 계속 null일 수 있다. 검증되지 않은 설치나
보정을 정상으로 바꿔 활성화 조건을 통과시키지 않는다. 서버 활성화 전 실제 입력의 판단 상태,
reference 일치, 원본/중앙값 차이, 동일 입력의 SSE·이력·기존 경보 및 화면 표시를 확인한다.
물리 설치의 floor/scan plane이 검증되지 않았다면 이 시뮬레이션 투영을 현장 기준으로 사용하지 않는다.

## 기존 높이 기반 수거 감지 유지

단면 표시 모드에서도 기존 `EDGE_V2_HEIGHT_FILL_ENABLED`·수거 감지 설정을 그대로 유지한다.
표시용 filter·분 이력은 Edge 종합 비율 입력만 사용하고, 원시 측정은 기존 `height_window`와
`height_fill` 관측을 갱신한다. 원시 높이 경로가 표시값이나 적재율 이력을 덮어쓰지 않는다.

수거 감지의 `UPPER`는 기존 스캔별 최댓값의 5초 중앙값을 센서별로 구한 뒤 센서 간 최댓값을
사용한다. `REPRESENTATIVE`도 기존 원시 높이 비율과 명시적 깊이 기준을 유지한다. 감소량·안정 폭
설정의 m 단위, 내부 cm, 확인·기준 준비·정체 시간, FIELD/SIMULATION 구분과 scope는 바꾸지 않는다.
단면 표시 선택만으로 준비된 수거 기준이나 진행 중인 탐지를 초기화하지 않는다.

수거 감지는 같은 DB snapshot에서 독립적인 현재 높이 관측·센서 상태·높이 창을 검증한다.
표시 판단이 LIMITED여도 유효한 원시 높이 관측은 기존 방식으로 처리할 수 있다. 이 내부 수거
입력 뷰를 공개 snapshot·SSE의 대체값으로 보내거나 Edge 판단을 AVAILABLE로 승격하지 않는다.
원시 높이가 만료·불일치·준비 불가이면 단면 면적 비율이나 이전 높이로 대신 진행하지 않는다.
추정 수거 감지·정체 종료와 작업자가 확인한 수거 완료는 계속 구분한다.

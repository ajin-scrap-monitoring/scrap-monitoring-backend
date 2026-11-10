# 점별 필터 XYZ 수신·조회

엣지의 선택 확장 `judgment.point_cloud`와 STATE의 `filtered_point_clouds`를 검증·보존한다.
기존 measurements의 원본 XYZ, section_profiles의 대표 높이와 적재율 판정은 유지한다.
수신 지원만으로 장치 설정을 변경하거나 기존 그래프의 데이터 소스를 전환하지 않는다.

## 수신 계약

- 기존 `POST /api/edge/v2/configurations`와 `/assessments`를 사용한다. schema_version은2.0이다.
- `judgment.point_cloud`는 선택 설정 객체다. 생략은 지원하고 명시적null은 거부한다.
- STATE의 `filtered_point_clouds`는 생략/null/센서2개 결과를 구분한다. 센서별 빈 배열은
  스캔 없음·UNAVAILABLE로 표현할 수 있다. 배열 순서 대신 sensor_id를 사용한다.
- `point_indices`, `angles_mdeg`, `xyz_mm`, `valid`, `invalid_reason_codes`, `sample_counts`는
  같은 길이·순서다. 원래 점 index와 기준 스캔으로 대응하며 무효점을 삭제하지 않는다.
- XYZ는 이미 설치 변환된world 좌표, mm 단위 숫자다. null은 무효, 실제0은 유효할 수 있다.
  설치 R/t를 다시 적용하거나 가공 좌표를 원본 position_mm에 덮어쓰지 않는다.
- POINT CLOUD 상태·설정 검증 상태는 기존 단면/적재율 상태와 별개다. DEFAULT는 실측 검증 완료가 아니다.
- anchor는 기준 스캔 시각이고 generated_at은STATE 생성 시각이다. 원본참조·시계·처리버전·
  설정/window·좌표계·보정 버전을 보존한다. A/B를 같은 점이나 동시 스캔으로 합치지 않는다.
- 필터는 엣지에서 수행한다. 백엔드는 필터를 재적용하거나 빈 점을 보간하지 않는다.

정수·유한 좌표·허용 범위·병렬 배열·유효 개수·상태·최소 스캔 수·원본 참조의 정합을 검증한다.
잘못된 본문은 기존422 오류 계약으로 거부한다. Idempotency-Key는message_id이고
첫 저장201/같은 메시지 중복200/본문 또는 불변 결과 충돌409를 유지한다.
새 필드는 본문/결과 해시에 포함하고, 새 필드가 없는 과거 설정·STATE 해시는 유지한다.
저장 완료 후 ACK한다. STATE와 EVIDENCE 도착 순서는 가정하지 않는다.

## 인증 조회

`GET /api/edge/v2/devices/{site_id}/{edge_id}/filtered-point-clouds`

기존 관리자·조회 사용자 로그인 세션을 사용한다. 미로그인401, 등록되지 않은 장치404다.
`Cache-Control: no-store`이며 장치 제어 토큰·설정 쓰기는 필요하지 않다.

응답은 `site_id`, `edge_id`, `message_id`, `assessment_id`, `config_revision`, `generated_at`,
`field_present`, `filtered_point_clouds`다. 마지막으로 보존된 STATE의 새 필드만 제공한다.

| 상태 | 응답 의미 |
| --- | --- |
| STATE 미수신 | 식별·생성시각null, field_present=false, filtered_point_clouds=null |
| 새 필드 없는 STATE | STATE 식별정보 존재, field_present=false, 배열null |
| 새 필드null인 STATE | field_present=true, 배열null |
| 센서별 결과 있는 STATE | field_present=true, 센서별 객체와 무효/빈 배열을 그대로 제공 |

**최신 보존 결과가 현재 유효하다는 보장은 없다.** 생성시각과 센서별anchor 시각·상태·입력나이
기준을 함께 확인해야 한다. 오래된 결과를 현재 정상값으로 표시하거나 원본으로 몰래 대체하지 않는다.
과거 STATE가 늦게 도착해도 최신 보존 결과를 덮어쓰지 않는다.
compact 모드는 최신 원문과 불변 해시를, 일반 모드는 원문을 사용한다. 모든 STATE의
전체 배열을 영구 이력으로 조회하는 API가 아니며 기존 보존 정책을 확대하지 않는다.

이 경로는 mm 단위의원천 가공 결과 조회다. 기존 cm 기반 모니터링 프로필 API와 구분한다.
바닥 기준 높이 계산·그래프 투영·기존 화면 연결은 별도 검증 단계다.

## 모니터링 그래프의 선택 입력

`EDGE_V2_FILTERED_PROFILES_ENABLED=true`로 기존
`GET /api/v2/monitoring/profiles`의 점 소스를 필터 XYZ로 선택한다. 기본값은false다.
이 선택 경로는 compact 저장과 명시적인 `EDGE_WORLD`·`SENSOR_ANGLE`
시뮬레이션 높이 기준이 필요하다. 검증된 현장 높이 모델에 자동 적용하지 않는다.

- 기존 `samples`·cm 높이·positionRatio 구조를 유지한다. `sampleSource`는
  `MEASUREMENT` 또는 `FILTERED_POINT_CLOUD`이며, 필터 경로에서는
  `processingVersion`·`sourceScanId`도 제공한다.
- A/B를 sensor_id로 구분하고 각 anchor 시각을 measuredAt에 사용한다. 선택 각도와 원본
  index를 유지하며 각도순으로 표시한다. 무효점은null과valid=false인 빈 구간이다.
- XYZ를cm로 변환하고 명시된 바닥 Z를 빼서 높이를 구한다. 같은 설치 R/t를 재적용하지
  않는다. 기존 피트 경계·벽 여유·높이 범위를 벗어난 점은 표시하지 않고 빈 구간으로 남긴다.
- 설정 revision·좌표계·설치 보정·적용 필터/window·센서/시계 상태·생성 및 anchor 시각을
  검사한다. 미수신/null/만료/불일치 때 원본이나 이전 필터점으로 대체하지 않는다.
- 개별점은 엣지가 필터링한 값을 사용한다. 백엔드의 최근5초 중앙값은 각 센서의 스캔 평균에만
  적용한다. 별도 `filtered_profile_window`에 센서별 평균/최댓값만 보존하며 전체 점 이력을
  복사하지 않는다. 반복 스캔·늦은 STATE는 관측 수를 늘리지 않는다.
- 그래프용 창은 기존 적재율·수거 판정용 원본 측정 창과 분리한다. 이 선택만으로 적재율 입력,
  판정·이력·알림 정책을 전환하거나 장치 이상을 해제하지 않는다.

기존 높이 기준에 고정된 revision이 실제 설정과 다르면 이 경로도 사용할 수 없다.
배포 시 설정의 기하·보정이 기존 기준과 일치하는지 확인한 뒤 기준을 명시적으로 갱신한다.
revision 검사를 생략하거나 시뮬레이션 DEFAULT를 현장 검증 완료로 바꾸지 않는다.
점별 필터가 투영 좌표의 왕복이나 실제 표면 겹침까지 없애 준다고 보장하지 않는다.

## 활성화 순서

백엔드 구현·검증 → 병합·서버 수신 준비 → 엣지 담당자의 지원 버전 배포 →
전체judgment에point_cloud를 포함한새revision 요청 → 저장ACK와APPLIED → 실제 배열·마스크·참조대조.
설정202 접수만으로 수신·활성화 완료라고 판단하지 않는다.
운영 주소·토큰·원본 센서 데이터는 이 문서에 포함하지 않는다.

공개 근거: [엣지 수신 계약](https://github.com/ajin-scrap-monitoring/ajin-edge-platform/blob/032a1b18fe57b504e202210d942e3ec914eae721/docs/FILTERED_POINT_CLOUD_CONTRACT.md),
[STATE 스키마](https://github.com/ajin-scrap-monitoring/ajin-edge-platform/blob/032a1b18fe57b504e202210d942e3ec914eae721/contracts/backend/v2/assessments.schema.json),
[설정 스키마](https://github.com/ajin-scrap-monitoring/ajin-edge-platform/blob/032a1b18fe57b504e202210d942e3ec914eae721/contracts/backend/v2/configurations.schema.json).

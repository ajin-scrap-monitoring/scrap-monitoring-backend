# 점별 필터 XYZ 수신·조회

엣지의 선택 확장 `judgment.point_cloud`와 STATE의 `filtered_point_clouds`를 검증·보존한다.
기존 measurements의 원본 XYZ, section_profiles의 대표 높이와 적재율 판정은 유지한다.
이 지원만으로 장치 설정을 변경하거나 기존 그래프의 데이터 소스를 전환하지 않는다.

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

## 활성화 순서

백엔드 구현·검증 → 병합·서버 수신 준비 → 엣지 담당자의 지원 버전 배포 →
전체judgment에point_cloud를 포함한새revision 요청 → 저장ACK와APPLIED → 실제 배열·마스크·참조대조.
설정202 접수만으로 수신·활성화 완료라고 판단하지 않는다.
운영 주소·토큰·원본 센서 데이터는 이 문서에 포함하지 않는다.

공개 근거: [엣지 수신 계약](https://github.com/ajin-scrap-monitoring/ajin-edge-platform/blob/032a1b18fe57b504e202210d942e3ec914eae721/docs/FILTERED_POINT_CLOUD_CONTRACT.md),
[STATE 스키마](https://github.com/ajin-scrap-monitoring/ajin-edge-platform/blob/032a1b18fe57b504e202210d942e3ec914eae721/contracts/backend/v2/assessments.schema.json),
[설정 스키마](https://github.com/ajin-scrap-monitoring/ajin-edge-platform/blob/032a1b18fe57b504e202210d942e3ec914eae721/contracts/backend/v2/configurations.schema.json).

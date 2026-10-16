# 좌표 기반 높이 프로필

`GET /api/v2/monitoring/profiles?pit_id=...`는 로그인된 사용자가 센서별 높이 곡선을 조회하는 API다. 단위는 cm이며 계산은 최신 관측을 읽는 동안만 수행한다. DB 테이블·원문 저장량·수신 ACK는 바꾸지 않는다.

## 설정과 검증 기준

기본 `EDGE_V2_HEIGHT_REFERENCES=[]`에서는 높이를 생성하지 않는다. 활성화하려면 compact 저장, 장치/피트 연결, 저장된 피트 총깊이와 비공개 높이 기준을 함께 등록한다. 기준은 다음 필드를 가진 JSON 배열이다. 내부 치수와 실제 설정을 Git이나 로그에 남기지 않는다.

| 필드 | 의미 |
| --- | --- |
| `pit_id` | 기존 모니터링 피트 연결 |
| `config_revision`, `coordinate_frame_id` | 사용할 불변 설정과 좌표계 |
| `calibration_versions` | 센서 ID 두 개와 각각의 보정 버전 |
| `verified` | z축이 수직 위쪽이며 바닥·측정 구간을 검증했는지 여부. 기본 false |
| `floor_z_cm` | 동일 좌표계에서 평평한 빈 바닥의 z좌표 |
| `x_min_cm`, `x_max_cm`, `y_min_cm`, `y_max_cm` | 사용할 수평 직사각형 구간, 경계 포함 |
| `profile_axis` | 그래프 A→B 방향의 좌표축 `x` 또는 `y` |
| `bin_count` | 동일 폭의 그래프 구간 수, 기본20, 2..200 |
| `minimum_points_per_bin` | 구간별 최소 유효 점 수, 기본3 |
| `minimum_coverage` | 단면 기준 적재율을 계산할 최소 유효 구간 비율, 기본1.0 |
| `minimum_quality` | 명시적으로 합의한 SDK 품질 하한. 기본 null이며 임의 해석하지 않음 |

좌표 기준 설정은 ±1,000,000cm 이내, 피트 깊이는 0초과 1,000,000cm 이하여야 한다. 센서 보정 VERIFIED, 보정 버전·좌표계·각도 범위·설정 일치를 별도로 검사한다. 설명 문자열에서 위쪽 축이나 바닥을 추측하지 않는다. `verified=true`는 현장 검증을 마친 담당자가 설정하는 선언이며 합성 테스트 통과만으로 실제 설정을 검증 처리하지 않는다.

`EDGE_V2_EMPTY_FLOOR_DISTANCE_CM`는 센서와 빈 바닥의 거리 참고값이다. 바닥 z좌표·피트 총깊이와 다르며 이 값만으로 레이저의 경사 거리를 높이로 환산하지 않는다. 평평하지 않은 바닥, 기울어진 z축, 임의 방향의 단면은 이 계산의 지원 범위에 포함하지 않는다.

## 계산과 응답

1. 최신 PERIODIC 관측만 사용한다. 과거 EVIDENCE는 최신 화면을 대체하지 않는다.
2. 장치 상태·센서 상태가 신선하고 시각 동기화·설정 일치가 확인돼야 한다. 관측 신선도는 `EDGE_V2_ASSESSMENT_TTL_SECONDS`를 사용한다. 미래/오래된 관측·센서 장애·설정 불일치는 null과 사유로 표시한다.
3. mm 좌표를 cm로 바꾸고 `높이 = z - floor_z_cm`으로 계산한다. 좌표 null·거리0이하·계산 불가 사유·설정한 품질 하한 미달·측정 구간 외부·바닥 아래·피트 깊이 초과인 점은 제외한다. 결측을 높이0으로 채우지 않는다.
4. 선택한 수평 축을 동일 폭으로 나누고 구간마다 유효 높이의 중앙값을 구한다. 표본이 부족한 구간은 `height:null, valid:false`다. `positionRatio`는 구간 중심의0..1 위치이며 samples는 최대200개다.
5. `average/minimum/maximum`은 유효 구간 중앙값들의 평균/최소/최대다. 점이 밀집된 구간에 가중치를 더 주지 않는다. `coverageRatio`는 유효 구간 비율이다. 일부 구간이 빠져도 확인 가능한 높이는 제공하지만 기준 적재율은 최소 coverage를 충족해야 한다.
6. 센서별 `sectionFillPercent = average / 피트 총깊이 × 100`이다. 두 센서가 MATCHED이고 두 단면 값이 모두 유효하면 `meanSectionFillPercent`에 두 값의 산술평균을 제공한다. 조건을 충족하지 못하면 null과 `meanSectionReasonCodes`를 반환한다.

`calibrationState`는 UNVERIFIED/VERIFIED/UNAVAILABLE이며 `valuePolicy`는 SPATIAL_BIN_MEDIAN이다. VERIFIED 상태여도 coverage가 부족하면 `sectionFillPercent`는 null일 수 있다. `reasonCodes`에서 부족 원인을 확인한다.

이 값은 두 대표 단면의 높이 비율이다. 전체 피트의 체적 적재율이나 현장 검증된 수거 기준을 보장하지 않는다. 공간 구간의 중앙값은 기존 최근5초의 시간 중앙값과 구분한다. `/profiles` 결과로 엣지 `capabilities.fill`을 ENABLED로 바꾸거나 `fill_estimate`를 생성하지 않으며 기본 설정의 snapshot/SSE·분 이력·수거 알림은 기존 검증 STATE를 계속 사용한다. 계산 방법과 현장 기준 검증 후 선택적으로 연결하려면 [단면 입력 설정](HEIGHT_FILL_PIPELINE.md)을 사용한다.

## 합성 검증

```bash
uv run pytest tests/test_lidar_height.py -v
uv run python -m tests.integration.verify_postgresql_api tests/test_lidar_height.py
```

합성 시험은 단위/바닥/깊이, 구간별 중앙값과 표본 밀도, 빠진 구간, 범위/품질/좌표 오류, 설정/보정 변경, 신선도/장치 장애, 늦게 도착한 설정, A/B 불일치, HTTP 인증과 동일 메시지 ACK/409, 원본 판정·알림과의 분리를 확인한다. 물리 장비·실제 설치 보정·브라우저 화면 검증과 배포는 포함하지 않는다.

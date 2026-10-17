# 높이 단면 계산과 중앙값·이력·수거 판정

기본 `EDGE_V2_HEIGHT_FILL_ENABLED=false`는 기존 검증 엣지 STATE를 사용한다. true로 바꾸면 [높이 프로필](LIDAR_HEIGHT_PROFILES.md)의 두 MATCHED 단면 비율을 입력으로 사용한다. compact 저장과 비공개 높이 기준 등록이 필요하다. 실제 설치 보정·바닥·깊이·측정 구간 검증을 먼저 완료한다. 합성 좌표 시험은 현장 검증을 대신하지 않는다.

## 입력과 표시

측정 저장 트랜잭션에서 최신 프로필을 계산하고 별도 `height_fill` 최신 행에 작은 숫자·출처·식별 정보만 저장한다. 과거 EVIDENCE·늦은 관측·같은 메시지 재전송은 창을 진행시키지 않는다. 점 원문을 추가 보관하지 않는다.

`summary.inputSource`는 EDGE_STATE 또는 HEIGHT_SECTIONS다. snapshot의 `assessment`와 그 수신 시각은 원래 엣지 판정을 유지한다. 높이 모드의 `fillObservation`은 출처·측정 message_id·별도 observation_id·생성/수신/관측 시각·계산 방법과 입력 비율이다. 엣지 `capabilities.fill`을 변경하거나 엣지가 보낸 것처럼 새 STATE를 만들지 않는다.

단면 모드의 `rawLoadPercent`는 현재 유효 단면 비율, `loadPercent`는 시간 중앙값으로 안정화한 높이에서 계산한 단면 비율이다. 비율에 두 번째 시간 중앙값을 적용하지 않는다. 두 대표 단면의 높이 비율이며 전체 체적 적재율이 아니다. 화면에는 단면 기준임을 표시하고 FRESH 상태의 summary 값만 현재 게이지에 사용한다. 과거 fillObservation 수치를 현재 값으로 복원하지 않는다. 높이 모드에서 검증 높이가 없으면 엣지가 숫자를 보내더라도 null이며 다른 입력으로 자동 대체하지 않는다.

## 필터와 수거 필요 판정

[높이 시간 필터](HEIGHT_TEMPORAL_MEDIAN.md)의 구간별 최근5초 중앙값과 기존 3초 위험 지속 확인, 기본 WARNING75/CRITICAL90/85이하 복귀와 30분 알림 제한을 사용한다. 명시적인 피트 임계치가 있으면 그 값을 사용한다. 피트 깊이는 센서와 빈 바닥 사이 거리와 별개다.

[시뮬레이션 수신점 모드](SIMULATION_HEIGHT_API.md)는 공간 구간 대신 각 센서 스캔의 유효 높이 평균에 최근5초 중앙값을 적용한다. 최신 측정점 높이는 시간 창 준비 여부와 관계없이 프로필에 제공한다. 고정 공간 구간·최소 측정 비율·최소 점 개수 조건을 적용하지 않으며, 유효점이 없는 센서나 오래된 관측은 과거 값으로 채우지 않는다. `SIMULATION_SENSOR_ANGLE_V1` 방법으로 기존 공간 평균 이력과 구분한다. 두 센서 비율의 평균은 측정점 기반 추정치이며 전체 피트 체적을 검증한 값이 아니다.

센서/장치 장애·시각 오류·새 부팅·관측 공백1.5초 초과·입력 모드·좌표 기준/보정/계산 방법·피트 깊이·위험 임계치 변경은 창을 다시 준비한다. 유효 관측5개가 모이기 전에는 null이며 과거 값이나0으로 채우지 않는다. 센서 FRESH를 확인할 수 없는 새 상태 보고는 다음 측정 전에도 표시값과 창을 무효화한다. 설정 변경 직후 과거 계산을 현재 기준의 값으로 표시하지 않는다.

기존 EDGE_V2_OPERATIONS_ENABLED=true로 활성화한 운영 작업자는 별도 관측 ID로 마지막 평가를 관리한다. 알림은 LOG/RECORDED이며 실제 이메일·SMS·수거업체 발송이 아니다. 오류 동안 마지막 경보 상태는 보존하지만 현재 상태는 ERROR/UNKNOWN이다. 수거 완료 사건을 자동 생성하지 않는다.

## 이력과 출처

분 기록은 최대60개의 숫자/null 슬롯과 출처·방법 식별자만 보관한다. valuePolicy=MEDIAN_MINUTE, inputSource, methodVersion, observationId를 제공한다. 높이 관측은 엣지 판정 ID가 아니므로 이력과 v2 경보의 assessmentId는 null이다. 경보 API는 원본 수신 메시지 종류로 출처를 구분한다. 기존 내부 링크의 assessment_id 컬럼은 호환성을 위해 계산 관측 ID도 저장하며 공개 API에서는 두 ID를 구분한다.

같은 조회 bucket에 서로 다른 유효 입력 출처 또는 계산 방법의 값이 있으면 평균하지 않고 MIXED/null과 FILL_METHOD_CHANGED_WITHIN_BUCKET을 제공한다. 무효 표본을 다른 계산법의 유효 표본으로 바꾸지 않는다. 입력/방법 변경은 작은 FILL_INPUT_CHANGED 사건으로 남긴다.

부분 분 조회에서 원래 ID와 세부 시각을 보존하지 않았다면 null과 COMPACT_SUBMINUTE_METADATA_NOT_RETAINED를 유지한다. 숫자 슬롯과 bucket 출처는 제공하되 원래 정밀 시각을 만들어내지 않는다. 프론트엔드는 해당 값을 bucket 위치에 표시하고 메타데이터 부재를 유지한다.

새 테이블/migration은 없으며 기존20개 업무 테이블을 사용한다. 장기 기록 만료·백업 변경·실제 서버 활성화는 포함하지 않는다.

## 검증

Ruff check/format, Mypy, 전체 Pytest와 `uv run python -m tests.integration.verify_postgresql_api`를 실행한다. `tests/test_height_fill_pipeline.py`는 HTTP 수신부터 중앙값·분 이력·수거 기록까지 검증한다. 순간 튐, 지속 위험·복귀·쿨다운, 유효0과결측, 재전송/충돌, DB 세션 재개, 장애·재부팅·공백, 늦은 설정, 기준/모드 변경과 이력 격리를 포함한다. 실물 장비·현장 정확도·외부 발송 검증으로 해석하지 않는다.

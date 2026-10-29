# 측정 영역·센서 배치 조회

`GET /api/v2/monitoring/layout?pit_id=pit-example`은 로그인 사용자가 읽는 JSON 배치 정보다.
응답은 `Cache-Control: no-store`. 미설정 피트는404, 잘못된 쿼리는422다.
실제 현장 치수·좌표는 비공개 실행 설정과 수신 설정에서 읽으며 공개 소스에 고정하지 않는다.
영상·측정점 본문·외부 미디어 서버를 조회하거나 설정·DB 값을 변경하지 않는다.

## 최상위 계약

| 필드 | 의미 |
| --- | --- |
| pitId / computedAt | 명시적 피트 연결 / 이번 응답 시각(UTC). 촬영·측정 시각 아님 |
| unit | 모든 위치·경계·바닥·높이 수치는 cm |
| state | SIMULATION / PARTIAL / UNVERIFIED / UNAVAILABLE |
| source / simulated | SIMULATION_REFERENCE / HEIGHT_REFERENCE / NONE 및 시뮬레이션 여부 |
| configRevision / configurationGeneratedAt | 참조가 고정한 설정 버전 / 수신 설정의 생성 시각 |
| bindingState | 가용한 최신 measurement/status의 설정 버전과 일치하면 MATCHED, 다르면 MISMATCH, 확인 근거 없으면 UNKNOWN |
| coordinateFrame | id, cm 단위, z 수직축, 오른손 좌표계 및 수신한 원점·축 설명 |
| region | 경계 점 배열·floorZ·nullable topZ·경계 종류 |
| sensors | 구성된 두 sensorId의 위치·보정 상태·측정선·그래프 위치 기준 |
| reasonCodes | 일부 또는 전체 정보를 제공하지 못하는 근거 |

프레임 설명은 수신 원문 메타데이터이며 문자열에서 축이나 변환을 추론하지 않는다.
원본 보정 translation_mm는 검증된 경우에만10으로 나눠cm로 변환한다.
레이아웃의 시각·설정 일치는 센서의 현재 측정 가능 상태나 최근 스캔의 신선도를 보장하지 않는다.
실시간 상태는 snapshot/profiles/active-alerts로 따로 확인한다.

참조 없음·고정 설정 미수신·프레임/센서 불일치·최신 관측의 설정 버전 불일치는
200과 UNAVAILABLE, 사유 코드 및 null 좌표로 반환한다. 임의 좌표(0,0)로 대체하지 않는다.
정상 좌표의0은 유효하다. 설정 버전이 바뀌면 이전 배치를 현재 배치처럼 표시하지 않는다.

## 시뮬레이션

- region.kind=SIMULATION_BOUNDARY. 비공개 시뮬레이션 참조의 단순 다각형이다.
  마지막 점에서 첫 점으로 닫으며 마지막 점을 중복 제공하지 않는다.
- 센서 position은 참조 origin_cm, calibrationState=SIMULATION이다.
  실제 센서 보정이 검증됐다는 의미가 아니며 실제 보정 버전을 만들어 넣지 않는다.
- measurementLine은 기존 높이 그래프의 `floor_section`과 같은 스캔 평면/바닥 교차 구간이다.
  kind=SCAN_PLANE_FLOOR_INTERSECTION, a/b(x,y,z), lengthCm을 제공한다.
  복수 구간은 기존 그래프와 같은 가장 긴 연속 구간을 사용한다.
- A→B는 기존 그래프의 positionRatio=0→1 방향이다. 가장 큰 성분의 축이 증가하는 방향이며
  두 성분 크기가 같으면Y 기준이다. 그래프를 좌우 반전하거나 별도 정렬 기준으로 추정하지 않는다.
- profileMapping.basis=FLOOR_SECTION, minimumCm=0/maximumCm=lengthCm.
  선상 거리/lengthCm가 positionRatio다. 그래프는 이 평면 방향에 투영한 위치를 사용한다.
- 교차 구간을 구할 수 없으면 선과 profileMapping은null, state=PARTIAL.
  위치를 알더라도 길이0의 가짜 측정선을 반환하지 않는다.
- 교차선은 계산용 바닥 기준이며 전 구간이 현재 스캔 각도나 유효점으로 관측됐다는 뜻이 아니다.
  실시간 유효점·결측은 profiles를 사용한다.

SIMULATION_GEOMETRY/PHYSICAL_INSTALLATION_UNVERIFIED를 함께 제공한다.
실제 현장 설치 위치·경계·물리 보정 완료의 증거로 사용하지 않는다.

## 실측 높이 참조

- 참조 verified=false면 state=UNVERIFIED, 경계·센서 위치는null이다.
- 참조가 검증됐어도 region.kind=HEIGHT_MEASUREMENT_REGION은 높이 계산용 직사각 영역이다.
  실측한 적재 공간의 전체 외곽선으로 간주하지 않는다.
- 개별 보정의 verification_state=VERIFIED 및 고정 calibration version 일치 시에만
  수신 변환 원점 translation_mm의cm 위치를 제공한다. 다른 센서나 보정 버전으로 대신 채우지 않는다.
- profileMapping.basis=FRAME_AXIS 및 axis=x/y, minimumCm/maximumCm는 기존 높이 그래프의
  축·범위 기준이다. 공간별 대표 값의 positionRatio는 해당 구간 중심이다.
- 수신 회전 행렬만으로 원시 스캔 평면의 축 기저를 가정하지 않는다.
  현재 계약에는 그 기저와 실측 전체 외곽선이 없으므로 측정선은null, state=PARTIAL,
  PHYSICAL_SCAN_PLANE_UNSPECIFIED/PHYSICAL_PIT_BOUNDARY_NOT_CONFIGURED를 제공한다.
- 일부 센서 보정만 맞지 않으면 그 센서 위치만null로 남긴다.
  피트 높이 근거가 없으면 topZ도null이며 HEIGHT_DEPTH_UNAVAILABLE를 제공한다.

실제 설치 배치를 완성하려면 Edge/현장 담당자의 측정 경계와 스캔 평면·보정 기준 확인이 필요하다.
이 API 구현만으로 실측 설치 보정이나 현장 검수가 끝난 것으로 설명하지 않는다.

## 화면 연결

센서 배열 순서 대신 sensorId로 연결한다. 모든 단위는cm이며m 표시 시100으로 한 번만 나눈다.
시뮬레이션/부분 정보/보정 미확인 표시를 유지하고 nullable 경계·위치·선을 임의로 그리지 않는다.
새 그래프 표본을 계산하는 API가 아니며 현재 높이 그래프·적재율 판정 동작은 유지한다.

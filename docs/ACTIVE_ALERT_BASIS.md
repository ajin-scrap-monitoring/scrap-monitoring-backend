# 현황 판정과 활성 알림의 비교 기준

`GET /api/v2/monitoring/active-alerts`는 같은 응답을 계산할 때 사용한 snapshot의
근거를 필수 `basis` 객체로 제공한다. 기존 evaluationState와 알림 판정은 유지한다.

| basis 필드 | 대응하는 snapshot 필드 |
| --- | --- |
| snapshotId | snapshotId |
| dataState | dataState |
| filterState | summary.filterState |
| inputSource | summary.inputSource |
| measuredAt | measuredAt |
| lastEvaluatedAt | operations.lastEvaluatedAt. 판정 기능 미사용이면null |
| evaluationSource | operations.evaluationSource. 근거가 없으면null |
| lastKnownState | operations.lastKnownState. 과거 상태이며 현재 판정으로 대신 쓰지 않음 |
| reasonCodes | snapshot.reasonCodes. 알림 항목 표시 정책 사유와 구분 |

현재 적재율 판정은 snapshot의 **operations.effectiveState**와 활성 알림의
**evaluationState**로 비교한다. 원본 assessment.fill_state나 lastKnownState는
현재 유효 판정이 아니다. operations가 없으면 활성 알림은UNKNOWN이다.

- 필터 WARMING이면 과거 lastKnownState가NORMAL이어도 현재 판정은UNKNOWN일 수 있다.
- 새 측정의 판정을 기다리는 동안 검증된 짧은 유예만 RECENT_PERSISTED로 제공한다.
  측정 시각과 lastEvaluatedAt이 다를 수 있으며 허용 시간이 지나면UNKNOWN으로 복귀한다.
- 두 API는 각각 조회 시점의 데이터를 읽는다. 별도 GET은 원자적으로 묶인 조회가 아니다.
  basis.snapshotId가 현재 snapshotId와 같을 때 같은 근거로 대조한다. 다르면 시간·필터·
  관측·평가가 갱신됐을 수 있으므로 다시 조회하고 과거 상태를 강제로 맞추지 않는다.
  식별자 일치를 기다리며 무한 재조회하지 않는다. 계속 새 관측이 들어오면 각 응답의
  기준 시각을 유지해 표시하고 서로 다른 시점의 결과를 동일 관측의 모순으로 처리하지 않는다.
- computedAt/serverTime은 응답 계산 시각. 측정 시각이나 평가 시각을 대신하지 않는다.
- snapshotId는 갱신 비교용 식별자이며 과거 데이터를 다시 읽는 조회 토큰이 아니다.
- scope=ALL에서도 evaluationState는 적재율 판정이다. 장비·측정 오류는 items와coverage로
  함께 판단한다. 경고 표시가 꺼져 items=[]여도 evaluationState가WARNING일 수 있다.

같은 근거인데도 판정이 다르면 두 응답의 snapshotId·시각·reasonCodes·비교 필드를 함께
확인한다. 과거 보고 당시 관측 근거가 보존되지 않은 차이의 원인을 이 기능만으로 단정하지
않는다. UNKNOWN/ERROR를 정상으로 치환하거나 히스테리시스·알림 쿨다운을 바꾸지 않는다.

# 경보 설정의 필드별 검증 오류

대시보드 추가 요청 A-05. `PUT /api/v1/settings/alerts`의 교차 필드 검증 실패는
기존422·detail 배열 구조를 유지하며 다음처럼 반환한다.

```json
{
  "detail": [{
    "type": "alert_threshold_gap",
    "loc": ["body", "preCollectionAlert", "thresholdPercent"],
    "msg": "Pre-alert threshold must be at least 5 percentage points below collection threshold",
    "input": 99,
    "ctx": {"minimumGap": 5, "collectionThresholdPercent": 3}
  }]
}
```

- 기계용 오류 코드는 `detail[].type`의 `alert_threshold_gap`. 영문 msg를 파싱하지 않음.
- loc로 사전 알림 임계율 입력 칸에 안내. 수거 임계율과의 관계 오류이므로 어느 값을
  수정할지는 사용자가 선택. 사전 알림 임계율은 수거 임계율보다 최소5%p 낮아야 함.
- 사전 알림이 비활성이어도 기존 안전 규칙 유지. 정확히5%p 차이는 허용.
- 개별 필드의 범위·자료형·필수값 오류는 기존 Pydantic type/loc를 유지.
- 잘못된 요청으로 저장된 설정·ETag·경보 판정·발송 상태를 변경하지 않음.
- OpenAPI의 기존 RequestValidationIssue.type/loc/ctx 구조를 사용하며 새 최상위 code 필드는 없음.

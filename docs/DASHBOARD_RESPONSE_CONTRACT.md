# 대시보드 응답과 OpenAPI

`GET /openapi.json`에서 응답 타입을 생성할 수 있다. 아래 모델은 실제 응답을 설명하며
기존 인증·권한·ETag·미디어 오류 처리 방식은 유지한다.

## LiDAR 표본

`profiles[].samples[]`는 이름 있는 `LidarProfileSampleV2` 구조다. 처리 과정의 사전 접근을
유지하는 TypedDict 모델이며 OpenAPI에는 객체의 필수 필드와 선택 필드가 선언된다.

| 필드 | 형식 | 의미 |
| --- | --- | --- |
| positionRatio | number 또는 null, 필수 | A→B 측정선의0~1 위치. 투영할 수 없으면 null |
| height | number 또는 null, 필수 | 높이 cm. 계산할 수 없으면 null |
| valid | boolean, 필수 | 해당 점의 높이 사용 가능 여부 |
| angleMdeg | integer, 선택 | SENSOR_ANGLE 수신 각도, 밀리도 |
| sourceIndex | integer, 선택 | SENSOR_ANGLE 원본 표본 인덱스 |

공간 구간 방식에는 각도·인덱스 필드를 추가하지 않는다. SENSOR_ANGLE 무효 점은
원래 각도·인덱스를 보존하며 height/positionRatio가 null일 수 있다. null을0으로 채우지 않는다.
실제 순서·누락·평균·필터 정책은 [높이 계약](LIDAR_HEIGHT_PROFILES.md)과
[시뮬레이션 계약](SIMULATION_HEIGHT_API.md)을 따른다.

`collection-summary.activitySource`는 실제 생성되는 `CONFIRMED_COMPLETION`,
`HEIGHT_SETTLED`, null을 허용한다. 생성하지 않는 HEIGHT_DETECTION은 제외한다.

## 오류 본문

일반 HTTP 오류는 `HTTPErrorResponse`다.

```json
{"detail": "Invalid or missing CSRF token"}
```

422는 의미 검증에서 위 문자열 형식, 요청 필드 검증에서 아래 배열 형식을 사용한다.
로그인 검증 오류는 비밀번호를 되돌려주지 않는 기존 문자열 형식을 유지한다.

```json
{"detail": [{"loc": ["query", "page"], "msg": "Input should be greater than or equal to 1", "type": "greater_than_equal", "input": 0, "ctx": {"ge": 1}}]}
```

미디어 오류는 `MediaErrorResponse`다. 내부 주소·인증값·상위 서비스 오류 원문은 노출하지 않는다.

```json
{"error": {"code": "MEDIA_UNAVAILABLE", "message": "Media service request failed"}}
```

404의 로컬 이벤트 확인 실패는 detail, 미디어 조회 실패는 error일 수 있다. 503도 인증 저장소
장애는 detail, 미디어 장애는 error일 수 있어 해당 경로에는 두 형식을 함께 선언한다.

## 상태 선언 범위

| API | 주요 오류 |
| --- | --- |
| v2 현황·그래프·통계·추이 | 401, 404, 422, 503 |
| 이벤트 녹화 목록 | 401, 404, 422, 503. 목록은 미디어 조회 없음 |
| 이벤트 녹화 상세·날짜 녹화·미디어 클립 | 인증·검증 오류와 미디어의400/404/409/410/415/416/429/502/503/504 |
| 관리자 조회 | 401, 403, 422, 503 |
| 경보 설정 PUT | 공통 오류에412·428 추가 |
| 수신자 생성 | 공통 오류에409 추가 |
| 수신자 PATCH | 공통 오류에404·409·412 추가. If-Match 생략은428로 선언하지 않음 |
| 피트 조회 | 401, 422, 503. 단일 조회404 추가 |
| 피트 수정·영상 삭제·정책 수정·다운로드 준비 | 해당 오류에 권한·CSRF 오류403 추가 |
| 알림 읽음·일괄 읽음 | 401, 403, 422, 503. 단일 알림 수정404 추가 |

미디어 클립 접수는 기존 정상 응답200/202에 ClipReceipt를 선언한다. 날짜 MP4 준비도
기존200/202 모델을 유지한다. 명세 선언 자체는 외부 발송·클립 생성·삭제를 실행하지 않는다.

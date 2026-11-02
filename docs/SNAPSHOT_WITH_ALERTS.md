# 현재 판정과 활성 알림 함께 조회

`GET /api/v2/monitoring/snapshot-with-alerts?pit_id=pit-demo&scope=ALL`

현재 적재율·판정과 활성 알림을 같은 근거로 표시할 때 사용한다. 서버가 현재 snapshot을
한 번 구성한 뒤 그 객체로 활성 알림을 만든다. 두 독립 GET 사이에 새 측정이 도착해
`snapshotId`가 달라지는 문제를 피한다. 과거 snapshot ID를 지정해 조회하는 API는 아니다.

## 요청과 응답

로그인 세션 필수, 조회 사용자와 관리자 모두 허용, CSRF 불필요, 성공 응답은 `no-store`다.
`pit_id`는 기존 명시적 피트·장비 연결을 사용한다. 등록되지 않은 피트는 404,
잘못된 `scope`는 422, 로그인하지 않았으면 401이다.

| 필드 | 내용 |
| --- | --- |
| `snapshot` | 기존 `/snapshot`과 같은 형식의 현재 판정 |
| `activeAlerts` | 기존 `/active-alerts`와 같은 형식의 활성 알림 |

두 필드 모두 항상 제공한다. `activeAlerts.basis.snapshotId`는 함께 반환한
`snapshot.snapshotId`와 같다. 적재율·측정 시각·판정 상태·판정 시각·판정 출처도
함께 반환한 snapshot에서 가져온다. `activeAlerts.computedAt`은 `snapshot.serverTime`이다.
알림을 구성하는 사이 새 관측이나 판정이 저장되어도 다른 snapshot으로 교체하지 않는다.

`scope`는 기존 활성 알림 API와 동일하다.

- 생략 또는 `FILL_OPERATIONS`: 적재율 알림만 조회. 카메라 HTTP 조회 없음.
- `ALL`: 같은 snapshot의 Edge·LiDAR·측정 상태와 기존 카메라 상태 조회를 함께 사용.
  카메라는 독립된 미디어 관측이므로 그 항목의 `evidenceSource`, `evaluatedAt`,
  `validUntilAt`과 coverage를 확인한다. LiDAR와 같은 촬영 시각을 보장하지 않는다.
  미디어 확인 실패는 카메라 `UNKNOWN`으로 격리하고 적재율 판정은 유지한다.

`basis`는 적재율 판정의 근거다. `ALL`의 모든 항목이 적재율과 동일한 센서에서
같은 시각에 측정되었다는 뜻은 아니다. 기존 카메라 요청 시간 제한·캐시를 그대로 사용한다.

## 화면 연결

현재 판정과 활성 알림을 비교할 화면은 이 응답의 두 필드를 한 묶음으로 교체한다.
서로 다른 응답에서 snapshot과 알림을 하나씩 선택하지 않는다. 병렬 요청의 응답 순서가
뒤바뀌면 먼저 시작한 오래된 요청 결과를 무시하는 처리는 소비자가 유지해야 한다.

기존 `/snapshot`, `/active-alerts`, SSE `/events`는 기존 형식과 동작을 유지한다.
이 API 추가만으로 기존 SSE에 활성 알림이 붙지는 않는다. SSE의 독립 snapshot을
받은 뒤 이전 묶음의 알림과 합쳐 동일 시점이라고 표시하지 않는다. 같은 근거 비교가
필요한 시점에는 결합 조회로 받은 두 필드를 함께 사용한다. 단독 SSE 전송과
카메라 상태의 SSE 전송은 별도 계약이다.

## 결측과 부수 효과

- `UNKNOWN`, `ERROR`, `NO_DATA`, `STALE`, 기능 비활성, 필터 준비 중은 기존 의미를 유지한다.
  확인 불가를 정상이나 0%로 바꾸지 않는다.
- `RECENT_PERSISTED`는 기존의 짧은 처리 대기 정책이다. 같은 응답 안에서도 최신 측정 시각과
  마지막 판정 시각은 다를 수 있다. `lastKnownState`를 현재 판정으로 대체하지 않는다.
- 사전 알림 비활성 설정, 히스테리시스, 쿨다운, 과거 알림·읽음 상태는 그대로 유지한다.
  조회가 새 알림·수거 기록·영상 작업을 만들거나 외부 알림을 발송하지 않는다.
- 새 DB 테이블·마이그레이션·과거 snapshot 보관·프레임 바이트 중계는 추가하지 않는다.
  성공·예외·요청 취소 시 읽기 트랜잭션을 종료한다.

`snapshotId`는 snapshot 내용의 변경 식별자이며 시간 순번이 아니다. 같은 ID라도
사전 알림 표시 설정이나 카메라 상태가 바뀔 수 있다. 화면은 결합 응답 전체를 반영하고
snapshot ID가 같다는 이유만으로 새 `activeAlerts`를 버리지 않는다.

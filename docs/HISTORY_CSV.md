# 이력 CSV 다운로드

로그인한 관리자·조회 사용자가 기존 이력을 스프레드시트에서 검토할 수 있는 파일로 받는다.
새 기록·영상·추출 작업을 생성하지 않으며 DB나 서버 디스크에 CSV 파일을 보관하지 않는다.
파일 전체를 제한 안에서 완성한 뒤 응답하므로 조회 실패 시 일부 파일 대신 JSON 오류를 반환한다.

| API | 기존 조회와 같은 조건 |
| --- | --- |
| `GET /api/v2/monitoring/load-history.csv` | `pit_id`, `from`, `to`, `bucket_seconds` |
| `GET /api/v1/events.csv` | `pit_id`, `from`, `to`, `category`, `collection_scope` |

`pit_id`는 설정된 모니터링 소스여야 한다. 기본값은 pit-01이며 미설정 피트는404다.
기간을 생략하면 최근24시간, 최대90일이다. 시각에는 시간대가 필수이며 UTC로 정규화한다.
적재율 구간의 종료는 미포함, 이벤트 종료는 기존 `/api/v1/events`와 같이 포함한다.
카테고리는 all/collection/alert/error, 수거 기준은 current/all이며 기본값은 all/current다.
`collection_scope=all`은 이전 수거 판정 기준의 기록도 포함한다. 자동 종료 감지를 사람의 수거 확인으로 바꾸지 않는다.
이벤트는 전체 선택 범위이며 페이지 번호·크기를 받지 않는다. 알림 센터의 사용자별 읽음/안읽음 목록은 대상이 아니다.

## 파일 형식과 의미

- `text/csv; charset=utf-8`, UTF-8 BOM, 쉼표 구분, CRLF 행 구분, 모든 셀을 큰따옴표로 인용한다.
- `Content-Disposition: attachment`와 ASCII 파일명, `X-CSV-Row-Count`에 헤더를 제외한 행 수를 제공한다.
- 성공과 처리한 오류 응답은 `Cache-Control: no-store`다. 수식처럼 시작하는 문자열은 작은따옴표를 붙여 텍스트로 취급한다.
- 큰따옴표·쉼표·줄바꿈은 CSV 규칙으로 보존한다. 원본 DB 문자열은 변경하지 않는다.
- null은 빈칸, 실제0은0.0 등 숫자, bool은 true/false다. 배열·객체는 셀 안의 JSON이다.
- 원본의 빈 문자열과 null은 CSV 빈칸만으로 구분되지 않을 수 있다. 품질·사유 필드를 함께 본다.
- 문자열 앞의 보호 문자도 다운로드 내용의 일부다. 임의로 제거하거나 스프레드시트에서 저장·재개방할 때 같은 보호가 유지된다고 보장하지 않는다.

적재율에는 피트·site/edge·실제 from/to·bucketSeconds·selectionPolicy와 기존 full JSON의 표본 필드를 모두 포함한다.
수치는 기존 JSON과 같은 표시 정밀도의 %, 시간은 UTC ISO8601이다.
`valuePercent`는 구간 대표값이며 연속 원시 측정 전체가 아니다. 최대1,441표본 제한과 간격 선택은 기존 이력 조회를 따른다.
`valid`, `dataState`, `reasonCodes`, `simulated`, 관측/생성 시각, 평균/최소/최대·유효/무효 표본 수와 출처를 보존한다.
누락 구간을0으로 채우지 않으며 시뮬레이션 여부를 임의로 추정하지 않는다.

이벤트에는 피트·from/to·categoryFilter·collectionScope와 기존 Event JSON 필드를 모두 포함한다.
이벤트 최신순·현재 판정/저장된 해제 근거·수거 기준·자동/수동 구분·시뮬레이션 필드를 유지한다.
알림의 현재 상태는 조회 시점의 기존 판정이며, 해당 기간 내내 유지된 상태나 과거 임계치를 새로 계산한 결과가 아니다.
빈 이벤트 범위는 헤더만 있는 파일이다. 빈 적재율 구간은 기존 JSON처럼 NO_DATA 표본이 포함될 수 있다.
시각 열은 스프레드시트 자동 날짜 변환 대신 UTC 텍스트로 가져오는 것을 권장한다.

## 제한·오류

| 조건 | 응답 |
| --- | --- |
| 미로그인·만료 세션 | 401 |
| 미설정 피트 | 404 |
| 기간·간격·필터 오류 | 422 |
| 최대10,000개 이벤트 입력 후보 초과 | 422 CSV_EVENT_LIMIT_EXCEEDED |
| 피트의 클립 연결 입력10,000개 초과 | 422 CSV_REFERENCE_LIMIT_EXCEEDED |
| 완성 CSV8MiB 초과 | 422 CSV_SIZE_LIMIT_EXCEEDED |
| 총 조회·CSV 변환20초 초과 | 503 CSV_QUERY_TIMEOUT |
| 적재율 기존 조회 제한 초과 | 503 HISTORY_QUERY_TIMEOUT |
| 저장소 오류 | 503 CSV_STORAGE_UNAVAILABLE |

이벤트 입력 제한은 최종 표시 행보다 보수적이다. 이전 기준의 자동 수거·표시하지 않는 원본 전이도 후보 수에 포함될 수 있다.
기간·유형을 좁혀 다시 요청할 수 있으며 일부 행만 잘라 성공시키지 않는다.
기존 이벤트 조립은 피트 전체의 영상 연결을 읽으므로 연결 입력 제한은 기간과 독립적이다. 기간을 줄여도 그 제한이 해소되지 않을 수 있다.
PostgreSQL은 인증 조회를 정리한 뒤 하나의 REPEATABLE READ READ ONLY 트랜잭션으로 제한 확인·이력 조회를 수행한다.
요청 안에서만 statement_timeout을 설정하고 성공·오류·취소 시 rollback하여 연결을 반환한다. DB 전역 설정·권한·스키마는 바꾸지 않는다.
20초는 연결 전 인증·전송·브라우저 저장까지 포함하는 전체 응답 보장값이 아니다.
SQLite는 기존 개발·시험 경로를 사용하며 PostgreSQL과 같은 동시 조회 스냅샷 보장을 주장하지 않는다.

## 프론트 연결

현재 화면의 기간·피트·간격·카테고리·수거 범위를 위 API에 전달한다. 기간은 `period`가 아니라 실제 `from`·`to`다.
버튼을 누를 때만 요청하며 성공 CSV만 파일로 저장한다. 오류 JSON을 .csv로 저장하지 않는다.
같은 출처 로그인 쿠키를 사용하고 프론트·미디어 주소나 쿠키를 쿼리에 넣지 않는다. GET이므로 CSRF 헤더는 필요 없다.
다운로드 중 중복 클릭을 막고 취소·오류 안내를 제공한다. 대시보드 화면·버튼은 이 백엔드 변경으로 자동 추가되지 않는다.

예시(합성 피트 ID, 실제 설정에 맞게 교체):

```text
/api/v2/monitoring/load-history.csv?pit_id=pit-demo&from=2026-01-01T00:00:00Z&to=2026-01-02T00:00:00Z&bucket_seconds=60
/api/v1/events.csv?pit_id=pit-demo&from=2026-01-01T00:00:00Z&to=2026-01-02T00:00:00Z&category=collection&collection_scope=all
```

형식 근거: [Python CSV](https://docs.python.org/3.13/library/csv.html),
문자열 처리 근거와 한계: [OWASP CSV Injection](https://community.owasp.org/attacks/CSV_Injection).

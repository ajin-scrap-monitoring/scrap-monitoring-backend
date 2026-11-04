# 이력 조회 DB 통계 점검·관리

PostgreSQL 17의 `edge_v2_messages` 통계가 새 관측의 시각 범위를 반영하는지 점검한다.
점검은 읽기 전용이며 갱신은 명시적인 관리 명령으로만 실행한다. 앱 시작·수신·API에서
실행하거나 자동 스케줄을 등록하지 않는다.

## 실행과 결과

기존 `DATABASE_URL`과 `EDGE_V2_MONITORING_SOURCES` 설정 환경에서 실행한다.

```bash
uv run python -m src.app.cli.history_statistics check
uv run python -m src.app.cli.history_statistics refresh
```

런타임 이미지에서는 같은 모듈을 `python -m src.app.cli.history_statistics`로 실행한다.
접속 정보·비밀번호를 명령 인자로 받거나 결과에 출력하지 않는다. UTC 시작/완료 시각,
경과 시간과 결과를 JSON 한 줄로 표준 출력한다. 필요하면 비공개 운영 로그로 보관한다.

- `CHECKED`: 성공한 점검. `before.refreshNeeded`와 `reason`을 확인한다.
- `SKIPPED`: 갱신 대상이 아니어서 실행하지 않음. 사유를 확인하며 정상 상태로 단정하지 않는다.
- `ANALYZED`: 지정 열을 갱신함. 새 도착·표본 오차가 있으므로 `after.reason`도 확인한다.
- `FAILED`: 종료 코드1과 안전한 `errorCode`. 권한·잠금/SQL/전체 시간 초과를 구분한다.

옵션 오류는 종료 코드2다. 성공 종료 코드0은 조회 성능이나 실행 계획의 정상 보장이 아니다.
SQL 예외 원문·접속 문자열은 출력하지 않는다. 결과 시각·스키마 등 운영 정보도 공개 커밋하지 않는다.

## 판단 범위

등록 source별 최신 `edge_assessment` 시각을 기존 복합 인덱스로 한 건씩 조회한다.
원문 JSON이나 전체 테이블 개수/최댓값을 스캔하지 않는다. `generated_at` 통계의
히스토그램과 최빈값에서 가장 늦은 시각을 찾아 비교한다. 최빈값이 전체 자료를 설명하면
히스토그램이 없어도 통계가 존재하므로 최빈값을 함께 사용한다.

`--max-lag-seconds` 기본86400초는 하루 뒤처진 통계를 검토하는 운영 기준이며 API SLA나
불변 이력 기준이 아니다. 통계가 없거나 차이가 기준을 넘으면 `STATISTICS_BEHIND`다.
기준 안이면 `WITHIN_LAG_THRESHOLD`, 자료가 없으면 `NO_DATA`다. 현재보다5초 이상
미래인 자료나 통계는 `FUTURE_DATA`/`FUTURE_STATISTICS`로 표시하고 갱신하지 않는다.

통계는 테이블 전체의 표본이며 source별 별도 통계가 아니다. 비교 범위가 맞더라도
분포·선택도·실행 계획이 좋다고 보장하지 않는다. 자료 정확성을 이 도구로 판정하지 않는다.

통계는 있지만 자료가 매우 적어 히스토그램·최빈값이 모두 없으면
`STATISTICS_RANGE_UNAVAILABLE`로 표시하고 갱신을 건너뛴다. 이를 통계 없음이나 정상
상태로 오판하지 않으며 반복 갱신으로 해결하려 하지 않는다.

## 고정 대상·권한·시간 제한

현재 접속 search_path의 기존 `edge_v2_messages` 일반 테이블 하나만 다룬다. 기존 스키마를
확인하고 카탈로그의 스키마명은 인용해 사용한다. 임의 테이블·열·SQL을 받지 않는다.
갱신 열은 `site_id`, `edge_id`, `message_type`, `compact`, `generated_at` 다섯 개이며
ANALYZE의 BUFFER_USAGE_LIMIT은2MB다.

| 옵션 | 기본 | 허용 범위 |
| --- | --- | --- |
| `--max-lag-seconds` | 86400 | 1~604800초 |
| `--statement-timeout-seconds` | 60 | 1~60초 |
| `--lock-timeout-seconds` | 1 | 1~5초 |

각 SQL에는 트랜잭션 한정 제한을 적용하고, 작업 전체에도 statement 제한+10초의 상한을
둔다. 연결 제한은10초다. 긴 잠금 대기는 실패 처리하며 SKIP_LOCKED로 건너뛴 작업을
성공으로 기록하지 않는다. 작업 뒤 연결을 정리하고 영구 제한값은 변경하지 않는다.

점검은 테이블 읽기 권한으로 가능하며 갱신에는 PostgreSQL의 MAINTAIN 또는 소유 권한이
필요하다. 기존 `scrap_app` 권한을 늘리지 않고 기존 소유자용 관리 환경을 사용한다.
권한 부족을 무시하고 성공 처리하지 않는다. SQLite와 PostgreSQL17 이외 DB는 갱신하지 않는다.

`refresh`도 먼저 점검하고 필요한 경우에만 실행한다. 갱신 후에도 차이가 남을 수 있으므로
무한 반복하지 않는다. 원문·업무 데이터·스키마·인덱스·역할·자동 DB 관리 설정·영상·백업은
변경하지 않는다. 자동 실행 주기는 실제 증가량과 점검 결과에 따라 별도 결정한다.

기준: [PostgreSQL17 ANALYZE](https://www.postgresql.org/docs/17/sql-analyze.html),
[pg_stats](https://www.postgresql.org/docs/17/view-pg-stats.html).

# 최신 라이다 관측과 중앙값 판정

`EDGE_V2_COMPACT_STORAGE_ENABLED=true`로 활성화한다. 기본값은 false이며 기존 설치의 동작을 유지한다. 새 이미지 적용 전 백업과 명시적 `python -m src.app.cli.db upgrade` / `check`가 필요하다. Alembic `0002_compact_observations`는 기존 원본을 지우지 않고 4개 테이블과 접수·해시·지속 판정 필드를 추가한다. 전체 업무 테이블은 20개다.

## 저장 내용

| 대상 | 새 모드의 저장 내용 |
| --- | --- |
| `edge_v2_messages` | 접수 ID·내용 해시·생성/접수 시각과 작은 식별 정보. 동일 재전송은 원래 ACK, 다른 내용은 409 |
| `edge_v2_artifacts` | 설정·보정 원문과 불변 식별/내용 해시. 과거 점 배열과 주기 STATE/status 본문은 누적하지 않음 |
| `edge_v2_latest` | 현장/장비별 현재 정기 측정·STATE·status 각각 1건. 점 배열은 정기 측정 한 벌에만 포함 |
| `edge_v2_filters` | 짧은 유효 관측 창, 중앙값과 임계 초과 시작 시각 |
| `edge_v2_minutes` | UTC 분별 안정화한 적재율의 합계·최소·최대·마지막 값과 유효/무효 수. 같은 초의 교체와 부분 조회를 위해 최대 60개의 숫자/null 슬롯 및 대응하는 실제 시각·ID·상태 메타데이터 포함 |
| `edge_v2_transitions` | 장치 오류/복구·측정 유효성·적재 상태 변화의 작은 사건 기록 |
| 사건 | EVENT의 원래 판정·개정·철회와 근거 식별 정보 유지. 과거 스캔의 점 원문은 기본 보관하지 않음 |

접수·불변성 해시, 사건과 분 이력은 누적되므로 전체 DB 크기가 고정되지는 않는다. 장기 보관 기간과 접수 기록의 만료는 별도로 정하며 자동 만료·기존 원문 전체 삭제는 포함하지 않는다. 기존 SQLite 파일이 즉시 줄어드는 변경도 아니다.

접수/해시·필요한 현재 관측·요약/사건을 같은 트랜잭션에 저장한 뒤 ACK를 응답한다. 새 모드의 ACK는 과거 원문 전체의 영구 보관을 뜻하지 않는다. 과거 점 원문이 없어도 내용 충돌은 해시로 검사한다. `EVIDENCE`는 식별/불변성 기록을 유지하며 현재 정기 관측을 덮지 않는다. 과거 점 원문 재생·재판정은 제공하지 않는다.

장비별 DB 잠금과 충돌 안전한 최초 행 생성으로 갱신을 직렬화한다. 재전송이나 같은 초의 추가 보고가 필터 표본 수·유효 초 수를 늘리지 않는다. STATE는 생성 시각, status는 부팅 인스턴스/sequence로 선택한다. 늦은 과거 STATE는 최신 표시/요약을 뒤집지 않으며 분 이력을 소급 복원하지 않는다.

## 중앙값과 수거 판정

시험용 기본값은 최근 5초의 서로 다른 유효 관측 5개 중앙값과 3초 지속 초과 확인이다. 창은 `EDGE_V2_MEDIAN_WINDOW_SECONDS`(3..31 홀수), 지속 시간은 `EDGE_V2_CRITICAL_CONFIRM_SECONDS`(0..60초)로 설정한다. 실제 적재 증가·수거 감소·순간/지속 가림에서 지연과 오경보를 검증해야 한다. 엣지 필터가 있다면 합산 지연도 확인한다.

서로 다른 관측은 UTC 정수 초가 아닌 관측 기준 시각에 맞춘1초 시간 칸으로 구분한다. 같은 칸에서는 최신 값만 유지하며 초 경계의 미세한 차이로 정상1초 간격 관측이 제외되지 않게 한다. 짧은 반복 관측만으로 창을 준비하지 않는다. 기준 시각 scalar는 기존 필터 JSON에 저장하고 재시작 후 이어받으며 이전 형식도 남은 관측으로 준비한다. 자세한 규칙은 [높이 시간 필터](HEIGHT_TEMPORAL_MEDIAN.md)를 따른다.

미검증 적재율·오래된 값·장치 장애·미수신은 사용하지 않는다. 오류, 관측 간격 1.5초 초과, 설정/방법/부팅 인스턴스/임계치 변경은 창을 다시 준비한다. 준비 중 표시값은 null/UNKNOWN이다. 재시작 후 DB에 저장한 창을 이어받지만 신선도는 다시 검사한다. 결측을 0으로 채우거나 이전 정상값을 현재값으로 표시하지 않는다.

`summary.loadPercent`는 안정화한 값, `rawLoadPercent`는 유효한 입력값, `filterState`는 DISABLED/WARMING/READY/UNAVAILABLE이다. 현재 `assessment`에는 원래 판정을 유지한다. 원문을 필터 결과로 덮지 않는다. 활성화한 v2 작업자는 지속 초과를 확인하고 기존 위험/복귀 기준과 30분 중복 알림 차단을 적용한다. 수신마다 확인하므로 작업자 순회가 늦어도 이어진 초과를 인식하며 같은 관측의 반복 평가로 타이머를 진행하지 않는다. RECORDED는 DB 기록으로, 외부 이메일/SMS 발송 완료가 아니다.

## 화면 조회

- 기존 snapshot/SSE는 중앙값과 준비 상태를 노출한다. 기존 세션 인증을 유지한다.
- 기존 load-history는 원문 이력과 새 분 요약을 함께 읽는다. 대표값은 마지막 값이며 평균·최소·최대/유효·무효 수를 추가한다. 새 요약의 `valuePolicy`는 MEDIAN_MINUTE, 기존 원문은 RAW_STATE다. 빈 구간은 null이며 최대90일/1441점으로 조회한다. [장기간 이력 조회](LONG_LOAD_HISTORY.md)의 분 통계·메모리 제한과 간격 규칙을 따른다.
- 분 중간에서 잘라 조회하면 초 슬롯의 값은 남아 있어도 원래 판정 ID/정밀 시각이 없을 수 있다. 해당 필드는 null이고 COMPACT_SUBMINUTE_METADATA_NOT_RETAINED를 명시한다. 미래 판정이나 임의 시각으로 채우지 않는다.
- `GET /api/v2/monitoring/state-events?pit_id=...&limit=20&offset=0`: 오류/복구/적재 상태 변화. 기존 수거 EVENT 개정과 경보 조회도 유지한다.
- `GET /api/v2/monitoring/profiles?pit_id=...`: 센서별 최신 관측 시각·설정/보정 버전·바닥 기준값. 별도 검증 기준을 등록하지 않으면 평균/최소/최대는 null, samples는 빈 배열, calibrationState는 UNVERIFIED다. 명시적인 좌표계·바닥·측정 구간과 검증 보정이 있으면 [높이 프로필](LIDAR_HEIGHT_PROFILES.md)을 계산한다. 기본 설정의 이 화면용 계산은 STATE·시간 중앙값·분 이력·수거 판정에 입력되지 않는다. 선택적 단면 입력 연결은 [HEIGHT_FILL_PIPELINE.md](HEIGHT_FILL_PIPELINE.md)를 따른다.

`EDGE_V2_EMPTY_FLOOR_DISTANCE_CM` 기본 1000cm는 임시 센서-빈바닥 거리이며 피트 총깊이와 별개다. 비공개 실행 설정을 바꾸고 앱을 재시작할 수 있다. 관리자 화면의 보정 편집은 포함하지 않는다. 좌표 원점·높이 방향·설치 각도 확인 전에는 반사점 z나 단순 거리 차를 검증된 표면 높이로 쓰지 않는다. 임시값으로 실제 수거 알림을 만들지 않는다.

## 검증과 적용

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest -v
uv run python -m tests.integration.verify_postgresql_api
uv run python -m tests.integration.verify_db_migrations
uv run python -m tests.integration.verify_db_transfer
uv run python -m tests.integration.verify_db_backup
```

합성 시험은 점 배열 한 벌 유지, 과거 메시지/근거 충돌, 최초 동시 수신, 순간 튐/실제 지속 상승, 히스테리시스/쿨다운, 단절·오류·방법 변경·재시작, 분 요약/부분 구간, 사건/개정 보존, 미검증 높이 null, 기존 원문의 migration 보존을 확인한다. 실제 서버 적용과 기존 DB 삭제/정리는 별도다. 스키마/권한 적용·설정 활성화 뒤 실장비/프론트 연결과 저장량을 검증한다.

기술 참고: [NIST 중앙값 설명](https://www.itl.nist.gov/div898/handbook/eda/section3/eda351.htm), [SQLAlchemy 트랜잭션](https://docs.sqlalchemy.org/en/20/orm/session_transaction.html).

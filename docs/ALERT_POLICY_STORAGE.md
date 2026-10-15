# 알림 정책 DB 저장

관리자 화면의 알림 정책을 피트별 `alert_policies` 테이블에 저장한다. 기존 피트 임계치는 `pit_configs`에 유지하고, 한 번의 저장 요청에서 두 테이블을 같은 트랜잭션으로 갱신한다. 실패하면 함께 롤백한다.

## 저장 항목

| API 필드 | 저장 위치 |
| --- | --- |
| collectionThresholdPercent | pit_configs.critical_threshold_percent |
| preCollectionAlert.thresholdPercent | pit_configs.warning_threshold_percent |
| preCollectionAlert.enabled | alert_policies.pre_alert_enabled |
| sendDelayMinutes | alert_policies.send_delay_minutes |
| repeatIntervalMinutes | alert_policies.repeat_interval_minutes |
| maximumRepeatCount | alert_policies.maximum_repeat_count |
| recoveryNotificationEnabled | alert_policies.recovery_notification_enabled |
| eventTypes | alert_policies.event_types |

정책 테이블의 pit_id는 PK이자 pit_configs.id의 FK다. 버전과 생성·변경 시각을 함께 저장한다. JSON 목록은 SQLite에서는 JSON, PostgreSQL에서는 JSONB를 사용한다. 정책 저장으로 피트 이름·깊이나 다른 피트의 설정을 바꾸지 않는다.

## 기존 데이터와 초기값

현재 DB 초기화 방식은 새 alert_policies 테이블을 생성한다. 기존 피트의 첫 관리자 설정 GET에서 정책 행이 없으면 기존 계약의 기본 정책을 저장한다. 기존 임계치는 그대로 사용한다. 이미 저장된 정책을 초기값으로 덮어쓰지 않는다. 같은 피트에 대한 최초 조회가 겹쳐도 고유 키 충돌은 중복 초기화로 처리한다.

기본 정책은 사전 알림 사용, 발송 지연 0분, 반복 간격 30분, 최대 3회, 복구 알림 사용, collection_required/measurement_error/device_error다. 과거 코드가 DB에 저장하지 않았던 사용자 설정은 복원할 수 없으며 필요한 경우 다시 저장해야 한다.

기존 테이블 컬럼은 변경하지 않는다. 운영 스키마 버전 관리와 전체 SQLite→PostgreSQL 이전 절차는 별도 단계다. 이번 변경만으로 운영 DB 전환이 완료된 것은 아니다.

## GET·PUT과 충돌 처리

API는 기존 `/api/v1/settings/alerts`를 사용한다. 로그인·관리자 권한·변경 시 CSRF 검사는 유지한다.

1. GET에서 현재 설정과 ETag를 받는다.
2. PUT에 `X-CSRF-Token`과 `If-Match: <GET 응답 ETag>`를 함께 보낸다.
3. 임계치·정책 전체를 저장하고 새 ETag를 받는다.
4. 재조회 또는 서버 재시작 후에도 모든 필드가 같은 값으로 반환된다.

**If-Match는 이제 필수다.** 없으면 428, 최신 버전과 다르면 412다. 기존 화면에 남은 이전 형식 ETag도 최신 GET으로 다시 받아야 한다. 다른 피트 설정 API로 임계치가 바뀐 경우에도 오래된 ETag는 거절한다. 설정 저장소 오류는 내부 SQL이나 값을 노출하지 않는 503이다.

PostgreSQL은 피트와 정책 행을 같은 순서로 잠그고 버전을 확인한다. 버전·기존 임계치·변경 시각 조건을 포함한 갱신으로 덮어쓰기를 막는다. SQLite의 동시 쓰기 잠금으로 실패하면 503일 수 있으며, 데이터가 섞이지 않도록 전체 롤백한다. 이런 경우 최신 GET 후 다시 저장한다.

## 적용 범위

이번 작업은 **관리자 정책의 저장·조회 보완**이다. 경보 작업자의 모든 발송 정책 적용이나 실제 이메일/SMS 발송까지 구현한 것은 아니다. 기존 경보의 상태 판정·히스테리시스·30분 중복 억제는 유지한다. 저장한 반복 간격이 기존 안전 기준을 자동 완화하지 않는다.

현재 실시간 상태 응답의 사전 알림 표시와 실제 발송 동작까지 이 정책을 적용하는 작업은 별도다. API에서 저장됐다는 응답을 받았다고 외부 발송까지 성공했다고 표시하지 않는다.

## 검증

설정 전 항목의 재조회, 독립 DB 세션, 파일 DB 재연결, 기존 피트 초기화, 피트별 분리, 정책만 변경 시 ETag 갱신, 오래된 저장 차단, 동시 쓰기, 저장/초기화 실패 롤백을 검증한다. 실제 Docker/Nginx에서도 정책을 저장한 후 백엔드를 재시작해 값과 ETag·로그인이 유지되는지 확인한다.

초기화의 충돌 처리 근거는 SQLAlchemy 공식 [SQLite INSERT ON CONFLICT](https://docs.sqlalchemy.org/en/20/dialects/sqlite.html#insert-on-conflict-upsert), [PostgreSQL INSERT ON CONFLICT](https://docs.sqlalchemy.org/en/20/dialects/postgresql.html#insert-on-conflict-upsert)를 따른다.

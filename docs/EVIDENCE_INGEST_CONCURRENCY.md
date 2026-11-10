# EVIDENCE 수신의 불필요한 투영 잠금 제거

실제 라이다 판단을 활성화하면 원본 근거 EVIDENCE가 추가된다.
기존 compact 수신은 모든 메시지에 장치 단위 projection_filter 잠금을 잡았지만,
EVIDENCE의 project 함수는 live 투영을 변경하지 않고 즉시 반환한다.

실제 입력의 읽기 전용 PostgreSQL 상태 관측에서 transactionid·tuple 잠금 대기가
projection_filter에 집중되는 것을 확인했다. EVIDENCE 수신은 이 불필요한 잠금만 생략한다.

- 원본 해시·장치 범위·멱등성·artifact 불변성·commit 후 ACK는 유지한다.
- artifact unique constraint와 savepoint 재시도로 동시 수신의 동일·충돌 데이터를 구분한다.
- PERIODIC·STATE·STATUS의 기존 투영 잠금은 유지한다.
- SQLite 단일 수신/재전송/충돌·실시간 잠금 회귀와 별도 PostgreSQL의
  동시 EVIDENCE·동일 message_id·상충 원본 회귀를 검증한다.
- 실제 DB 스키마·그래프·필터·보관 정책은 변경하지 않는다.

Pi 내구 저장의 반복 디렉터리 스캔 개선과 별개로 검증하며,
실제 입력의 전송 대기가 지속 증가하지 않는지 확인하기 전에는 판단 상시 활성화를 확정하지 않는다.

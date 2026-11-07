# C 우선만 / A 무보증 제거

- 출발 HEAD 01f4be7; 이전 구현 0a67422 및 보류 d16988c/01f4be7 보고서 확인.
- 기존 dirty market-test.log/test-report.json 및 untracked 원본은 보존/미스테이징.
- week-08 README/checker 없음. 운영 state/.market/키/env/pem 접근 금지.
- 이번 범위는 로컬 수정과 원격 격리 후보 검증뿐. 운영 교체/재시작/push 없음.
- 이전 격리 harness를 새 고유 history에서 재사용하고 baseline부터 실행한다.

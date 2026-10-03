"""검수: 사람이 셀 이미지를 보고 값을 입력한다. 정답 만들기와 운영 중 검수가 같은 도구, 같은 데이터다.

  store.py    검수 기록의 저장(추가 전용 파일 → DB) 과 재적용
  queue.py    무엇을 보여 줄 것인가 (운반 숫자 표본, 교차검증 불일치, 검수 대기)
  crops.py    정합 이미지에서 셀·행 잘라 내기
  server.py   로컬 웹 화면 (표준 라이브러리만)

설계는 docs/tasks/0001-review-tool.md 4절, 결정 기록은 docs/decisions/0008-review-records.md.
"""

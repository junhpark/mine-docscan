"""표 밖 메타 필드 읽기 — 차량번호·작성자·날짜의 월·일 (tasks/0004).

  choose.py   숫자 모델(CTC)의 점수로 닫힌 목록에서 고르기: 후보 가능도, 정규화한 신뢰도, 목록에 없는 값
  calib.py    온도, 임계값별 자동 적재율·오류율, 기준 (0003 의 규칙 그대로 — ADR 0012)
  model.py    모델 폴더(card.json + classes.json + model.onnx) 읽기, 후보 목록(학습 때 본 값 + 템플릿), 설정 [recognize.meta]
  train.py    학습 (torch): --cv K(날짜 묶음을 돌려 가며) 또는 검증 날짜, 숫자 모델은 digits 의 망, 분류기는 recognize/choice
  evaluate.py 크롭 단위 평가 (recognizer eval)

읽는 법은 둘이다 (4.1): 숫자로 된 필드는 내용을 읽고 목록에서 고른다(digits — 글씨체가 아니라 숫자를 읽는다),
이름은 닫힌 집합 분류기로 고른다(choice — 글씨체가 곧 단서다).

설정:
  [recognize.meta]
  vehicle_no = "veh-v1"            # <site>/models/<이름>/ 또는 폴더 경로. 설정 파일 > site.toml
  operator = "op-v1"
  "date.day" = "date-v1"           # 점이 든 키는 따옴표로 (따옴표 없이 써도 받는다)
"""

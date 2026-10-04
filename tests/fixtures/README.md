# tests/fixtures

시험에 쓰는 고정 파일. **실제 문서·실제 글씨는 여기에 넣지 않는다** (CLAUDE.md, docs/DATA.md).

## digits-fixture/ — 숫자 인식기 시험용 모델

합성 셀(`tools/synth_cells.py`)만으로 학습한 모델이다. torch 가 없는 환경에서 `digits` 백엔드의 추론 경로
(ONNX → OpenCV, 자동 적재 규칙)를 시험하려고 둔다 (tasks/0003 4.5). 손글씨 인식률과는 관계가 없다.
`model.onnx` 는 `.gitignore` 의 `*.onnx` 에서 이 한 파일만 예외다.

다시 만드는 명령 (torch 필요: `pip install -e ".[train]"`, CPU 4코어에서 약 2분):

```bash
minedocscan recognizer train --name digits-fixture --synthetic-geometry 112x22,92x21 --target-auto-error 0.005 \
    --out tests/fixtures/digits-fixture --allow-in-repo
```

칸 크기 112x22 는 낮은 칸 합성 양식(`synth --low-cells`)의 숫자 칸, 92x21 은 실제 운반 칸과 같은 크기다.
자동 적재 목표를 0.005 로 둔 것은 다시 학습해도 단계 5 의 문턱(자동 적재 오류 1 % 이하)을 여유 있게 넘게 하려는 것이다.
바이트까지 같을 필요는 없다 — CI 의 `pytest -m train` 이 다시 만든 모델로 같은 문턱을 확인한다.

## meta-digits/ — 메타 필드 숫자 모델 (차량번호, 월, 일)

합성 메타 필드(`tools/synth_meta.py` — 사람마다 다른 획, 네 자리 차량번호, 월·일)만으로 학습한 숫자 모델 (tasks/0004 단계 3).
torch 없는 시험에서 닫힌 목록 고르기·쪽 메타 채우기·대조를 돌린다. 이름·차량번호는 전부 합성 값이다 (`classes.json`).

```bash
minedocscan recognizer train --meta-key vehicle_no,date.month,date.day --synthetic-meta 60 --steps 1500 \
    --name meta-digits --out tests/fixtures/meta-digits --allow-in-repo
```

합성 60일 × 6명. 검증 날짜(train 날짜의 20 %)를 그 날짜 없이 학습한 모델로 읽어 기준을 정한다 (자동 적재된 읽기 100 이상, 목표 2 %).

## meta-operator/ — 작성자 분류기

합성 작성자 6명(`tools/synth_meta.py` 의 ROSTER — 사람마다 다른 획으로 자기 이름을 쓴다)만으로 학습한 닫힌 집합 분류기 (tasks/0004 단계 4).
처음 보는 사람(STRANGERS)이 쓴 쪽은 "그 밖"으로 거절되는지, torch 없이 OpenCV 로 도는지 시험한다.

```bash
minedocscan recognizer train --meta-key operator --synthetic-meta 100 --name meta-operator \
    --out tests/fixtures/meta-operator --allow-in-repo
```

합성 100일 × 6명. 검증 날짜(20 %)를 읽은 168번으로 기준을 정한다.

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

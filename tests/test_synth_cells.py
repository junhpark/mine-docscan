"""어려운 합성 셀 (tasks/0003 단계 3): 같은 씨앗이면 같은 셀, 구성은 인자대로, 낮은 칸 양식은 oracle 로 CER 0."""
from collections import Counter

import numpy as np
import pytest

from minedocscan.config import Settings
from minedocscan.evaluate.fields import evaluate_fields
from minedocscan.imaging.cropspec import CropSpec
from minedocscan.pipeline import Pipeline
from minedocscan.recognize import OracleRecognizer, load_answers_json
from minedocscan.tools.synth import LOW_ROW_H, T_LOG, T_MATRIX, generate
from minedocscan.tools.synth_cells import CellParams, make_cell, make_cells


def test_same_seed_same_pixels():
    a = make_cells(40, seed=11)
    b = make_cells(40, seed=11)
    assert all(np.array_equal(x.image, y.image) and x.text == y.text and x.kind == y.kind for x, y in zip(a, b, strict=True))
    c = make_cells(40, seed=12)
    assert any(not np.array_equal(x.image, y.image) for x, y in zip(a, c, strict=True))
    # 셀 하나는 (씨앗, 종류, 이웃 여부)로만 정해진다 — 학습 중에 셀을 그때그때 만들어도 다시 만들 수 있다
    one = make_cell(np.random.default_rng([11, 7]), CellParams(), "x", True)
    assert np.array_equal(one.image, make_cell(np.random.default_rng([11, 7]), CellParams(), "x", True).image)


def test_composition_follows_the_parameters():
    p = CellParams(p_value=0.5, p_x=0.1, p_scribble=0.05, p_note=0.05, p_blank=0.1, p_neighbor=0.6)
    n = 200
    cells = make_cells(n, seed=3, params=p)
    kinds = Counter(c.kind for c in cells)
    assert kinds == {"value": 100, "x": 20, "scribble": 10, "note": 10, "blank": 20, "spill": 40}
    assert sum(c.neighbors for c in cells) == 120
    assert all(c.neighbors for c in cells if c.kind == "spill")
    # 정답: 값 있음은 숫자열(앞의 0 없음), 나머지는 빈 칸
    assert all(c.text.isdigit() and not c.text.startswith("0") for c in cells if c.kind == "value")
    assert all(c.text == "" for c in cells if c.kind != "value")
    # 크기는 규격대로 (imaging/cropspec 이 같은 칸을 뜬 크기)
    assert {c.image.shape for c in cells} == {(p.out_size()[1], p.out_size()[0])}
    with pytest.raises(ValueError):
        make_cells(10, params=CellParams(p_value=0.9, p_x=0.2))
    with pytest.raises(ValueError):
        make_cells(10, params=CellParams(p_neighbor=0.05))          # spill 칸 몫보다 작다


@pytest.mark.parametrize("spec,cell", [(CropSpec("source", 1.5, None), (100, 29)), (CropSpec("aligned", 1.0, 0), (110, 30)),
                                       (CropSpec("aligned", 2.0, 5), (90, 28))])
def test_cell_size_and_spec_are_parameters(spec, cell):
    p = CellParams(cell_w=cell[0], cell_h=cell[1], spec=spec)
    w, h = p.out_size()
    pad = spec.pad_for((0, 0, *cell))
    assert (w, h) == (round((cell[0] + 2 * pad) * spec.scale), round((cell[1] + 2 * pad) * spec.scale))
    assert {c.image.shape for c in make_cells(12, seed=5, params=p)} == {(h, w)}


def test_low_cell_forms_pass_with_the_oracle(tmp_path):
    """낮은 칸·거친 숫자·X 표 양식: 인식기가 아니라 파이프라인을 확인하는 것 — oracle 로 CER 0."""
    synth = generate(tmp_path / "data", days=1, seed=0, low_cells=True)
    answers = load_answers_json(synth.answers_path)
    settings = Settings(site=synth.site, archive_root=synth.scans, work_root=tmp_path / "work", reviews=tmp_path / "r.jsonl")
    pipe = Pipeline(settings, recognizer=OracleRecognizer(answers))
    pipe.run([synth.scans])
    res = evaluate_fields(pipe.con, answers)
    assert res["cer"] == 0.0 and res["field_accuracy"] == 1.0 and res["answers_not_in_db"] == 0
    assert set(res["by_field_kind"]) >= {f"{T_LOG}/handwritten_number", f"{T_MATRIX}/handwritten_number"}
    con = pipe.con
    heights = {r[0] for r in con.execute("SELECT y1 - y0 FROM doc_field WHERE kind = 'handwritten_number'")}
    assert max(heights) <= LOW_ROW_H                                  # 칸이 낮다 (괘선 안쪽 여백을 뺀 높이)
    # 어려운 칸이 실제로 있다: 잉크로는 값이 있는데 정답은 빈 칸 (X 표, 이웃 칸에서 넘어온 획)
    hard = con.execute("SELECT COUNT(*) FROM doc_field WHERE kind = 'handwritten_number' AND has_value_raw = 1 "
                       "AND (value_raw IS NULL OR value_raw = '')").fetchone()[0]
    assert hard > 0

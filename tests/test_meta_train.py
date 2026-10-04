"""메타 필드 모델 학습 (tasks/0004 단계 3·4) — torch 가 있어야 돈다: `pytest -m train`.

수치는 합성 필드에 대한 문턱이다 — 인식률이 아니다. 합성 크롭은 tools/synth_meta (사람마다 다른 획).
"""
import json

import pytest

from minedocscan.cli import main
from minedocscan.recognize.digits.data import read_crops
from minedocscan.recognize.digits.model import preprocess, read_answers
from minedocscan.recognize.meta.model import MetaModel
from minedocscan.recognize.meta.train import MetaTrainArgs, cv_folds, train_meta
from minedocscan.tools import synth_meta

pytestmark = pytest.mark.train
torch = pytest.importorskip("torch")
pytest.importorskip("onnx")


@pytest.fixture(scope="module")
def vehicle_model(tmp_path_factory):
    """번호마다 쓰는 사람이 정해진 합성 50일치(6명 × 50쪽)로 학습한 차량번호 모델."""
    root = tmp_path_factory.mktemp("meta_vehicle")
    synth_meta.write_meta_crops(root / "crops", ("vehicle_no",), 50, seed=0)
    card = train_meta(root / "crops", root / "model", MetaTrainArgs(name="veh", keys=("vehicle_no",), steps=1500,
                                                                     eval_every=500))
    return {"root": root, "card": card, "model": MetaModel(root / "model")}


def _read(m: MetaModel, folder, key="vehicle_no"):
    crops = read_crops(folder, allow_test=True, meta_keys=(key,))
    cands = m.candidates(key)
    out = []
    for s in crops.samples:
        img = s.image()
        out.append((s, m.read(img, key, cands), read_answers(m.net.logits(preprocess(img, m.position)), m.temperature)[0][0]))
    return out


def test_vehicle_numbers_are_read_not_recognized_by_handwriting(vehicle_model, tmp_path):
    """학습: 번호마다 쓰는 사람이 정해져 있다. 시험: 사람과 번호의 짝을 바꾼 쪽만 → 닫힌 목록으로 고른 답 0.95 이상.
    짝이 그대로인 쪽까지 합치면 0.97 이상이고 자유롭게 읽은 답보다 낮지 않다. 목록에 없는 번호는 "목록에 없는 값"(0.9 이상)."""
    m = vehicle_model["model"]
    assert set(m.values("vehicle_no")) == set(synth_meta.VEHICLES)
    swap = {w: synth_meta.VEHICLES[(i + 1) % len(synth_meta.ROSTER)] for i, w in enumerate(synth_meta.ROSTER)}
    synth_meta.write_meta_crops(tmp_path / "swap", ("vehicle_no",), 20, seed=9, start="2031-01-01", vehicles=swap,
                                split="test")
    synth_meta.write_meta_crops(tmp_path / "same", ("vehicle_no",), 10, seed=8, start="2031-06-01", split="test")
    swapped = _read(m, tmp_path / "swap")
    same = _read(m, tmp_path / "same")
    acc = lambda rows: sum(c.answer == "value" and c.value == s.text for s, c, _f in rows) / len(rows)   # noqa: E731
    free = lambda rows: sum(f == s.text for s, _c, f in rows) / len(rows)                              # noqa: E731
    print(f"짝을 바꾼 쪽 {len(swapped)}: 목록에서 고름 {acc(swapped):.3f}, 자유롭게 읽음 {free(swapped):.3f}; "
          f"전체 {acc(swapped + same):.3f} / {free(swapped + same):.3f}")
    assert acc(swapped) >= 0.95
    assert acc(swapped + same) >= 0.97 and acc(swapped + same) >= free(swapped + same)
    new = {w: synth_meta.NEW_VEHICLES[i % len(synth_meta.NEW_VEHICLES)] for i, w in enumerate(synth_meta.ROSTER)}
    synth_meta.write_meta_crops(tmp_path / "new", ("vehicle_no",), 10, seed=10, start="2032-01-01", vehicles=new,
                                split="test")
    unseen = _read(m, tmp_path / "new")
    unl = sum(c.answer == "unlisted" for _s, c, _f in unseen) / len(unseen)
    print(f"목록에 없는 번호 {len(unseen)}: 목록에 없는 값으로 답함 {unl:.3f}")
    assert unl >= 0.9 and not any(m.status(c) == "auto" for _s, c, _f in unseen)


def test_card_has_no_values_and_candidates_ignore_reviews(vehicle_model):
    """카드·로그에는 값(차량번호)이 없다 — classes.json 에만. 후보 목록은 모델 폴더와 템플릿에서만 온다."""
    d = vehicle_model["root"] / "model"
    assert {p.name for p in d.iterdir()} == {"model.onnx", "card.json", "classes.json", "train-log.jsonl"}
    text = (d / "card.json").read_text(encoding="utf-8") + (d / "train-log.jsonl").read_text(encoding="utf-8")
    for v in synth_meta.VEHICLES + synth_meta.ROSTER:
        assert v not in text, v
    card = vehicle_model["card"]
    assert card["meta"]["reader"] == "digits" and card["meta"]["keys"] == ["vehicle_no"]
    assert card["meta"]["classes"]["vehicle_no"]["n"] == len(synth_meta.VEHICLES)
    assert card["validation"]["export_check"]["max_abs_diff"] < 1e-3 and card["validation"]["export_check"]["same_answers"]
    assert (d / "model.onnx").stat().st_size < 1_000_000


def test_cv_folds_are_disjoint_dates_without_test(tmp_path):
    """--cv 5: 묶음의 날짜가 서로 겹치지 않고 train 날짜 전부를 덮는다. 기준·온도가 방식과 함께 카드에. test 줄은 거절."""
    synth_meta.write_meta_crops(tmp_path / "c", ("date.day",), 12, seed=1)
    card = train_meta(tmp_path / "c", tmp_path / "m", MetaTrainArgs(name="d", keys=("date.day",), steps=30, eval_every=30,
                                                                     cv=5, synthetic=200))
    cv = card["meta"]["cv"]
    assert cv["method"] == "cv5" and cv["k"] == 5 and len(cv["folds"]) == 5
    dates = [d for f in cv["folds"] for d in f["dates"]]
    assert len(dates) == len(set(dates)) == 12
    assert card["validation"]["reads"] == sum(f["cells"] for f in cv["folds"]) == 12 * len(synth_meta.ROSTER)
    assert card["temperature"] > 0 and card["auto_accept"]["basis"].endswith("(cv5)")
    assert card["auto_accept"]["met"] is False and "100" in card["auto_accept"]["reason"]     # 72번 읽기로는 기준이 없다
    assert cv_folds(dates, "s", 5) == cv_folds(list(reversed(dates)), "s", 5)              # 순서와 무관
    # 묶음 교차 읽기는 값 없이 모델 폴더에 남고, recognizer eval --split val 이 그것으로 카드와 같은 표를 낸다
    rows = [json.loads(line) for line in (tmp_path / "m" / "cv-reads.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(rows) == card["validation"]["reads"] and {r["fold"] for r in rows} == set(range(5))
    assert set(rows[0]) == {"field_id", "key", "date", "fold", "correct", "confidence", "answer", "truth_listed"}
    from minedocscan.recognize.meta.evaluate import evaluate_meta

    r = evaluate_meta(tmp_path / "c", tmp_path / "m", split="val", errors=tmp_path / "err")
    assert r["score"] == card["validation"]["score"] and r["thresholds"] == card["auto_accept"]["table"]
    assert r["cells"] == len(rows) and "cv-reads" in r["source"]
    assert r["errors"] == 0 or r["errors_image"] is not None
    synth_meta.write_meta_crops(tmp_path / "c", ("date.day",), 2, seed=2, start="2031-01-01", split="test")
    from minedocscan.recognize.digits.train import TrainError

    with pytest.raises(TrainError, match="test"):
        train_meta(tmp_path / "c", tmp_path / "m2", MetaTrainArgs(name="d", keys=("date.day",), steps=5, cv=5))


def test_cli_trains_from_synthetic_meta_and_prints_no_values(tmp_path, capsys):
    out = tmp_path / "m"
    assert main(["recognizer", "train", "--meta-key", "vehicle_no", "--synthetic-meta", "6", "--steps", "20",
                 "--synthetic", "200", "--name", "veh-x", "--out", str(out), "--json"]) == 0
    stdout = capsys.readouterr()
    card = json.loads(stdout.out)["card"]
    assert card["meta"]["keys"] == ["vehicle_no"] and (out / "classes.json").is_file()
    for v in synth_meta.VEHICLES:
        assert v not in stdout.out and v not in stdout.err
    # 숫자 칸 백엔드에 꽂으면 거절
    from minedocscan.recognize.digits.backend import DigitsRecognizer

    with pytest.raises(ValueError, match="recognize.meta"):
        DigitsRecognizer(out)


# ── 분류기 (단계 4) ─────────────────────────────────────────────────────────
def test_operator_classifier_from_five_examples(tmp_path, capsys):
    """합성 작성자 6명, 종류당 예 5개(5일치)로 학습한 분류기가 다른 날짜의 쪽에서 0.95 이상. torch ↔ OpenCV 출력 차이 1e-3 미만이고
    고른 종류가 같다. 카드·학습 로그·표준 출력에 이름이 없다. 학습에 없던 사람이 쓴 쪽은 자동 적재되지 않는다."""
    synth_meta.write_meta_crops(tmp_path / "c", ("operator",), 5, seed=0)
    capsys.readouterr()
    card = train_meta(tmp_path / "c", tmp_path / "m", MetaTrainArgs(name="op", keys=("operator",), steps=600, eval_every=300,
                                                                     val_share=0.0))
    io = capsys.readouterr()
    m = MetaModel(tmp_path / "m")
    assert m.reader == "choice" and m.values("operator") == sorted(synth_meta.ROSTER)
    assert card["meta"]["classes"]["n"] == 6 and card["meta"]["classes"]["per_class"] == {"min": 5, "median": 5.0, "max": 5}
    ec = card["validation"]["export_check"]
    assert ec["max_abs_diff"] < 1e-3 and ec["same_answers"] and ec["cells"] > 30
    text = "".join((tmp_path / "m" / f).read_text(encoding="utf-8") for f in ("card.json", "train-log.jsonl")) + io.out + io.err
    for name in synth_meta.ROSTER + synth_meta.STRANGERS:
        assert name not in text and name.lower() not in text, name
    synth_meta.write_meta_crops(tmp_path / "t", ("operator",), 10, seed=9, start="2031-01-01", split="test")
    synth_meta.write_meta_crops(tmp_path / "s", ("operator",), 10, seed=10, start="2032-01-01", writers=synth_meta.STRANGERS,
                                split="test")
    for folder, known in ((tmp_path / "t", True), (tmp_path / "s", False)):
        rows = [(s, m.read(s.image(), "operator", m.candidates("operator")))
                for s in read_crops(folder, allow_test=True, meta_keys=("operator",)).samples]
        acc = sum(c.answer == "value" and c.value == s.text for s, c in rows) / len(rows)
        auto = sum(m.status(c) == "auto" for _s, c in rows) / len(rows)
        print(f"{'아는' if known else '처음 보는'} 사람 {len(rows)}쪽: 정확도 {acc:.3f}, 자동 적재 {auto:.3f}")
        if known:
            assert acc >= 0.95
        else:
            assert auto <= 0.05 and acc == 0


def test_rare_values_are_not_classes(tmp_path):
    """학습 날짜에 예가 min_examples 미만인 값은 종류가 아니다 — "그 밖"으로 학습하고 그 수만 카드에."""
    synth_meta.write_meta_crops(tmp_path / "c", ("operator",), 4, seed=0)
    synth_meta.write_meta_crops(tmp_path / "c2", ("operator",), 2, seed=1, start="2030-05-01", writers=("MIKE",))
    import shutil

    for p in (tmp_path / "c2" / "train" / "meta" / "operator").iterdir():
        shutil.copy(p, tmp_path / "c" / "train" / "meta" / "operator" / p.name)
    with open(tmp_path / "c" / "train" / "meta" / "labels.jsonl", "a", encoding="utf-8") as f:
        f.write((tmp_path / "c2" / "train" / "meta" / "labels.jsonl").read_text(encoding="utf-8"))
    card = train_meta(tmp_path / "c", tmp_path / "m", MetaTrainArgs(name="op", keys=("operator",), steps=20, eval_every=20,
                                                                     val_share=0.0))
    cls = card["meta"]["classes"]
    assert cls["n"] == 6 and cls["dropped_values"] == 1 and cls["dropped_examples"] == 2
    assert "MIKE" not in json.loads((tmp_path / "m" / "classes.json").read_text(encoding="utf-8"))["classes"]


# ── 시험용 모델을 다시 만들어도 (tasks/0004 6절) ────────────────────────────────
def test_regenerated_meta_fixtures_meet_stage5(tmp_path):
    """tests/fixtures 의 메타 모델 두 개를 README 의 명령으로 다시 만들어도 단계 5 의 문턱을 넘는다 (바이트가 같을 필요는 없다):
    라벨 없이 돌린 합성 묶음에서 자동 적재된 차량번호·작성자 중 틀린 것 2 % 이하, 일보의 자리가 라벨을 준 실행과 같은 쪽 0.95 이상."""
    from pathlib import Path

    from conftest import meta_options, meta_run
    from minedocscan.tools.synth import generate

    readme = (Path(__file__).parent / "fixtures" / "README.md").read_text(encoding="utf-8").replace("\\\n    ", "")
    cmds = {"meta-digits": ["--meta-key", "vehicle_no,date.month,date.day", "--synthetic-meta", "60", "--steps", "1500",
                            "--name", "meta-digits"],
            "meta-operator": ["--meta-key", "operator", "--synthetic-meta", "100", "--name", "meta-operator"]}
    for name, args in cmds.items():
        assert " ".join(args) in readme, name                                  # README 의 명령과 같은 인자
        assert main(["recognizer", "train", *args, "--out", str(tmp_path / name)]) == 0
    synth = generate(tmp_path / "data", days=4, seed=3, meta_fields=True)
    truth = json.loads((synth.site / "labels" / "pages.json").read_text(encoding="utf-8"))
    opts = meta_options(tmp_path / "meta-digits", tmp_path / "meta-operator")
    run = meta_run(synth, tmp_path / "nolabels", labels={}, options=opts)
    lab = meta_run(synth, tmp_path / "labeled", options=opts)
    con = run["pipe"].con
    rows = con.execute("SELECT m.meta_key, m.machine_status, m.machine_value, d.source_name || '#' || p.page_no AS src "
                       "FROM doc_page_meta m JOIN doc_page p ON m.page_id = p.page_id JOIN doc_document d ON "
                       "p.document_id = d.document_id WHERE m.meta_key IN ('vehicle_no', 'operator')").fetchall()
    auto = [r for r in rows if r["machine_status"] == "auto"]
    wrong = sum(r["machine_value"] != truth[r["src"]][r["meta_key"]] for r in auto)
    slots = lambda c: dict(c.execute("SELECT page_id, MAX(slot) FROM prod_haul WHERE source_role='log' GROUP BY 1"))  # noqa: E731
    got, want = slots(con), slots(lab["pipe"].con)
    same = sum(got.get(p) == s and s is not None for p, s in want.items())
    print(f"다시 만든 메타 모델: 자동 적재 {len(auto)}/{len(rows)}, 틀린 것 {wrong}; 자리 같은 쪽 {same}/{len(want)}")
    assert len(auto) >= 0.85 * len(rows) and wrong <= 0.02 * len(auto)
    assert same >= 0.95 * len(want)


# ── tasks/0005 단계 1 ─────────────────────────────────────────────────────────
def test_extra_digits_go_into_training_and_an_orphan_staging_folder_does_not_block_the_name(tmp_path):
    """끊긴 학습의 임시 폴더가 있어도 같은 이름으로 다시 학습이 된다 (그 폴더는 치운다). --extra-digits 의 숫자 칸 수가 카드에 적히고,
    후보 목록(classes.json)·읽기 수에는 들어가지 않는다."""
    from test_recognizer_tidy import dead_pid, write_digit_crops

    synth_meta.write_meta_crops(tmp_path / "c", ("vehicle_no",), 6, seed=0)
    n = write_digit_crops(tmp_path / "digits", 60, seed=1)
    models = tmp_path / "models"
    orphan = models / f".veh.tmp-{dead_pid()}"
    orphan.mkdir(parents=True)
    card = train_meta(tmp_path / "c", models / "veh", MetaTrainArgs(name="veh", keys=("vehicle_no",), steps=30, eval_every=30,
                                                                     synthetic=200, extra_digits=str(tmp_path / "digits")))
    assert not orphan.exists() and (models / "veh" / "model.onnx").is_file()
    assert card["data"]["extra_digits"]["cells"] == n and "extra_digits" not in card["train_args"]
    plain = train_meta(tmp_path / "c", models / "veh2", MetaTrainArgs(name="veh2", keys=("vehicle_no",), steps=30, eval_every=30,
                                                                       synthetic=200))
    assert "extra_digits" not in plain["data"]
    assert card["validation"]["reads"] == plain["validation"]["reads"]          # 읽기(기준의 근거)에는 들어가지 않는다
    classes = json.loads((models / "veh" / "classes.json").read_text(encoding="utf-8"))["values"]["vehicle_no"]
    assert set(classes) == set(synth_meta.VEHICLES)                              # 후보 목록에도 (운반 횟수는 차량번호가 아니다)

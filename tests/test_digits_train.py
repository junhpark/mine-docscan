"""숫자 인식기 학습 (tasks/0003 단계 4) — torch 가 있어야 돈다: `pytest -m train`.

기본 `pytest` 에서는 빠진다 (pyproject 의 addopts). 수치는 합성 셀에 대한 문턱이다 — 손글씨 인식률이 아니다.
"""
import json
import os
import time

import numpy as np
import pytest

from minedocscan.cli import main
from minedocscan.imaging.cropspec import CropSpec
from minedocscan.recognize import load_answers_json
from minedocscan.recognize.digits import calib
from minedocscan.recognize.digits.model import CARD_KEYS, OnnxNet, normalize, read_answers
from minedocscan.recognize.digits.train import (
    DEFAULT_GEOMETRY,
    DEFAULT_SPEC,
    TrainArgs,
    TrainError,
    build_net,
    export_onnx,
    synth_pool,
    train,
)

pytestmark = pytest.mark.train
torch = pytest.importorskip("torch")
pytest.importorskip("onnx")


def test_opencv_reads_the_exported_model_like_torch(tmp_path):
    """같은 입력에 대해 torch 출력과 OpenCV(cv2.dnn) 출력의 차이가 1e-3 미만이고 읽은 답이 전부 같다."""
    torch.manual_seed(0)
    net = build_net(torch)
    with torch.no_grad():                                   # 배치 정규화 통계를 흔들어 둔다 (기본값이면 너무 쉽다)
        for m in net.modules():
            if isinstance(m, torch.nn.BatchNorm2d):
                m.running_mean.uniform_(-0.5, 0.5)
                m.running_var.uniform_(0.5, 2.0)
    net.eval()
    exporter = export_onnx(torch, net, tmp_path / "model.onnx")
    assert "opset" in exporter
    small, _texts, _kinds = synth_pool(64, 5, DEFAULT_GEOMETRY, DEFAULT_SPEC, workers=1)
    xs = np.stack([normalize(x) for x in small])
    cv_out = OnnxNet(tmp_path / "model.onnx").logits_many(xs)
    with torch.no_grad():
        th_out = net(torch.from_numpy(xs)).squeeze(2).numpy()
    assert cv_out.shape == th_out.shape
    assert float(np.abs(cv_out - th_out).max()) < 1e-3
    assert [read_answers(a)[0][0] for a in cv_out] == [read_answers(b)[0][0] for b in th_out]


def test_synthetic_only_model_meets_the_thresholds(tmp_path):
    """합성 셀만으로 CPU 에서 2분 안에 학습한 모델이, 따로 뽑은 합성 셀 2,000개에서 값 있는 칸 ≥ 0.97, 빈 칸 ≥ 0.97."""
    held_x, held_y, _kinds = synth_pool(2000, 424_242, DEFAULT_GEOMETRY, DEFAULT_SPEC)   # 학습에 쓰지 않은 씨앗
    t0 = time.time()
    card = train(None, tmp_path / "m", TrainArgs(name="m", seed=0))
    elapsed = time.time() - t0
    v = card["validation"]
    print(f"학습 {v['train_seconds']}s, 전체 {elapsed:.0f}s, CPU {os.cpu_count()}개, 파라미터 {card['architecture']['params']}, "
          f"ONNX {card['architecture']['onnx_bytes']} B")
    # "CPU 에서 2분 안에": CPU 4개 이상인 개발 환경에서만 잰다. CI(공유 러너)는 기계마다 속도가 달라 정확도만 본다
    if (os.cpu_count() or 1) >= 4 and not os.environ.get("CI"):
        assert v["train_seconds"] < 120
    assert v["export_check"]["max_abs_diff"] < 1e-3 and v["export_check"]["same_answers"]
    net = OnnxNet(tmp_path / "m" / "model.onnx")
    preds = calib.predict(list(net.logits_many(np.stack([normalize(x) for x in held_x]))), card["temperature"])
    s = calib.score([(a, c) for a, c, _ in preds], held_y)
    print("합성 셀 2,000개:", json.dumps(s))
    assert s["value"]["n"] > 900 and s["empty"]["n"] > 700
    assert s["value"]["accuracy"] >= 0.97 and s["empty"]["accuracy"] >= 0.97
    assert (tmp_path / "m" / "model.onnx").stat().st_size < 1_000_000


@pytest.fixture(scope="module")
def crops(tmp_path_factory):
    """낮은 칸 합성 양식 3일치를 정답대로 검수하고 숫자 칸을 train / test 로 내보낸 폴더."""
    from conftest import review_everything
    from minedocscan.config import Settings
    from minedocscan.pipeline import Pipeline
    from minedocscan.review.export import export_crops
    from minedocscan.tools.synth import generate

    root = tmp_path_factory.mktemp("digits_crops")
    synth = generate(root / "data", days=3, seed=1, low_cells=True)
    settings = Settings(site=synth.site, archive_root=synth.scans, work_root=root / "work", reviews=root / "r.jsonl")
    pipe = Pipeline(settings)
    pipe.run([synth.scans])
    review_everything(pipe.con, pipe.site, settings, load_answers_json(synth.answers_path))
    out_train = export_crops(pipe.con, pipe.site, settings, root / "crops-train", split="train", kind="handwritten_number")
    out_all = export_crops(pipe.con, pipe.site, settings, root / "crops-all", kind="handwritten_number")
    return {"root": root, "site": pipe.site, "train": out_train, "all": out_all, "settings": settings}


def test_train_from_crops_writes_a_card_without_images_or_reviewers(crops, tmp_path):
    assert crops["train"]["written"] > 0
    site = crops["site"]
    card = train(crops["root"] / "crops-train", tmp_path / "real", TrainArgs(name="real", steps=120, synthetic=400,
                                                                            val_share=0.5, eval_every=60),
                 split_salt=site.split_salt, trips_max=30)
    d = tmp_path / "real"
    assert {p.name for p in d.iterdir()} == {"model.onnx", "card.json", "train-log.jsonl"}
    assert all(k in card for k in CARD_KEYS)
    assert CropSpec.from_dict(card["spec"]) == CropSpec("source", 1.5, None)         # export-crops 의 기본 규격
    dt = card["data"]
    assert dt["train"]["cells"] + dt["val"]["cells"] == crops["train"]["written"]
    # 검증(온도·기준·성적)은 잉크가 있던 칸만 — 잉크가 없던 빈 칸은 학습에만 쓴다
    if card["validation"]["source"] == "real":
        assert card["validation"]["cells"] + card["validation"]["excluded_no_ink"] == dt["val"]["cells"]
        assert card["validation"]["excluded_no_ink"] > 0
    dates = {json.loads(x)["work_date"] for x in (crops["root"] / "crops-train" / "train" / "labels.jsonl").read_text().splitlines()}
    assert dt["train"]["dates"] + dt["val"]["dates"] == len(dates) and dt["synthetic"]["cells"] == 400
    assert card["validation"]["source"] == ("real" if dt["val"]["cells"] else "synthetic")
    text = (d / "card.json").read_text(encoding="utf-8") + (d / "train-log.jsonl").read_text(encoding="utf-8")
    for forbidden in ("reviewer", '"jp"', "field_id", ":haul:", ":matrix:", "png", "base64"):
        assert forbidden not in text, forbidden
    log = [json.loads(x) for x in (d / "train-log.jsonl").read_text().splitlines()]
    assert [r["step"] for r in log] == [60, 120] and set(log[0]) == {"step", "loss", "lr", "val_acc", "val_loss", "seconds"}
    # 같은 이름이면 멈춘다
    with pytest.raises(TrainError, match="덮어쓰지"):
        train(crops["root"] / "crops-train", d, TrainArgs(name="real", steps=10, synthetic=50))


def test_no_real_validation_means_no_auto_threshold(crops, tmp_path, monkeypatch):
    """현장 셀로 학습했는데 검증 날짜의 실제 셀이 없으면 합성 셀로 기준을 정하지 않는다 (자동 적재 없음, 이유를 적는다)."""
    card = train(crops["root"] / "crops-train", tmp_path / "noval", TrainArgs(name="noval", steps=20, synthetic=100,
                                                                             val_share=0.0, eval_every=20))
    aa = card["auto_accept"]
    assert card["validation"]["source"] == "synthetic" and aa["met"] is False and aa["threshold"] is None
    assert "실제 검증" in aa["reason"]
    from minedocscan.recognize.digits.backend import DigitsRecognizer

    assert DigitsRecognizer(tmp_path / "noval").threshold == float("inf")       # 백엔드는 자동 적재하지 않는다
    # 내보내기를 못 하는 torch 면 학습 전에 멈춘다
    import minedocscan.recognize.digits.train as tr

    def broken(*a, **k):
        raise RuntimeError("no exporter")

    monkeypatch.setattr(tr, "export_onnx", broken)
    with pytest.raises(TrainError, match="ONNX"):
        train(None, tmp_path / "x", TrainArgs(name="x", steps=5, synthetic=20))
    assert not (tmp_path / "x").exists()


def test_train_command_refuses_test_lines(crops, tmp_path):
    if "test" not in crops["all"]["by_split"]:
        pytest.skip("이 씨앗의 3일 중 test 날짜가 없다")
    with pytest.raises(SystemExit, match="test"):
        main(["recognizer", "train", "--crops", str(crops["root"] / "crops-all"), "--name", "x", "--out", str(tmp_path / "x")])


def test_regenerated_fixture_model_meets_stage5(tmp_path):
    """시험용 모델을 README 의 명령으로 다시 만들어도 단계 5 의 문턱을 넘는다 (바이트가 같을 필요는 없다)."""
    from pathlib import Path

    from conftest import digits_metrics, digits_settings, number_cells
    from minedocscan.pipeline import Pipeline
    from minedocscan.tools.synth import generate

    args = ["--name", "digits-fixture", "--synthetic-geometry", "112x22,92x21", "--target-auto-error", "0.005"]
    readme = (Path(__file__).parent / "fixtures" / "README.md").read_text(encoding="utf-8")
    assert " ".join(args) in readme.replace("\\\n    ", "")                 # README 의 명령과 같은 인자
    out = tmp_path / "digits-fixture"
    assert main(["recognizer", "train", *args, "--out", str(out)]) == 0
    synth = generate(tmp_path / "data", days=3, seed=0, low_cells=True)
    pipe = Pipeline(digits_settings(synth, tmp_path / "work", model_dir=out, reviews=tmp_path / "r.jsonl"))
    pipe.run([synth.scans])
    m = digits_metrics(number_cells(pipe.con, load_answers_json(synth.answers_path)))
    print("다시 만든 시험용 모델, 합성 3일치:", json.dumps(m))
    assert m["value_correct"] / m["values"] >= 0.95
    assert m["auto"] > 100 and m["auto_wrong"] / m["auto"] <= 0.01
    assert m["inked_empty_auto"] > 0

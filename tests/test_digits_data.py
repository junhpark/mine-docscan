"""숫자 인식기: torch 없이 도는 부분 (tasks/0003 단계 4) — 크롭 폴더 읽기·거절, 검증 날짜, 답 읽기, 보정 계산, 학습 명령의 안내."""
import importlib.util
import itertools
import json
import math
from pathlib import Path

import cv2
import numpy as np
import pytest

from minedocscan.cli import main
from minedocscan.imaging.cropspec import CropSpec
from minedocscan.recognize.digits import calib, data
from minedocscan.recognize.digits.model import (
    CHARS,
    INPUT_H,
    INPUT_W,
    beam_search,
    log_softmax,
    normalize_answer,
    preprocess,
    read_answers,
)
from minedocscan.recognize.digits.train import TrainArgs, TrainError, train
from minedocscan.review.export import export_crops
from minedocscan.review.store import Review, save

REPO = Path(__file__).resolve().parents[1]
SPEC = {"res": "source", "scale": 1.5, "pad": "auto"}


def write_crops(root: Path, lines: list[dict], split: str = "train") -> Path:
    """labels.jsonl + 작은 PNG (내용은 상관없다)."""
    (root / split / "handwritten_number").mkdir(parents=True, exist_ok=True)
    with open(root / split / "labels.jsonl", "w", encoding="utf-8") as f:
        for i, ln in enumerate(lines):
            rel = f"{split}/handwritten_number/c{i}.png"
            cv2.imwrite(str(root / rel), np.full((40, 120), 230, np.uint8))
            f.write(json.dumps({"field_id": f"doc-p1:haul:day:{i}", "file": rel, "text": "", "verdict": "value",
                                "kind": "handwritten_number", "work_date": "2030-01-07", "split": split, "spec": SPEC,
                                "bbox": [0, 0, 92, 21], "reviewer": "jp"} | ln) + "\n")
    return root


def test_read_crops_maps_verdicts_and_skips_what_it_cannot_use(tmp_path):
    root = write_crops(tmp_path / "c", [{"text": "07"}, {"text": "12"}, {"verdict": "empty"}, {"verdict": "illegible"},
                                        {"text": "A3"}, {"kind": "handwritten_text", "text": "oil"}])
    c = data.read_crops(root)
    assert [s.text for s in c.samples] == ["7", "12", "", "?"]            # 앞의 0 을 뗀다, empty → "", illegible → "?"
    assert c.skipped == {"not_a_number": 1, "other_kind": 1} and c.lines == 6
    assert c.spec == CropSpec("source", 1.5, None)
    # 내보낸 폴더를 줘도, 그 안의 분할 폴더를 줘도 같다
    assert [s.path for s in data.read_crops(root / "train").samples] == [s.path for s in c.samples]


def test_read_crops_refuses_test_lines_and_mixed_specs(tmp_path):
    root = write_crops(tmp_path / "t", [{"text": "3"}])
    write_crops(root, [{"text": "4", "split": "test"}], split="test")
    with pytest.raises(data.CropsError, match="test"):
        data.read_crops(root)
    assert [s.text for s in data.read_crops(root, allow_test=True, only_split="test").samples] == ["4"]   # 평가는 된다
    mixed = write_crops(tmp_path / "m", [{"text": "3"}, {"text": "5", "spec": {"res": "aligned", "scale": 1, "pad": 0}}])
    with pytest.raises(data.CropsError, match="규격"):
        data.read_crops(mixed)
    nospec = write_crops(tmp_path / "n", [{"text": "3", "spec": None}])
    lines = [json.loads(x) for x in (nospec / "train" / "labels.jsonl").read_text().splitlines()]
    (nospec / "train" / "labels.jsonl").write_text("\n".join(json.dumps({k: v for k, v in ln.items() if k != "spec"})
                                                             for ln in lines))
    with pytest.raises(data.CropsError, match="spec"):
        data.read_crops(nospec)
    # 학습 명령도 같은 이유로 거절한다 (torch 를 부르기 전에)
    with pytest.raises(SystemExit, match="test"):
        main(["recognizer", "train", "--crops", str(root), "--name", "x", "--out", str(tmp_path / "out" / "x")])


def test_val_dates_are_whole_dates_and_disjoint_from_train():
    days = [f"2030-{m:02d}-{d:02d}" for m in range(1, 13) for d in range(1, 29)]
    samples = [data.Sample(Path(f"{i}.png"), "3", "value", day, None) for i, day in enumerate(days * 2)]
    tr, va = data.split_val(samples, data.val_salt("synthetic-2030"), 0.2)
    tr_d, va_d = {s.work_date for s in tr}, {s.work_date for s in va}
    assert tr_d and va_d and not tr_d & va_d and tr_d | va_d == set(days)
    assert 0.14 < len(va_d) / len(days) < 0.26
    assert data.split_val(samples, data.val_salt("synthetic-2030"), 0.2)[1] == va             # 다시 해도 같다
    # test 분할(같은 소금값)과는 다른 규칙이다 — 검증 날짜가 test 날짜와 같은 집합이 아니다
    from minedocscan.evaluate.split import split_of

    assert va_d != {d for d in days if split_of(d, "synthetic-2030", 0.2) == "test"}
    assert data.is_val_date(None, "s", 0.5) is False


def test_beam_search_sums_paths_like_brute_force():
    rng = np.random.default_rng(1)
    C, T = 5, 6
    z = rng.normal(size=(C, T)) * 2
    lp = log_softmax(z)
    exact: dict[str, float] = {}
    for path in itertools.product(range(C), repeat=T):
        s, prev = [], 0
        for c in path:
            if c != prev and c != 0:
                s.append(CHARS[c - 1])
            prev = c
        k = "".join(s)
        exact[k] = exact.get(k, 0.0) + math.exp(sum(lp[t, c] for t, c in enumerate(path)))
    got = dict(beam_search(lp, beam=500, prune=-1e9))
    for k, p in sorted(exact.items(), key=lambda kv: -kv[1])[:8]:
        assert math.isclose(math.exp(got[k]), p, rel_tol=1e-9), k
    assert math.isclose(sum(exact.values()), 1.0, rel_tol=1e-9)


def test_answers_are_digit_strings_empty_or_reject():
    assert normalize_answer("007") == "7" and normalize_answer("0") == "0" and normalize_answer("") == ""
    assert normalize_answer("1?") == "?"
    # "07" 과 "7" 은 같은 답으로 합친다
    T = 6
    z = np.full((len(CHARS) + 1, T), -20.0)
    z[0, :] = 0.0                                     # blank
    z[1, 1] = 2.0                                     # '0' 이 t=1 에 조금
    z[8, 3] = 5.0                                     # '7' 이 t=3 에 크게
    ans = read_answers(z)
    assert ans[0][0] == "7" and [a for a, _ in ans].count("7") == 1 and sum(p for _a, p in ans) <= 1.0 + 1e-9
    assert preprocess(np.full((61, 168), 200, np.uint8)).shape == (1, 3, INPUT_H, INPUT_W)


def test_ctc_nll_matches_brute_force():
    rng = np.random.default_rng(3)
    C, T = 4, 5
    lp = log_softmax(rng.normal(size=(C, T)))
    for target in ([], [1], [2, 2], [1, 3]):
        want = sum(math.exp(sum(lp[t, c] for t, c in enumerate(path))) for path in itertools.product(range(C), repeat=T)
                   if _collapse(path) == target)
        assert math.isclose(calib.ctc_nll(lp, target), -math.log(want), rel_tol=1e-9)


def _collapse(path):
    out, prev = [], 0
    for c in path:
        if c != prev and c != 0:
            out.append(c)
        prev = c
    return out


def test_threshold_table_by_hand():
    # 정답과 (답, 신뢰도): 손으로 셀 수 있는 예
    preds = [("3", 0.99), ("4", 0.97), ("", 0.95), ("5", 0.92), ("?", 0.99), ("12", 0.6), ("", 0.4)]
    truth = ["3", "9", "", "5", "7", "12", "8"]
    rows = {r["threshold"]: r for r in calib.threshold_table(preds, truth, grid=(0.5, 0.9, 0.96, 0.98))}
    # t=0.9: 자동 = 3(맞음) 4(틀림) ""(맞음) 5(맞음) → 4개 중 1개 틀림. "?" 는 신뢰도와 상관없이 자동이 아니다
    assert (rows[0.9]["auto"], rows[0.9]["errors"], rows[0.9]["auto_rate"]) == (4, 1, round(4 / 7, 4))
    assert rows[0.9]["auto_value"] == 3 and rows[0.9]["auto_empty"] == 1
    assert (rows[0.5]["auto"], rows[0.5]["errors"]) == (5, 1) and (rows[0.98]["auto"], rows[0.98]["errors"]) == (1, 0)
    assert calib.choose_threshold(list(rows.values()), 0.01)["threshold"] == 0.98
    assert calib.choose_threshold(list(rows.values()), 0.25)["threshold"] == 0.5
    assert calib.choose_threshold([r | {"errors": r["auto"]} for r in rows.values()], 0.01) is None
    # 범위 밖(trips_max)은 자동 적재 대상이 아니다
    assert calib.threshold_table(preds, truth, trips_max=10, grid=(0.5,))[0]["auto"] == 4
    lo, hi = calib.wilson(1, 4)
    assert (round(lo, 4), round(hi, 4)) == (0.0456, 0.6994)
    assert calib.wilson(0, 0) == (0.0, 1.0)
    s = calib.score(preds, truth)
    assert (s["all"]["n"], s["all"]["correct"]) == (7, 4)
    assert (s["value"]["n"], s["value"]["correct"], s["empty"]["n"], s["empty"]["correct"]) == (6, 3, 1, 1)


def test_train_without_torch_says_what_to_install(monkeypatch, tmp_path):
    real = importlib.util.find_spec
    monkeypatch.setattr(importlib.util, "find_spec", lambda name, *a: None if name == "torch" else real(name, *a))
    with pytest.raises(TrainError, match=r'pip install -e "\.\[train\]"'):
        train(None, tmp_path / "m", TrainArgs(name="m"))
    with pytest.raises(SystemExit, match=r"\.\[train\]"):                 # 명령줄: 트레이스백이 아니라 안내
        main(["recognizer", "train", "--name", "m", "--out", str(tmp_path / "m2")])


def test_train_refuses_targets_in_the_repo_and_bad_names(tmp_path):
    with pytest.raises(TrainError, match="git 작업 트리"):
        train(None, REPO / "models-should-not-be-here" / "m", TrainArgs(name="m"))
    assert not (REPO / "models-should-not-be-here").exists()
    with pytest.raises(SystemExit, match="--name"):
        main(["recognizer", "train", "--name", "../x", "--out", str(tmp_path / "x")])
    with pytest.raises(SystemExit, match="사이트 팩"):
        main(["recognizer", "train", "--name", "x"])                       # 사이트 팩도 --out 도 없다
    (tmp_path / "taken").mkdir()
    (tmp_path / "taken" / "card.json").write_text("{}")
    with pytest.raises(TrainError, match="덮어쓰지"):
        train(None, tmp_path / "taken", TrainArgs(name="taken"))


def test_export_crops_can_include_illegible(reviewed_day, tmp_path):
    import sqlite3
    from dataclasses import replace

    rsite, settings = reviewed_day["null"].site, reviewed_day["settings"]
    con = sqlite3.connect(":memory:")                     # 세션 픽스처의 DB 를 더럽히지 않게 복사본에서
    reviewed_day["null"].con.backup(con)
    con.row_factory = sqlite3.Row
    settings = replace(settings, reviews=tmp_path / "r.jsonl")
    fid = con.execute("SELECT field_id FROM doc_field WHERE kind = 'handwritten_number' LIMIT 1").fetchone()[0]
    save(con, rsite, settings, Review(fid, "illegible", reviewer="jp"))
    plain = export_crops(con, rsite, settings, tmp_path / "a", kind="handwritten_number")
    withill = export_crops(con, rsite, settings, tmp_path / "b", kind="handwritten_number", include_illegible=True)
    assert withill["written"] == plain["written"] + 1 and plain["skipped_illegible"] >= 1
    lines = [json.loads(x) for p in (tmp_path / "b").rglob("labels.jsonl") for x in p.read_text().splitlines()]
    ill = [x for x in lines if x["field_id"] == fid]
    assert len(ill) == 1 and ill[0]["verdict"] == "illegible" and ill[0]["text"] == ""
    assert [s.text for s in data.read_crops(tmp_path / "b", allow_test=True).samples if s.field_id == fid] == ["?"]

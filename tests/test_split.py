"""평가셋 분할(날짜 단위, 소금값으로 고정)과 내보내기 (tasks/0002 단계 6)."""
import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from minedocscan.cli import main
from minedocscan.evaluate.fields import evaluate_fields
from minedocscan.evaluate.split import split_of
from minedocscan.recognize import load_answers_json
from minedocscan.review.export import ExportError, export_crops, inside_git_tree
from minedocscan.review.store import Review, effective, export_answers, save, stats

REPO = Path(__file__).resolve().parents[1]


def test_split_is_deterministic_and_roughly_the_requested_share():
    days = [(date(2030, 1, 1) + timedelta(days=i)).isoformat() for i in range(1000)]
    a = [split_of(d, "salt-a", 0.2) for d in days]
    assert a == [split_of(d, "salt-a", 0.2) for d in days]
    share = a.count("test") / len(a)
    assert 0.15 < share < 0.25
    assert [split_of(d, "salt-b", 0.2) for d in days] != a            # 소금값이 다르면 다른 평가셋
    assert split_of(None, "salt-a") == "unknown" and split_of("", "salt-a") == "unknown"
    assert all(split_of(d, "x", 0.0) == "train" for d in days[:50]) and all(split_of(d, "x", 1.0) == "test" for d in days[:50])


def test_all_cells_of_a_date_share_a_split_and_splits_partition(reviewed_day, null_run, site):
    con, rsite, root = reviewed_day["null"].con, reviewed_day["null"].site, reviewed_day["root"]
    day = reviewed_day["synth"].truth["days"][0]["date"]
    sp = rsite.split_of(day)
    assert sp in ("test", "train") and rsite.split_salt == "synthetic-2030" and rsite.test_share == 0.2
    n_all = export_answers(con, root / "all.json", "all", rsite)
    n_test = export_answers(con, root / "test.json", "test", rsite)
    n_train = export_answers(con, root / "train.json", "train", rsite)
    assert n_all == n_test + n_train == reviewed_day["n_reviewed"]
    assert (n_test if sp == "test" else n_train) == n_all                 # 한 날짜의 셀은 전부 같은 분할
    keys_t, keys_r = set(load_answers_json(root / "test.json")), set(load_answers_json(root / "train.json"))
    assert not (keys_t & keys_r) and (keys_t | keys_r) == set(load_answers_json(root / "all.json"))
    # 검수를 더 넣어도 이미 있던 셀의 분할은 바뀌지 않는다 (날짜와 소금값만으로 정해진다)
    assert {rsite.split_of(r["work_date"]) for r in null_run.con.execute("SELECT DISTINCT work_date FROM doc_page")} \
        == {site.split_of(d["date"]) for d in null_run.site and __import__("json").loads(
            (null_run.settings.archive_root.parent / "truth.json").read_text(encoding="utf-8"))["days"]}
    st = stats(con, rsite)
    assert st["by_split"] == {sp: {"fields": n_all, "dates": 1}}
    # eval --split: 그 분할의 셀만 센다
    ans = load_answers_json(root / "all.json")
    assert evaluate_fields(con, ans, target="raw", only_listed=True, split=sp, site=rsite)["n"] == n_all
    other = "train" if sp == "test" else "test"
    assert evaluate_fields(con, ans, target="raw", only_listed=True, split=other, site=rsite)["n"] == 0
    with pytest.raises(ValueError):
        evaluate_fields(con, ans, split="test")


def test_export_crops_writes_labels_and_pngs_outside_the_repo(reviewed_day, tmp_path):
    con, rsite, settings = reviewed_day["null"].con, reviewed_day["null"].site, reviewed_day["settings"]
    day = reviewed_day["synth"].truth["days"][0]["date"]
    sp = rsite.split_of(day)
    # 읽을 수 없음 하나를 더해 둔다 — 내보내기에서 빠져야 한다
    fid = con.execute("SELECT field_id FROM doc_field WHERE kind='handwritten_number' AND has_value=1 LIMIT 1").fetchone()[0]
    before = effective(con, field_ids=[fid])[fid]
    save(con, rsite, settings, Review(fid, "illegible", reviewer="jp"))
    n_valid = sum(1 for _ in load_answers_json(reviewed_day["exported"])) - 1
    out = tmp_path / "crops"
    try:
        r = export_crops(con, rsite, settings, out, split=sp)
    finally:                                                               # 공유 픽스처: 유효한 값을 원래대로 되돌린다
        save(con, rsite, settings, Review(fid, before.verdict, before.value, before.reviewer, reviewed_at="2031-01-01T00:00:00Z"))
    labels = [json.loads(line) for line in (out / sp / "labels.jsonl").read_text(encoding="utf-8").splitlines()]
    pngs = list((out / sp).rglob("*.png"))
    assert r["written"] == len(labels) == len(pngs) == n_valid and r["skipped_illegible"] == 1
    assert r["by_split"] == {sp: n_valid} and set(r["by_source"]) == {"source"}
    assert all(lab["split"] == sp and lab["resolution"] == "source" and lab["out_scale"] == 1.5 for lab in labels)
    assert all(lab["pad"] >= 8 and len(lab["bbox"]) == 4 for lab in labels)          # 여유는 화면과 같이 행 높이의 절반
    import cv2
    lab = next(lab for lab in labels if lab["kind"] == "handwritten_number")
    img = cv2.imread(str(out / lab["file"]), cv2.IMREAD_GRAYSCALE)
    x0, y0, x1, y1 = lab["bbox"]
    assert img.shape == (round((y1 - y0 + 2 * lab["pad"]) * 1.5), round((x1 - x0 + 2 * lab["pad"]) * 1.5))
    assert {lab["verdict"] for lab in labels} == {"value", "empty"} and all((out / lab["file"]).exists() for lab in labels)
    assert all(fid != lab["field_id"] for lab in labels)
    other = "train" if sp == "test" else "test"
    assert export_crops(con, rsite, settings, tmp_path / "none", split=other)["written"] == 0
    numbers = export_crops(con, rsite, settings, tmp_path / "num", kind="handwritten_number", res="aligned", out_scale=1.0, pad=0)
    assert 0 < numbers["written"] < n_valid and set(numbers["by_source"]) == {"aligned"}
    lab0 = json.loads((tmp_path / "num" / sp / "labels.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert lab0["pad"] == 0
    # 대상이 git 작업 트리 안이면 거절한다
    assert inside_git_tree(REPO / "out" / "crops") and not inside_git_tree(tmp_path)
    with pytest.raises(ExportError, match="git"):
        export_crops(con, rsite, settings, REPO / "out" / "crops")
    assert not (REPO / "out" / "crops").exists()


def test_cli_split_commands(reviewed_day, tmp_path, capsys, monkeypatch):
    synth, settings = reviewed_day["synth"], reviewed_day["settings"]
    sp = reviewed_day["null"].site.split_of(synth.truth["days"][0]["date"])
    common = ["--site", str(synth.site), "--archive-root", str(synth.scans), "--work-root", str(settings.work_root),
              "--db-url", settings.resolved_db_url]
    monkeypatch.setenv("MINEDOCSCAN_REVIEWS", str(settings.reviews))
    assert main(["review", "stats", "--json"] + common) == 0
    assert sp in json.loads(capsys.readouterr().out)["stats"]["by_split"]
    assert main(["review", "export-answers", str(tmp_path / "t.json"), "--split", sp] + common) == 0
    assert f"--split {sp}" in capsys.readouterr().out
    assert main(["eval", "--answers", str(tmp_path / "t.json"), "--target", "raw", "--only-listed", "--split", sp, "--json"]
                + common) == 0
    assert json.loads(capsys.readouterr().out)["fields"]["split"] == sp
    assert main(["review", "export-crops", str(tmp_path / "c"), "--split", sp, "--kind", "handwritten_number"] + common) == 0
    assert "저장소에 넣지 마세요" in capsys.readouterr().out and (tmp_path / "c" / sp / "labels.jsonl").exists()
    with pytest.raises(SystemExit, match="git"):
        main(["review", "export-crops", str(REPO / "out" / "crops2")] + common)
    # 범위를 벗어난 --scale·--pad 는 아무것도 쓰지 않고 실패한다
    for bad in (["--scale", "0"], ["--scale", "-1"], ["--scale", "7"], ["--pad", "-1"]):
        target = tmp_path / ("bad" + "_".join(bad))
        with pytest.raises(SystemExit):
            main(["review", "export-crops", str(target)] + bad + common)
        assert not list(target.rglob("*.png")) if target.exists() else True
    # 파일 이름에 Windows 가 받지 않는 글자가 없다. 줄 수 = PNG 수
    labels = [json.loads(x) for x in (tmp_path / "c" / sp / "labels.jsonl").read_text(encoding="utf-8").splitlines()]
    pngs = list((tmp_path / "c" / sp).rglob("*.png"))
    assert len(labels) == len(pngs) > 0
    assert all(not set('<>:"\\|?*') & set(p.name) for p in pngs)
    assert {lab["file"].split("/")[-1] for lab in labels} == {p.name for p in pngs}

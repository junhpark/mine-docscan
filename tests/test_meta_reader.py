"""메타 필드 모델 읽기 (tasks/0004 단계 3) — torch 없이, 시험용 모델(tests/fixtures/meta-digits)로."""
import json
from dataclasses import replace
from pathlib import Path

import pytest

from conftest import clone_db
from minedocscan.cli import main
from minedocscan.config import Settings
from minedocscan.forms.sitepack import SitePack
from minedocscan.recognize.digits.backend import DigitsRecognizer
from minedocscan.recognize.meta.model import (
    FIXED_VALUES,
    MetaModel,
    MetaModelError,
    build_meta_readers,
    template_values,
)
from minedocscan.review.store import Review, field_id_of, save
from minedocscan.tools import synth_meta
from minedocscan.tools.synth import SLOTS_META, T_LOG

META_DIGITS = Path(__file__).resolve().parent / "fixtures" / "meta-digits"


def test_fixture_card_and_candidates(meta_synth):
    m = MetaModel(META_DIGITS)
    site = SitePack(meta_synth.site)
    assert m.reader == "digits" and m.keys == ["vehicle_no", "date.month", "date.day"] and not m.position
    assert m.threshold < 1 and m.card["auto_accept"]["upper95"] is not None
    # 후보 = 학습 때 본 값 + 템플릿의 header_vehicle_no (숫자만). 날짜의 부분은 고정 범위
    printed = [v for _s, v, _o in SLOTS_META]
    assert template_values(site, "vehicle_no") == printed
    cands = m.candidates("vehicle_no", site)
    assert set(cands) == set(synth_meta.VEHICLES) | set(printed)
    assert m.candidates("date.day", site) == FIXED_VALUES["date.day"] and len(m.candidates("date.month")) == 12
    # 카드에는 값이 없다
    text = (META_DIGITS / "card.json").read_text(encoding="utf-8")
    assert not any(v in text for v in synth_meta.VEHICLES)


def test_candidates_do_not_change_when_reviews_are_saved(meta_synth, meta_null, tmp_path):
    """기계의 후보 목록은 모델 폴더와 템플릿에서만 온다 — 새 값을 검수해도 바뀌지 않는다 (4.2)."""
    m = MetaModel(META_DIGITS)
    site = meta_null["pipe"].site
    before = m.candidates("vehicle_no", site)
    con = clone_db(meta_null["pipe"].con)
    pid = con.execute("SELECT page_id FROM doc_page WHERE template_name = ?", (T_LOG,)).fetchone()[0]
    save(con, site, replace(meta_null["settings"], reviews=tmp_path / "r.jsonl"),
         Review(field_id_of(pid, "vehicle_no"), "value", "9999", "jp"))
    assert m.candidates("vehicle_no", site) == before and "9999" not in before


def test_settings_pick_a_model_per_key(meta_synth, tmp_path):
    site = SitePack(meta_synth.site)
    s = Settings(site=meta_synth.site, recognizer_options={"meta": {"vehicle_no": str(META_DIGITS),
                                                                   "date": {"day": str(META_DIGITS)}}})
    readers = build_meta_readers(s, site)
    assert set(readers) == {"vehicle_no", "date.day"} and readers["vehicle_no"] is readers["date.day"]   # 한 번만 읽는다
    with pytest.raises(MetaModelError, match="operator"):                 # 다른 키에 꽂으면 시작할 때 오류
        build_meta_readers(Settings(site=meta_synth.site, recognizer_options={"meta": {"operator": str(META_DIGITS)}}), site)
    with pytest.raises(MetaModelError, match="nope"):
        build_meta_readers(Settings(site=meta_synth.site, recognizer_options={"meta": {"vehicle_no": "nope"}}), site)
    assert build_meta_readers(Settings(site=meta_synth.site), site) == {}
    # 숫자 칸 백엔드에 꽂으면 거절, 메타 필드 모델이 아닌 것을 메타에 꽂아도 거절
    with pytest.raises(ValueError, match="recognize.meta"):
        DigitsRecognizer(META_DIGITS)
    with pytest.raises(MetaModelError, match="메타 필드 모델이 아닙니다"):
        MetaModel(Path(__file__).resolve().parent / "fixtures" / "digits-fixture")


def test_recognizer_eval_and_list_for_meta_models(tmp_path, capsys):
    """크롭 단위 평가: 0003 과 같은 출력 + 목록에 없는 값. 값(차량번호)은 찍지 않는다."""
    new = {w: synth_meta.NEW_VEHICLES[i % 3] for i, w in enumerate(synth_meta.ROSTER)}
    synth_meta.write_meta_crops(tmp_path / "c", ("vehicle_no",), 3, seed=5, start="2031-02-01", split="test")
    synth_meta.write_meta_crops(tmp_path / "c", ("vehicle_no",), 2, seed=6, start="2031-03-01", vehicles=new, split="test")
    capsys.readouterr()
    assert main(["recognizer", "eval", "--crops", str(tmp_path / "c"), "--model", str(META_DIGITS), "--split", "test",
                 "--json"]) == 0
    out = capsys.readouterr().out
    r = json.loads(out)
    assert r["cells"] == 30 and r["score"]["truth_unlisted"]["n"] == 12 and "predictions" not in r
    assert r["score"]["truth_unlisted"]["answered_unlisted"] >= 11 and r["score"]["listed"]["accuracy"] >= 0.9
    for v in synth_meta.VEHICLES + synth_meta.NEW_VEHICLES:
        assert v not in out
    assert main(["recognizer", "eval", "--crops", str(tmp_path / "c"), "--model", str(META_DIGITS), "--split", "test"]) == 0
    assert "목록에 없는 값으로 답함" in capsys.readouterr().out
    # 검증 날짜 방식으로 만든 모델: --split val 은 그 날짜만 (이 폴더에는 train 줄이 없다)
    with pytest.raises(SystemExit, match="vehicle_no"):
        main(["recognizer", "eval", "--crops", str(tmp_path / "c"), "--model", str(META_DIGITS), "--split", "val"])
    site = tmp_path / "site"
    (site / "models").mkdir(parents=True)
    import shutil

    shutil.copytree(META_DIGITS, site / "models" / "meta-digits")
    assert main(["recognizer", "list", "--site", str(site)]) == 0
    assert "메타 vehicle_no, date.month, date.day (digits)" in capsys.readouterr().out

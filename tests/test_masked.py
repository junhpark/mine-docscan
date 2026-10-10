"""가린 쪽 그림 (tasks/0008 단계 6, 4.9): 템플릿이 아는 자리만 한 색으로. 합성 쪽(synth --display-names --usage-logs
--usage-variants)만 — 가린 그림·정합 그림은 tmp_path 에 (저장소에 넣지 않는다).

묶음 하나(가동 일보 + 판 B + 표시 이름)를 새로 돌리는 시험(masked_world 를 쓰는 것)만 slow 다 — 기본 시험 시간을 main 의 1.15배
안에 두려고 (tasks/0008 6절). 설정 검사([redact] 를 읽는 것과 기본값, 메타 키의 오타, 템플릿 검사, info, 쪽이 없는 날짜, 저장소·
접수 폴더·보관 폴더의 거절)는 템플릿만 쓴 사이트 팩(masked_pack — 스캔·실행 없이 0.5초)과 빈 작업 DB 로 기본 pytest 에서 돈다
(tasks/0009 4.1 자). 동시 판의 redact 이름 검사는 test_variants.py.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest
import yaml

from conftest import FIXTURE_MODEL, META_FIXTURES
from minedocscan.config import Settings
from minedocscan.export.masked import DEFAULT_PAD_PX, FILL, export_masked, format_summary, page_boxes
from minedocscan.forms.sitepack import SitePack
from minedocscan.forms.template import Template
from minedocscan.imaging.io import imread_gray, imwrite
from minedocscan.pipeline import Pipeline
from minedocscan.store.db import open_db, upsert
from minedocscan.tools.synth import generate, write_site_pack

_ENV = ("MINEDOCSCAN_CONFIG", "MINEDOCSCAN_SITE", "MINEDOCSCAN_WORK_ROOT", "MINEDOCSCAN_DB_URL", "MINEDOCSCAN_ARCHIVE_ROOT",
        "MINEDOCSCAN_INBOX", "MINEDOCSCAN_EXCEL_DIR", "MINEDOCSCAN_REVIEWS", "MINEDOCSCAN_PUBLISH_URL")


def quiet_env(monkeypatch) -> None:
    """명령 시험: 바깥의 설정(환경변수·현재 폴더의 minedocscan.toml)이 끼어들지 않게."""
    for k in _ENV:
        monkeypatch.delenv(k, raising=False)


@pytest.fixture(scope="module")
def masked_pack(tmp_path_factory) -> Path:
    """masked_world 와 같은 템플릿의 사이트 팩 (가동 일보 + 판 B + 표시 이름) — 스캔도 실행도 없이. 이 폴더에 쓰지 않는다."""
    return write_site_pack(tmp_path_factory.mktemp("masked_pack") / "site", usage=True, usage_variants=True,
                           display_names=True)


def work_db(st: Settings, unloaded_day: str | None = None) -> sqlite3.Connection:
    """빈 작업 DB (스키마만). unloaded_day: 그 날짜에 적재되지 않은 쪽(unknown_form) 하나를 넣는다 — 내보낼 쪽은 없다."""
    con = open_db(st.resolved_db_url)
    if unloaded_day:
        upsert(con, "doc_document", {"document_id": "d0", "source_path": str(Path(st.archive_root) / "x.pdf"),
                                     "source_name": "x", "status": "processed", "created_at": "2030-01-01T00:00:00Z"})
        upsert(con, "doc_page", {"page_id": "d0-p1", "document_id": "d0", "page_no": 1, "status": "unknown_form",
                                 "work_date": unloaded_day})
        con.commit()
    return con


@pytest.fixture(scope="module")
def masked_world(tmp_path_factory):
    root = tmp_path_factory.mktemp("masked")
    syn = generate(root / "data", days=1, seed=0, display_names=True, usage_logs=True, usage_variants=True)
    st = Settings(site=syn.site, archive_root=syn.scans, work_root=root / "work", reviews=root / "r.jsonl", save_aligned=True)
    pipe = Pipeline(st)
    pipe.run([syn.scans])
    day = pipe.con.execute("SELECT MIN(work_date) FROM doc_page").fetchone()[0]
    return {"pipe": pipe, "st": st, "site": pipe.site, "day": day, "root": root}


def loaded(con) -> list[sqlite3.Row]:
    return con.execute("SELECT page_id, template_name, aligned_image FROM doc_page WHERE status = 'loaded' ORDER BY page_id").fetchall()


def inside(shape, boxes) -> np.ndarray:
    m = np.zeros(shape, bool)
    h, w = shape
    for _k, (x0, y0, x1, y1) in boxes:
        m[max(0, y0):min(h, y1), max(0, x0):min(w, x1)] = True
    return m


@pytest.mark.slow                                  # masked_world (묶음 하나를 새로 돌린다)
def test_inside_is_one_color_and_outside_is_the_aligned_image(masked_world, tmp_path):
    w = masked_world
    con, site, st = w["pipe"].con, w["site"], w["st"]
    out = tmp_path / "가린 그림"
    r = export_masked(con, site, st, out, date=w["day"])
    pages = loaded(con)
    assert r["pages"] == len(pages) and sorted(p.name for p in out.iterdir()) == sorted(f"{p['page_id']}.png" for p in pages)
    assert all(r["boxes"][k] for k in ("signature", "meta", "redact", "text"))
    for p in pages:
        tpl = site.templates[p["template_name"]]
        aligned = imread_gray(st.work_root / p["aligned_image"])
        got = imread_gray(out / f"{p['page_id']}.png")
        boxes = page_boxes(tpl, ("operator", "vehicle_no"), DEFAULT_PAD_PX)
        m = inside(got.shape, boxes)
        assert (got[m] == FILL).all() and (got[~m] == aligned[~m]).all(), p["page_id"]
        kinds = {k for k, _ in boxes}
        assert "redact" in kinds and "text" in kinds
        fields = {f["name"]: f for f in tpl.fields}
        for name in ("operator", "vehicle_no", "signature"):                # 서명·작성자·차량번호 필드는 가려진다
            if name in fields:
                x0, y0, x1, y1 = fields[name]["bbox"]
                assert (got[y0:y1, x0:x1] == FILL).all(), (p["page_id"], name)
        for c in tpl.cells(inset=4):                                       # 수 칸·✓ 칸은 남는다 (다른 상자에 덮이지 않은 화소)
            if c.kind in ("handwritten_number", "checkmark"):
                x0, y0, x1, y1 = c.bbox
                sub = ~m[y0:y1, x0:x1]
                assert (got[y0:y1, x0:x1][sub] == aligned[y0:y1, x0:x1][sub]).all()
    text = format_summary(r, out)
    assert "템플릿이 아는 자리만" in text and "사람이" in text
    names = [r_[0] for r_ in con.execute("SELECT source_name FROM doc_document")]
    assert not any(n in text for n in names) and not any(n in p.name for n in names for p in out.iterdir())


@pytest.mark.slow                                  # masked_world (묶음 하나를 새로 돌린다)
def test_keep_text_and_meta_keys_from_the_site_pack(masked_world, tmp_path):
    w = masked_world
    con, site, st = w["pipe"].con, w["site"], w["st"]
    page = next(p for p in loaded(con) if any(f.get("meta_key") == "equipment" for f in site.templates[p["template_name"]].fields))
    tpl = site.templates[page["template_name"]]
    aligned = imread_gray(st.work_root / page["aligned_image"])
    eq = next(f for f in tpl.fields if f.get("meta_key") == "equipment")
    x0, y0, x1, y1 = eq["bbox"]
    text_cells = [c.bbox for c in tpl.cells(inset=0) if c.kind == "handwritten_text"]

    def run(keep, keys=None) -> np.ndarray:
        old = dict(site.redact)
        site.redact = {"meta_keys": keys, "pad_px": None} if keys is not None else old
        try:
            out = tmp_path / f"o{keep}{bool(keys)}"
            export_masked(con, site, st, out, page_ids=[page["page_id"]], keep_text=keep)
            return imread_gray(out / f"{page['page_id']}.png")
        finally:
            site.redact = old

    full = run(False)
    assert (full[y0:y1, x0:x1] == FILL).all()                                  # 장비명은 글자 필드 — 기본으로 가린다
    kept = run(True)
    assert (kept[y0:y1, x0:x1] == aligned[y0:y1, x0:x1]).all()                 # --keep-text: 장비명 필드가 남는다
    if text_cells:
        a0, b0, a1, b1 = text_cells[0]
        assert (full[b0:b1, a0:a1] == FILL).all()
        others = inside(kept.shape, page_boxes(tpl, ("operator", "vehicle_no"), DEFAULT_PAD_PX, keep_text=True))
        sub = ~others[b0:b1, a0:a1]
        assert (kept[b0:b1, a0:a1][sub] == aligned[b0:b1, a0:a1][sub]).all()   # 글자 칸이 남는다
    with_eq = run(True, ["operator", "vehicle_no", "equipment"])
    assert (with_eq[y0:y1, x0:x1] == FILL).all()                               # [redact] meta_keys 에 더하면 가려진다
    sig = next((f for f in tpl.fields if f["kind"] == "signature"), None)
    if sig is not None:
        sx0, sy0, sx1, sy1 = sig["bbox"]
        assert (kept[sy0:sy1, sx0:sx1] == FILL).all()                          # 서명은 --keep-text 여도 가린다


@pytest.mark.slow                                  # masked_world (묶음 하나를 새로 돌린다)
def test_writing_past_the_box_is_covered_by_the_pad(masked_world, tmp_path):
    """필드의 상자를 넘어가게 쓴 글씨(정합 그림에 직접 그린다 — 상자 밖 pad_px 안)가 가린 그림에 남지 않는다."""
    w = masked_world
    con, site = w["pipe"].con, w["site"]
    work = tmp_path / "work"
    shutil.copytree(w["st"].work_root, work, ignore=shutil.ignore_patterns("*.db*"))
    st = replace(w["st"], work_root=work)
    page = next(p for p in loaded(con) if any(f.get("meta_key") == "operator" for f in site.templates[p["template_name"]].fields))
    tpl = site.templates[page["template_name"]]
    f = next(f for f in tpl.fields if f.get("meta_key") == "operator")
    x0, y0, x1, y1 = f["bbox"]
    img = imread_gray(work / page["aligned_image"])
    pad = DEFAULT_PAD_PX
    img[y1:y1 + pad - 1, x0 + 10:x0 + 14] = 7                                  # 상자 아래로 넘친 획 (pad 안)
    img[y0 + 5:y0 + 9, x1:x1 + pad - 1] = 7                                    # 오른쪽으로
    img[y1 + pad + 3:y1 + pad + 6, x0 + 10:x0 + 14] = 7                        # pad 밖 (대조 — 남는다)
    imwrite(work / page["aligned_image"], img)
    out = tmp_path / "o"
    export_masked(con, site, st, out, page_ids=[page["page_id"]], keep_text=True)
    got = imread_gray(out / f"{page['page_id']}.png")
    assert (got[y1:y1 + pad - 1, x0 + 10:x0 + 14] == FILL).all() and (got[y0 + 5:y0 + 9, x1:x1 + pad - 1] == FILL).all()
    assert (got[y1 + pad + 3:y1 + pad + 6, x0 + 10:x0 + 14] == 7).all()


@pytest.mark.slow                                  # masked_world (묶음 하나를 새로 돌린다)
def test_without_saved_aligned_images_the_same_picture(masked_world, tmp_path):
    """정합 그림을 저장하지 않은 DB(aligned_image 가 없다)에서는 원본을 호모그래피로 다시 편다 — 화소 차이는 보간만큼(평균 2 이하)."""
    w = masked_world
    con, site, st = w["pipe"].con, w["site"], w["st"]
    c2 = sqlite3.connect(":memory:")
    con.backup(c2)
    c2.row_factory = sqlite3.Row
    c2.execute("UPDATE doc_page SET aligned_image = NULL")
    a, b = tmp_path / "a", tmp_path / "b"
    ra = export_masked(con, site, st, a, date=w["day"])
    rb = export_masked(c2, site, st, b, date=w["day"])
    assert ra["pages"] == rb["pages"] > 0 and ra["boxes"] == rb["boxes"]
    for p in a.iterdir():
        x, y = imread_gray(p).astype(int), imread_gray(b / p.name).astype(int)
        assert np.abs(x - y).mean() <= 2.0, p.name


@pytest.mark.slow                                  # masked_world (묶음 하나를 새로 돌린다)
def test_variant_b_pages_use_their_own_boxes(masked_world, tmp_path):
    w = masked_world
    con, site, st = w["pipe"].con, w["site"], w["st"]
    a, b = site.templates["synth_usage_log"], site.templates["synth_usage_log_b"]
    assert a.redact[0]["bbox"] != b.redact[0]["bbox"]                          # 판마다 따로 (기하)
    pages = [p for p in loaded(con) if p["template_name"] == "synth_usage_log_b"]
    assert pages
    out = tmp_path / "o"
    export_masked(con, site, st, out, page_ids=[p["page_id"] for p in pages])
    for p in pages:
        got = imread_gray(out / f"{p['page_id']}.png")
        aligned = imread_gray(st.work_root / p["aligned_image"])
        x0, y0, x1, y1 = b.redact[0]["bbox"]
        assert (got[y0:y1, x0:x1] == FILL).all()
        mine = inside(got.shape, page_boxes(b, ("operator", "vehicle_no"), DEFAULT_PAD_PX))
        ax0, ay0, ax1, ay1 = a.redact[0]["bbox"]
        only_a = np.zeros(got.shape, bool)
        only_a[ay0 - DEFAULT_PAD_PX:ay1 + DEFAULT_PAD_PX, ax0 - DEFAULT_PAD_PX:ax1 + DEFAULT_PAD_PX] = True
        only_a &= ~mine
        assert only_a.any() and (got[only_a] == aligned[only_a]).all()         # 판 A 의 상자로 가리지 않았다


@pytest.mark.slow                                  # masked_world (묶음 하나를 새로 돌린다)
def test_command_refuses_the_repo_counts_skipped_pages_and_ignores_the_lock(masked_world, tmp_path, capsys):
    from minedocscan.cli import main
    from minedocscan.pipeline.lock import PipelineLock

    w = masked_world
    st, con = w["st"], w["pipe"].con
    common = ["--site", str(st.site), "--archive-root", str(st.archive_root), "--work-root", str(st.work_root)]
    repo = Path(__file__).resolve().parents[1]
    with pytest.raises(SystemExit) as e:
        main(["export", "masked-pages", str(repo / "out" / "masked"), "--date", w["day"], *common])
    assert "저장소 밖" in str(e.value) and not (repo / "out" / "masked").exists()
    pid = loaded(con)[0]["page_id"]
    lock = PipelineLock.for_settings(st).acquire()
    try:
        out = tmp_path / "o"
        assert main(["export", "masked-pages", str(out), "--page-id", pid, "nope-p1", *common]) == 0
    finally:
        lock.release()
    text = capsys.readouterr().out
    assert "가린 쪽 1장" in text and "없는 쪽 ID 1" in text and [p.name for p in out.iterdir()] == [f"{pid}.png"]
    with pytest.raises(SystemExit):
        main(["export", "masked-pages", str(tmp_path / "x"), "--date", "2030-02-30", *common])


def test_template_check_preview_and_variant_note(masked_pack, tmp_path):
    from minedocscan.tools.tpltools import check_template, draw
    from minedocscan.tools.variant import format_summary as variant_summary

    site = SitePack(masked_pack)
    src = site.templates["synth_haul_log"].dir
    tdir = tmp_path / "t"
    tdir.mkdir()
    (tdir / "reference.png").write_bytes((src / "reference.png").read_bytes())
    spec = yaml.safe_load((src / "template.yaml").read_text(encoding="utf-8"))
    h, w = Template(src / "template.yaml").reference.shape
    spec["redact"] = [{"name": "out", "bbox": [w - 10, 10, w + 50, 40]}, {"name": "flat", "bbox": [10, 10, 10, 40]},
                      {"name": "out", "bbox": [1, 1, 5, 5]}]
    (tdir / "template.yaml").write_text(yaml.safe_dump(spec, allow_unicode=True), encoding="utf-8")
    errs = check_template(tdir)
    assert any("redact/out: 쪽 밖으로" in e for e in errs) and any("redact/flat: 넓이가 없는" in e for e in errs)
    assert any("redact 의 이름 'out' 이 겹칩니다" in e for e in errs)
    tpl = site.templates["synth_haul_log"]
    img, n = draw(tpl, tpl.reference)
    plain = Template(src / "template.yaml")
    plain.redact = []
    img0, n0 = draw(plain, plain.reference)
    assert n == n0 + 1 and (img != img0).any()
    r = {"out": "x", "variant": "b", "template": "a", "family": "f", "inliers": 9, "rotation": 0, "tables": [],
         "fix_by_hand": [], "existing_needs": [], "existing": "y", "redact": 1}
    assert "가릴 자리(redact 1개)" in variant_summary(r) and "다시 확인" in variant_summary(r)
    from minedocscan.config import ConfigError
    from minedocscan.forms.sitepack import _redact

    assert _redact({}) == {"meta_keys": None, "pad_px": None}
    assert _redact({"redact": {"meta_keys": ["equipment"], "pad_px": 4}}) == {"meta_keys": ["equipment"], "pad_px": 4}
    assert _redact({"redact": {"meta_keys": []}}) == {"meta_keys": [], "pad_px": None}     # 빈 목록은 받는다 (가리지 않는다)
    for bad in ({"pad_px": -1}, {"pad_px": "8"}, {"meta_keys": "operator"}, {"other": 1}):
        with pytest.raises(ConfigError):
            _redact({"redact": bad})
    assert SitePack(site.root).redact == {"meta_keys": None, "pad_px": None}


def test_info_shows_redact_and_haul_table(masked_pack, tmp_path, monkeypatch, capsys):
    from minedocscan.cli import main

    quiet_env(monkeypatch)
    assert main(["info", "--json", "--site", str(masked_pack), "--work-root", str(tmp_path / "work")]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["redact"] == {"meta_keys": ["operator", "vehicle_no"], "pad_px": DEFAULT_PAD_PX, "default": True}
    assert data["haul_table"] == {"columns": 0, "slots": 0}


def test_table_signatures_inbox_refusal_and_meta_key_typos(masked_pack, tmp_path, monkeypatch):
    """표 안의 서명 칸도 가린다. 접수 폴더·보관 폴더 안의 OUT 은 거절한다 (가린 그림을 스캔으로 접수하게 된다).
    [redact] meta_keys 에 템플릿에 없는 키(오타)가 있으면 사이트 팩이 열리지 않는다 (조용히 아무것도 가리지 않는 일이 없게)."""
    from minedocscan.cli import main
    from minedocscan.config import ConfigError

    tdir = tmp_path / "t"
    tdir.mkdir()
    spec = {"name": "t_sig", "reference_image": "reference.png", "handler": "generic",
            "regions": [{"name": "main", "grid": {"ys": [0, 50, 100], "xs": [0, 100, 200]}, "header_rows": 1,
                         "columns": [{"idx": 0, "name": "what", "kind": "printed"}, {"idx": 1, "name": "sign", "kind": "signature"}],
                         "rows": [{"row": 0, "key": "a", "what": "x"}]}], "fields": []}
    (tdir / "template.yaml").write_text(yaml.safe_dump(spec), encoding="utf-8")
    boxes = page_boxes(Template(tdir / "template.yaml"), (), 0, keep_text=True)
    assert ("signature", (100, 50, 200, 100)) in boxes
    quiet_env(monkeypatch)
    st = Settings(site=masked_pack, archive_root=tmp_path / "보관", work_root=tmp_path / "work")
    work_db(st).close()                                                        # DB 가 있어도 열기 전에 거절한다
    inbox = tmp_path / "스캐너"
    monkeypatch.setenv("MINEDOCSCAN_INBOX", str(inbox))
    common = ["--site", str(st.site), "--archive-root", str(st.archive_root), "--work-root", str(st.work_root)]
    for out in (inbox / "가린", Path(st.archive_root) / "가린"):
        with pytest.raises(SystemExit) as e:
            main(["export", "masked-pages", str(out), "--date", "2030-01-07", *common])
        assert "안입니다" in str(e.value) and not out.exists()
    site_dir = tmp_path / "site"
    shutil.copytree(st.site, site_dir)
    with open(site_dir / "site.toml", "a", encoding="utf-8") as f:
        f.write('\n[redact]\nmeta_keys = ["operator", "vehicel_no"]\n')
    with pytest.raises(ConfigError) as e:
        SitePack(site_dir)
    assert "vehicel_no" in str(e.value)


# ── tasks/0009 4.1 마·사 ─────────────────────────────────────────────────────────
def test_default_pad_is_48_px_and_its_reason_is_beside_it():
    """넓히는 폭의 기본값은 48 px 이고 근거(실제 일보 30쪽, 초안 템플릿 — 16 px 로는 이름이 남았다)가 상수 바로 위의 주석에 있다.
    모듈 설명은 "수 칸은 남는다" 대신 "넓힌 상자에 걸친 만큼은 가려진다" (tasks/0009 4.1 마)."""
    import inspect

    from minedocscan.export import masked

    assert DEFAULT_PAD_PX == 48
    lines = inspect.getsource(masked).splitlines()
    at = next(i for i, ln in enumerate(lines) if ln.startswith("DEFAULT_PAD_PX ="))
    above = []
    for ln in reversed(lines[:at]):
        if not ln.startswith("#"):
            break
        above.append(ln)
    note = " ".join(above)
    assert "30쪽" in note and "48 px" in note and "16 px" in note and "상자 자체를 넓힌다" in note
    assert "넓힌 상자에 걸친 만큼은" in masked.__doc__


def test_no_pages_to_export_makes_no_out_folder(masked_pack, tmp_path, monkeypatch, capsys):
    """내보낼 쪽이 없으면(쪽이 없는 날짜, 적재되지 않은 쪽뿐인 날짜, 없는 쪽 ID) OUT 을 만들지 않고, 요약이 그렇다고 말한다 —
    함수로도 명령으로도 (tasks/0009 4.1 마)."""
    from minedocscan.cli import main

    site = SitePack(masked_pack)
    st = Settings(site=masked_pack, archive_root=tmp_path / "보관", work_root=tmp_path / "work")
    con = work_db(st, unloaded_day="2030-01-07")
    out = tmp_path / "가린"
    for kw, skipped in (({"date": "2030-01-08"}, {}), ({"date": "2030-01-07"}, {"not_loaded": 1}),
                        ({"page_ids": ["nope-p1"]}, {"missing": 1})):
        r = export_masked(con, site, st, out, **kw)
        assert r["pages"] == 0 and r["skipped"] == skipped and not out.exists(), kw
        text = format_summary(r, out)
        assert f"내보낼 쪽이 없습니다 — {out} 을 만들지 않았습니다" in text and "가린 쪽 0장" not in text
    con.close()
    quiet_env(monkeypatch)
    common = ["--site", str(st.site), "--archive-root", str(st.archive_root), "--work-root", str(st.work_root)]
    for sel in (["--date", "2030-01-08"], ["--date", "2030-01-07"], ["--page-id", "nope-p1"]):
        assert main(["export", "masked-pages", str(out), *sel, *common]) == 0
        assert "만들지 않았습니다" in capsys.readouterr().out and not out.exists(), sel


def test_empty_meta_keys_mask_no_meta_field_and_info_says_so(masked_pack, tmp_path, monkeypatch, capsys):
    """[redact] meta_keys = [] 는 받는다 — 메타 필드를 가리지 않는다 (기본값으로 바꾸지 않는다). info 가 그렇다고 적는다
    (tasks/0009 4.1 사)."""
    from minedocscan.cli import main

    site_dir = tmp_path / "site"
    shutil.copytree(masked_pack, site_dir)
    with open(site_dir / "site.toml", "a", encoding="utf-8") as f:
        f.write("\n[redact]\nmeta_keys = []\n")
    site = SitePack(site_dir)
    assert site.redact == {"meta_keys": [], "pad_px": None}
    quiet_env(monkeypatch)
    args = ["--site", str(site_dir), "--work-root", str(tmp_path / "work")]
    assert main(["info", *args]) == 0
    line = next(ln for ln in capsys.readouterr().out.splitlines() if "[redact]" in ln)
    assert "메타 필드를 가리지 않는다" in line and "operator" not in line and "(기본값)" not in line
    assert f"넓히는 폭 {DEFAULT_PAD_PX} px" in line
    assert main(["info", "--json", *args]) == 0
    assert json.loads(capsys.readouterr().out)["redact"] == {"meta_keys": [], "pad_px": DEFAULT_PAD_PX, "default": False}
    st = Settings(site=site_dir, archive_root=tmp_path / "보관", work_root=tmp_path / "work")
    r = export_masked(work_db(st), site, st, tmp_path / "o", date="2030-01-07")
    assert r["meta_keys"] == 0 and r["pad_px"] == DEFAULT_PAD_PX                # 빈 목록 그대로 (기본 키 둘이 아니다)
    tpl = next(t for t in site.templates.values() if any(f.get("meta_key") == "operator" for f in t.fields))
    assert "meta" not in {k for k, _b in page_boxes(tpl, site.redact["meta_keys"], DEFAULT_PAD_PX)}


def test_crops_and_error_sheets_refuse_the_inbox_and_the_archive(masked_pack, tmp_path, monkeypatch):
    """review export-crops(--meta 도)와 recognizer eval --errors 도 export excel·masked-pages 처럼 접수 폴더([paths] inbox 또는
    MINEDOCSCAN_INBOX)·보관 폴더(archive_root) 안을 한 줄로 거절한다 — 아무것도 쓰기 전에, 평가하기 전에 (tasks/0009 4.1 마)."""
    from minedocscan.cli import main
    from minedocscan.recognize.digits import evaluate as digits_evaluate
    from minedocscan.recognize.meta import evaluate as meta_evaluate
    from minedocscan.review.export import ExportError, export_crops, export_meta_crops

    quiet_env(monkeypatch)
    inbox, archive = tmp_path / "스캐너", tmp_path / "보관"
    inbox.mkdir()
    archive.mkdir()
    site = SitePack(masked_pack)
    st = Settings(site=masked_pack, archive_root=archive, work_root=tmp_path / "work", inbox=inbox)
    con = work_db(st)
    outs = (inbox, inbox / "크롭", archive / "크롭")
    for out in outs:                                                           # 함수
        for fn in (export_crops, export_meta_crops):
            with pytest.raises(ExportError) as e:
                fn(con, site, st, out)
            assert "안입니다" in str(e.value) and "\n" not in str(e.value), (fn.__name__, out)
    con.close()

    def boom(*_a, **_k):
        raise AssertionError("평가에 닿았다")

    monkeypatch.setattr(digits_evaluate, "evaluate", boom)
    monkeypatch.setattr(meta_evaluate, "evaluate_meta", boom)
    common = ["--site", str(masked_pack), "--archive-root", str(archive), "--work-root", str(st.work_root)]
    toml = tmp_path / "minedocscan.toml"
    toml.write_text(f"[paths]\ninbox = {json.dumps(str(inbox))}\n", encoding="utf-8")
    for how in ("config", "env"):                                              # 명령 — 접수 폴더는 설정 파일로도 환경변수로도
        extra = ["--config", str(toml)] if how == "config" else []
        if how == "env":
            monkeypatch.setenv("MINEDOCSCAN_INBOX", str(inbox))
        for out in outs:
            for cmd in (["review", "export-crops", str(out)], ["review", "export-crops", str(out), "--meta"],
                        ["recognizer", "eval", "--crops", str(tmp_path / "crops"), "--model", str(FIXTURE_MODEL),
                         "--errors", str(out)],
                        ["recognizer", "eval", "--crops", str(tmp_path / "crops"), "--model", str(META_FIXTURES / "meta-digits"),
                         "--errors", str(out)]):
                with pytest.raises(SystemExit) as e:
                    main([*cmd, *common, *extra])
                assert "안입니다" in str(e.value) and "\n" not in str(e.value), (how, cmd)
    assert list(inbox.iterdir()) == [] and list(archive.iterdir()) == []      # 아무것도 쓰지 않았다
    for model in (FIXTURE_MODEL, META_FIXTURES / "meta-digits"):              # 대조: 밖이면 평가까지 간다 (거절이 평가 앞이다)
        with pytest.raises(AssertionError, match="평가에 닿았다"):
            main(["recognizer", "eval", "--crops", str(tmp_path / "crops"), "--model", str(model),
                  "--errors", str(tmp_path / "errs"), *common])

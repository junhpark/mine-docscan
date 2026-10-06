"""인쇄 층 만들기 (tasks/0006 단계 2): template print-layer, print_image, print_mask, check·preview --print·info.

시험의 인쇄 층은 전부 합성 쪽으로 시험 중에 만든다 (tmp_path — 저장소에 넣지 않는다). 적재된 쪽은 usage_run(save_aligned=False)의
호모그래피로 다시 펴고(4.2, 3일치), 분류 전용 쪽은 명령이 직접 정합한다(usage_classify_only, 하루치). 수용 기준 1 은 합성
10일치에서 같은 방법으로 추정한 층(synth --print-layers — 정합·파이프라인 없이)으로 잰다.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from dataclasses import replace
from pathlib import Path

import cv2
import numpy as np
import pytest
import yaml

from conftest import clone_db
from minedocscan.cli import main
from minedocscan.config import Settings
from minedocscan.forms.sitepack import SitePack
from minedocscan.forms.template import Template, TemplateError
from minedocscan.imaging import grid, printlayer
from minedocscan.imaging.align import align_to_template
from minedocscan.imaging.io import image_size, imread_gray, imwrite, load_page
from minedocscan.tools import synth_usage
from minedocscan.tools.printlayer import PrintLayerError, build, page_image, pick_order, rewarp
from minedocscan.tools.tpltools import PRINT_COLOR, check_template, draw, handwritten_boxes, preview

USAGE = (synth_usage.T_USAGE, synth_usage.T_LOADER)
ROLE_TABLES = ("meter", "shifts", "tally")             # 인쇄 층을 쓰는 표 (4.3) — 수용 기준의 "인쇄로 잡힌 화소"를 재는 칸


def _copy_template(site: Path, name: str, dest: Path) -> Path:
    """템플릿 폴더 하나를 dest/<이름> 으로 복사한다 (세션 픽스처의 사이트 팩을 더럽히지 않게) — 인쇄 층 없이: 세션의 합성
    가동 일보는 인쇄 층을 켰다(usage_synth, 단계 3). 이 파일의 시험은 층이 없는 템플릿에서 시작한다 (print.png 와 키를 뺀다)."""
    out = dest / name
    shutil.copytree(site / "templates" / name, out, ignore=shutil.ignore_patterns("print.png"))
    _drop_print(out)
    return out


def _drop_print(tdir: Path) -> None:
    p = tdir / "template.yaml"
    spec = yaml.safe_load(p.read_text(encoding="utf-8"))
    if spec.pop("print_image", None) is not None:
        p.write_text(yaml.safe_dump(spec, allow_unicode=True, sort_keys=False), encoding="utf-8")


def _plain_site(site: Path, dest: Path) -> Path:
    """사이트 팩을 인쇄 층 없이 복사한다."""
    shutil.copytree(site, dest, ignore=shutil.ignore_patterns("print.png"))
    for t in (dest / "templates").iterdir():
        _drop_print(t)
    return dest


def _copy_built(layers: dict, name: str, dest: Path) -> Path:
    """layers 의 템플릿 폴더(print.png 가 있다)를 dest/<이름> 으로 복사한다."""
    out = dest / name
    shutil.copytree(layers[name]["dir"], out)
    return out


@pytest.fixture(scope="module")
def layers(usage_synth, usage_run, tmp_path_factory) -> dict:
    """합성 가동 일보 두 종의 인쇄 층 (기본 설정: 최대 40장, 백분위 75). 정합 그림이 WORK_ROOT 에 없으므로 다시 펴는 경로다.
    {양식 이름: {"dir": 템플릿 폴더 복사본, "summary": 요약}}."""
    root = tmp_path_factory.mktemp("print_layers")
    out = {}
    for name in USAGE:
        tdir = _copy_template(usage_synth.site, name, root)
        out[name] = {"dir": tdir, "summary": build(tdir, usage_run["settings"])}
    return out


def _false_print(name: str, layer: np.ndarray, tpl: Template) -> tuple[int, int, list[float]]:
    """role 표의 손으로 쓰는 칸에서 (인쇄 아닌 화소 중 층이 인쇄로 잡은 수, 인쇄 아닌 화소의 수, 칸마다의 비율).
    인쇄 = 생성기의 빈 그림을 grid.binarize 한 것 (수용 기준의 정의 그대로)."""
    blank_print = grid.binarize(synth_usage.BUILDERS[name]()[0]) > 0
    roles = {reg["name"]: reg.get("role") for reg in tpl.regions}
    num = den = 0
    per = []
    for c in tpl.cells():
        if roles.get(c.region) in ROLE_TABLES and c.kind.startswith("handwritten"):
            x0, y0, x1, y1 = c.bbox
            free = ~blank_print[y0:y1, x0:x1]
            n = int((layer[y0:y1, x0:x1] & free).sum())
            num, den = num + n, den + int(free.sum())
            per.append(round(n / max(1, int(free.sum())), 4))
    return num, den, per


# ── 수용 기준 1: 빈 양식의 인쇄를 덮고, role 표의 칸에는 인쇄가 거의 없다 ──────────────
def test_layer_covers_the_blank_print_and_leaves_the_value_cells_clear(synth10):
    """양식마다: 넓히지 않은 층(binary)이 빈 양식의 인쇄 화소를 99 % 이상 덮고, role 표의 손으로 쓰는 칸 전체에서 인쇄 아닌 화소 중
    인쇄로 잡힌 것이 1 % 미만이다. 2 px 넓힌 마스크로 재지 않는다 (괘선 둘레가 칸의 4 px 안쪽으로 들어온다).
    층은 10일치(운행일보 31쪽, 로우더 20쪽)를 synth --print-layers 가 추정한 것 (정합 대신 스캔 효과의 기하 행렬로 되돌린 쪽 — 명령의
    다시 펴기와 같은 추정) — 3일치(운행일보 9쪽)는 모든 쪽이 같은 자리에 계기 값을 써서 운행일보만 약 1.9 % (12절).
    10일치에서 운행일보 0.46 %·로우더 0.56 % (OpenCV 5.0), 0.48 %·0.58 % (4.9), 덮음은 둘 다 1.0. 칸마다의 값은 메시지에만."""
    pages = {name: sum(p["template"] == name for info in synth10.truth["documents"].values() for p in info) for name in USAGE}
    assert pages == {synth_usage.T_USAGE: 31, synth_usage.T_LOADER: 20}
    for name in USAGE:
        tdir = synth10.site / "templates" / name
        layer = printlayer.binary(imread_gray(tdir / "print.png"))
        blank_print = grid.binarize(synth_usage.BUILDERS[name]()[0]) > 0
        cov = float((layer & blank_print).sum() / blank_print.sum())
        n, d, per = _false_print(name, layer, Template(tdir / "template.yaml"))
        msg = f"{pages[name]}쪽: 덮음 {cov:.4f}, 인쇄로 잡힌 칸 화소 {n}/{d} = {n / d:.4f}, 칸마다 {per}"
        assert printlayer.sha(imread_gray(tdir / "print.png")) == synth10.truth["print_layers"][name], msg
        assert cov >= 0.99, msg
        assert n / d < 0.01, msg


# ── 수용 기준 2: 1장이면 거절, 3장이면 경고, 같은 쪽이면 같은 해시 ──────────────────────
def test_one_page_is_refused_three_pages_warn_and_the_layer_is_deterministic(usage_synth, usage_run, tmp_path):
    tdir = _copy_template(usage_synth.site, synth_usage.T_LOADER, tmp_path)
    s = usage_run["settings"]
    with pytest.raises(PrintLayerError, match="2장 이상") as e:          # 인자의 거절 (쪽을 보기 전에)
        build(tdir, s, max_pages=1)
    assert "\n" not in str(e.value) and not (tdir / "print.png").exists()

    a = build(tdir, s, max_pages=3, out=tmp_path / "a.png")
    # 같은 쪽·같은 설정 → 같은 그림. 설정의 dpi 가 달라도 적재된 쪽은 doc_page.render_dpi 로 다시 렌더링한다 (4.2)
    b = build(tdir, replace(s, dpi=150), max_pages=3, out=tmp_path / "b.png")
    assert a["pages"] == 3 and a["dates"] == 3                      # 날짜별로 고르게: 사흘에서 한 장씩
    assert len(a["warnings"]) == 1 and "5장 미만" in a["warnings"][0]
    assert a["sha"] == b["sha"] == printlayer.sha(imread_gray(tmp_path / "a.png"))
    assert (tmp_path / "a.png").read_bytes() == (tmp_path / "b.png").read_bytes()
    assert not (tdir / "print.png").exists()                        # --out 이면 템플릿 폴더에 쓰지 않는다
    assert yaml.safe_load((tdir / "template.yaml").read_text(encoding="utf-8")).get("print_image") is None
    assert "print_image: a.png" not in (a["hint"] or "")            # 템플릿 폴더 밖이면 파일 이름을 그대로 안내하지 않는다
    # --percentile 이 그림에 닿는다 (같은 세 쪽, 50 이면 다른 그림)
    c = build(tdir, s, max_pages=3, percentile=50, out=tmp_path / "c.png")
    assert c["percentile"] == 50 and c["pages"] == 3 and c["sha"] != a["sha"]

    # 키가 파일보다 먼저 있어도 만든다 (검증 없이 읽는다 — 4.2). 이미 이 파일을 가리키므로 안내는 없다
    _with_print(tdir)
    with pytest.raises(TemplateError, match="인쇄 층 파일이 없습니다"):
        Template(tdir / "template.yaml")
    k = build(tdir, s, max_pages=3)
    assert k["sha"] == a["sha"] and k["hint"] is None and (tdir / "print.png").is_file()
    assert Template(tdir / "template.yaml").print_sha == a["sha"]    # 이제 검증을 통과한다

    # 분류된 쪽이 2장 미만이면 (없는 양식 이름) 거절
    spec = yaml.safe_load((tdir / "template.yaml").read_text(encoding="utf-8"))
    spec["name"] = "synth_nothing_here"
    (tdir / "template.yaml").write_text(yaml.safe_dump(spec), encoding="utf-8")
    with pytest.raises(PrintLayerError, match="0장뿐"):
        build(tdir, s)


def test_pages_that_cannot_be_used_are_counted_and_replaced(usage_synth, usage_run, tmp_path):
    """쓸 수 있는 쪽이 1장이면 거절 (인자가 아니라 쪽을 모은 뒤의 검사). 뺀 쪽은 이유별로 세고, 다음 쪽으로 채운다."""
    name = synth_usage.T_LOADER
    tdir = _copy_template(usage_synth.site, name, tmp_path)
    s = usage_run["settings"]
    con = clone_db(usage_run["pipe"].con)
    ids = [r[0] for r in con.execute("SELECT DISTINCT p.document_id FROM doc_page p WHERE p.template_name = ? "
                                     "ORDER BY p.document_id", (name,))]
    n_pages = con.execute("SELECT COUNT(*) FROM doc_page WHERE template_name = ?", (name,)).fetchone()[0]
    assert len(ids) == 3 and n_pages == 6                           # 하루 한 묶음, 로우더 두 쪽씩

    def lose(doc_ids):                                              # 원본을 찾을 수 없게 (절대경로도 상대경로도 없다)
        con.executemany("UPDATE doc_document SET source_path = ?, source_rel = NULL WHERE document_id = ?",
                        [(str(tmp_path / "gone.pdf"), d) for d in doc_ids])

    lose(ids[:2])                                                   # 남은 하루치 두 쪽 — 앞의 날짜 쪽은 빠지고 그것으로 채운다
    r = build(tdir, s, max_pages=3, out=tmp_path / "two.png", con=con)
    assert r["pages"] == 2 and r["skipped"] == {"no_source": 4} and r["dates"] == 1 and r["warnings"]
    con.execute("UPDATE doc_page SET status = 'align_failed' WHERE page_id = (SELECT page_id FROM doc_page "
                "WHERE document_id = ? AND template_name = ? ORDER BY page_no DESC LIMIT 1)", (ids[2], name))
    with pytest.raises(PrintLayerError, match="1장뿐") as e:
        build(tdir, s, out=tmp_path / "one.png", con=con)
    assert "no_source 4" in str(e.value) and "\n" not in str(e.value) and not (tmp_path / "one.png").exists()
    # 분류 전용 쪽을 정합하지 못하면 (인라이어 부족) 뺀다: 아무것도 없는 흰 쪽
    tpl = Template(tdir / "template.yaml")
    imwrite(tmp_path / "white.png", np.full(tpl.reference.shape, 255, np.uint8))
    row = {"status": "classified_only", "aligned_image": None, "homography": None, "render_dpi": None,
           "source_path": str(tmp_path / "white.png"), "source_rel": None, "page_no": 1}
    assert page_image(row, tpl, s, tpl.reference.shape) == (None, "few_inliers")
    row["source_path"] = str(tmp_path / "gone.png")
    assert page_image(row, tpl, s, tpl.reference.shape) == (None, "no_source")
    assert page_image({**row, "status": "loaded"}, tpl, s, tpl.reference.shape) == (None, "no_homography")


def test_pick_order_spreads_over_dates_and_puts_undated_last():
    rows = [{"work_date": d, "document_id": doc, "page_no": n} for d, doc, n in
            [(None, "z", 1), ("2026-01-02", "b", 2), ("2026-01-01", "a", 1), ("2026-01-02", "b", 1),
             (None, "y", 1), ("2026-01-01", "a", 2), ("", "x", 1)]]
    got = [(r["work_date"], r["document_id"], r["page_no"]) for r in pick_order(rows)]
    assert got == [("2026-01-01", "a", 1), ("2026-01-02", "b", 1), ("", "x", 1),
                   ("2026-01-01", "a", 2), ("2026-01-02", "b", 2), (None, "y", 1), (None, "z", 1)]


def test_output_inside_a_git_tree_needs_allow_in_repo(usage_synth, usage_run, tmp_path):
    tdir = _copy_template(usage_synth.site, synth_usage.T_LOADER, tmp_path)
    (tmp_path / ".git").mkdir()                                    # tmp_path 를 작업 트리로
    with pytest.raises(PrintLayerError, match="git 작업 트리"):
        build(tdir, usage_run["settings"], max_pages=2)
    assert not (tdir / "print.png").exists()
    r = build(tdir, usage_run["settings"], max_pages=2, allow_in_repo=True)
    assert r["pages"] == 2 and (tdir / "print.png").is_file()


def test_default_build_writes_print_png_and_only_hints_the_key(layers):
    for name in USAGE:
        r = layers[name]["summary"]
        tdir = layers[name]["dir"]
        assert Path(r["out"]) == tdir / "print.png" and (tdir / "print.png").is_file()
        assert r["pages"] == r["candidates"] and r["warnings"] == [] and r["skipped"] == {}
        assert r["by_source"] == {"rewarped": r["pages"]} and r["percentile"] == 75
        assert "print_image: print.png" in r["hint"]
        assert "print_image" not in yaml.safe_load((tdir / "template.yaml").read_text(encoding="utf-8"))
        assert image_size(tdir / "print.png") == image_size(tdir / "reference.png")
        assert 0 < r["print_ratio"] < 0.2 and r["covered"]
        hand = {n for n, _b in handwritten_boxes(Template(tdir / "template.yaml", validate=False))}
        assert {c["cell"] for c in r["covered"]} <= hand and len(r["covered"]) <= 10
        assert [c["coverage"] for c in r["covered"]] == sorted((c["coverage"] for c in r["covered"]), reverse=True)
        # 덮인 비율은 넓히지 않은 층(binary)으로 잰다 (마스크가 아니다)
        layer = imread_gray(tdir / "print.png")
        boxes = dict(handwritten_boxes(Template(tdir / "template.yaml", validate=False)))
        top = r["covered"][0]
        assert top["coverage"] == round(printlayer.coverage(printlayer.binary(layer), [boxes[top["cell"]]])[0], 4)
        assert top["coverage"] < round(printlayer.coverage(printlayer.mask(layer), [boxes[top["cell"]]])[0], 4)
        # 3일치(다시 편 쪽)로도 빈 양식의 인쇄는 다 덮는다 (role 칸의 잔상은 synth10 의 시험)
        blank_print = grid.binarize(synth_usage.BUILDERS[name]()[0]) > 0
        assert (printlayer.binary(layer) & blank_print).sum() / blank_print.sum() >= 0.99


# ── 정합 그림이 있을 때와 없을 때 같은 그림 (4.2, 6절) ──────────────────────────────────
def test_saved_aligned_images_and_the_rewarp_give_the_same_layer(layers, usage_synth, usage_run, tmp_path):
    """시험 픽스처는 save_aligned=False 라 인쇄 층은 다시 펴서 만든다. 정합 그림을 WORK_ROOT 에 두면 그것을 읽고,
    print.png 가 바이트까지 같다. 다시 펴기는 정합(align_to_template)이 만든 그림과 같다."""
    name = synth_usage.T_LOADER
    tpl = Template(layers[name]["dir"] / "template.yaml")
    con = clone_db(usage_run["pipe"].con)
    rows = con.execute("SELECT p.page_id, p.page_no, p.homography, p.render_dpi, d.source_path FROM doc_page p "
                       "JOIN doc_document d ON p.document_id = d.document_id WHERE p.template_name = ? "
                       "AND p.status = 'loaded' ORDER BY p.page_id", (name,)).fetchall()
    assert len(rows) == layers[name]["summary"]["pages"]
    work = tmp_path / "work"
    for i, r in enumerate(rows):
        gray = load_page(r["source_path"], r["page_no"], r["render_dpi"])
        ar = align_to_template(gray, tpl.reference, tpl.regions, ref_features=tpl.features)
        if i == 0:
            assert np.array_equal(rewarp(gray, r["homography"], tpl.reference.shape), ar.warped)
        rel = f"aligned/{r['page_id']}.png"
        imwrite(work / rel, ar.warped if i else ar.warped[:-1])      # 크기가 다른 정합 그림은 쓰지 않고 다시 편다
        con.execute("UPDATE doc_page SET aligned_image = ? WHERE page_id = ?", (rel, r["page_id"]))
    s = Settings(site=usage_synth.site, archive_root=usage_synth.scans, work_root=work, save_aligned=False)
    r = build(layers[name]["dir"], s, out=tmp_path / "from-aligned.png", con=con)
    assert r["by_source"] == {"aligned": len(rows) - 1, "rewarped": 1}
    assert r["sha"] == layers[name]["summary"]["sha"]
    assert (tmp_path / "from-aligned.png").read_bytes() == (layers[name]["dir"] / "print.png").read_bytes()


# ── 분류 전용 템플릿: 쪽을 직접 정합한다, DB 에는 쓰지 않는다 (4.2) ────────────────────────
def test_classification_only_pages_are_aligned_by_the_command(usage_classify_only):
    """usage_classify_only 의 사이트 팩은 두 양식의 칸 정의를 지웠다 → 쪽은 classified_only(호모그래피·정합 그림 없음).
    print-layer 는 그 쪽들을 직접 정합해 만들고(aligned_now), DB 는 바이트까지 그대로다 (읽기 전용으로 연다). 하루치라 5장 미만 —
    경고와 함께 만든다. 쪽은 판이 섞이지 않은 synth10 의 첫날이다 — 층에 생성기의 빈 양식의 인쇄와 표의 괘선이 다 남는다
    (단계 6 의 add-region 이 이 층에서 괘선을 잡는다. 판이 반씩 섞인 쪽이면 75 백분위 층에서 표 괘선이 빠진다 — 4.2)."""
    con = sqlite3.connect(usage_classify_only["db"])
    rows = con.execute("SELECT template_name, status, homography, aligned_image FROM doc_page").fetchall()
    con.close()
    assert rows and {r[1] for r in rows} == {"classified_only"} and all(r[2] is None and r[3] is None for r in rows)
    assert {r[0] for r in rows} == set(USAGE)
    assert hashlib.sha256(usage_classify_only["db"].read_bytes()).hexdigest() == usage_classify_only["db_sha"]
    for name in USAGE:
        r = usage_classify_only["layers"][name]["summary"]
        n = sum(t == name for t, *_ in rows)
        assert r["by_source"] == {"aligned_now": n} and r["pages"] == r["candidates"] == n >= 2 and r["skipped"] == {}
        assert r["dates"] == 1 and len(r["warnings"]) == 1 and "5장 미만" in r["warnings"][0] and r["covered"]
        blank, spec = synth_usage.BUILDERS[name]()
        layer = printlayer.binary(imread_gray(usage_classify_only["layers"][name]["dir"] / "print.png"))
        blank_print = grid.binarize(blank) > 0
        cov = float((layer & blank_print).sum() / blank_print.sum())
        lines = _line_presence(layer, spec)
        assert cov >= 0.99 and min(lines.values()) >= 0.99, (name, round(cov, 4), lines)


def _line_presence(layer: np.ndarray, spec: dict) -> dict[str, float]:
    """생성기의 표 괘선마다 그 길이 중 층의 인쇄 화소(±1 px 안)가 있는 비율 — 괘선이 층에서 빠졌는지 (4.2)."""
    out = {}
    for reg in spec["regions"]:
        ys, xs = reg["grid"]["ys"], reg["grid"]["xs"]
        for y in ys:
            out[f"{reg['name']}/y{y}"] = round(float(layer[y - 1:y + 2, xs[0]:xs[-1]].any(axis=0).mean()), 3)
        for x in xs:
            out[f"{reg['name']}/x{x}"] = round(float(layer[ys[0]:ys[-1], x - 1:x + 2].any(axis=1).mean()), 3)
    return out


# ── 요약·오류 메시지에 값·이름이 없다 ──────────────────────────────────────────────
NAMES = sorted({e[0] for e in synth_usage.EQUIPMENT} | {e[3] for e in synth_usage.EQUIPMENT} | {synth_usage.NIGHT_OPERATOR}
               | {f"{i}|{p}" for i in synth_usage.ITEMS for p in synth_usage.PLACES})


def _no_names(text: str) -> None:
    found = [n for n in NAMES if n in text]
    assert not found, found


def test_summary_and_errors_have_no_values_or_names(layers, usage_classify_only, usage_synth, usage_run, tmp_path, capsys):
    from minedocscan.tools.printlayer import format_summary

    for r in [x[n]["summary"] for x in (layers, usage_classify_only["layers"]) for n in USAGE]:
        _no_names(json.dumps(r, ensure_ascii=False))
        _no_names(format_summary(r))
        assert all(c["cell"].startswith(("fields/", "meter/", "shifts/", "tally/", "work/")) for c in r["covered"])
    tdir = _copy_template(usage_synth.site, synth_usage.T_LOADER, tmp_path)
    s = usage_run["settings"]
    common = ["--work-root", str(s.work_root), "--archive-root", str(s.archive_root)]
    for args in (["--max-pages", "1"], ["--out", str(Path(__file__).parent / "print-should-not-exist.png")],
                 ["--percentile", "120"]):
        with pytest.raises(SystemExit) as e:
            main(["template", "print-layer", str(tdir), *common, *args])
        msg = str(e.value.code)
        assert msg and "\n" not in msg, msg
        _no_names(msg)
    assert not (Path(__file__).parent / "print-should-not-exist.png").exists() and not (tdir / "print.png").exists()
    with pytest.raises(SystemExit) as e:                                # DB 가 없다
        main(["template", "print-layer", str(tdir), "--work-root", str(tmp_path / "nowhere")])
    assert "DB 가 없습니다" in str(e.value.code) and not (tmp_path / "nowhere").exists()
    capsys.readouterr()
    assert main(["template", "print-layer", str(tdir), *common, "--json"]) == 0
    out = capsys.readouterr().out
    _no_names(out)
    r = json.loads(out)
    assert r["sha"] == layers[synth_usage.T_LOADER]["summary"]["sha"] and r["percentile"] == 75
    assert main(["template", "print-layer", str(tdir), *common, "--out", str(tmp_path / "t.png")]) == 0
    text = capsys.readouterr().out
    _no_names(text)
    assert "print_sha" in text and "저장소에 넣지" in text


def test_broken_inputs_are_refused_in_one_line(usage_synth, usage_run, tmp_path):
    """깨진 템플릿·DB, PNG 가 아닌 출력: 계산하기 전에 한 줄로 거절한다 (traceback 이 아니라). print.png 를 쓰지 않는다."""
    from minedocscan.store.db import open_db

    s = usage_run["settings"]
    common = ["--work-root", str(s.work_root), "--archive-root", str(s.archive_root)]

    def refused(tdir, *args, work=None):
        cmn = common if work is None else ["--work-root", str(work)]
        with pytest.raises(SystemExit) as e:
            main(["template", "print-layer", str(tdir), *cmn, *args])
        msg = str(e.value.code)
        assert msg and "\n" not in msg and "Traceback" not in msg, msg
        _no_names(msg)
        assert not (tdir / "print.png").exists() and not list(tmp_path.glob("**/out.*"))
        return msg

    tdir = _copy_template(usage_synth.site, synth_usage.T_LOADER, tmp_path / "ok")
    assert "PNG" in refused(tdir, "--out", str(tmp_path / "out.jpg"))
    assert "PNG" in refused(tdir, "--out", str(tmp_path / "out.xyz"))
    for case, edit in {"no_reference": lambda sp: sp.pop("reference_image"),
                       "fields_not_list": lambda sp: sp.update(fields="oops"),
                       "bad_reference": lambda sp: sp.update(reference_image="missing.png")}.items():
        d = _copy_template(usage_synth.site, synth_usage.T_LOADER, tmp_path / case)
        spec = yaml.safe_load((d / "template.yaml").read_text(encoding="utf-8"))
        edit(spec)
        (d / "template.yaml").write_text(yaml.safe_dump(spec, allow_unicode=True), encoding="utf-8")
        assert "template check" in refused(d), case
    for case, content in {"empty_db": b"", "garbage_db": b"not a database" * 100}.items():
        w = tmp_path / case
        w.mkdir()
        (w / "minedocscan.db").write_bytes(content)
        assert "DB" in refused(tdir, work=w), case
    w = tmp_path / "old_db"                                          # 스키마 버전이 다른 DB
    con = open_db(f"sqlite:///{(w / 'minedocscan.db').as_posix()}")
    con.execute("UPDATE meta_schema SET value = '1' WHERE key = 'schema_version'")
    con.commit()
    con.close()
    assert "스키마 버전" in refused(tdir, work=w)


# ── print_image: 템플릿의 키, check, preview --print, info ────────────────────────────
def _with_print(tdir: Path, img: np.ndarray | None = None, value="print.png") -> Path:
    """tdir 의 template.yaml 에 print_image 를 적고 (img 가 있으면) 그 그림을 쓴다."""
    if img is not None:
        imwrite(tdir / str(value), img)
    p = tdir / "template.yaml"
    spec = yaml.safe_load(p.read_text(encoding="utf-8"))
    spec["print_image"] = value
    p.write_text(yaml.safe_dump(spec, allow_unicode=True), encoding="utf-8")
    return tdir


def test_template_print_properties(layers, tmp_path):
    tdir = _copy_built(layers, synth_usage.T_LOADER, tmp_path)
    plain = Template(tdir / "template.yaml")
    assert (plain.print_path, plain.print_layer, plain.print_mask, plain.print_sha) == (None, None, None, None)
    tpl = Template(_with_print(tdir) / "template.yaml")
    layer = imread_gray(tdir / "print.png")
    assert tpl.print_path == tdir / "print.png" and np.array_equal(tpl.print_layer, layer)
    assert tpl.print_sha == layers[synth_usage.T_LOADER]["summary"]["sha"] == printlayer.sha(layer)
    m = tpl.print_mask
    assert m.dtype == bool and m.shape == layer.shape and tpl.print_mask is m          # 한 번만 계산
    b = printlayer.binary(layer)
    assert np.array_equal(m, cv2.dilate(b.astype(np.uint8), np.ones((5, 5), np.uint8)) > 0) and m.sum() > b.sum()
    # 다른 도구로 다시 저장해도(화소가 같으면) 해시가 같다 — PNG 바이트가 아니라 화소의 해시
    imwrite(tmp_path / "again.png", layer)
    cv2.imencode(".png", layer, [cv2.IMWRITE_PNG_COMPRESSION, 0])[1].tofile(str(tmp_path / "raw.png"))
    assert (tmp_path / "raw.png").read_bytes() != (tdir / "print.png").read_bytes()
    assert printlayer.sha(imread_gray(tmp_path / "raw.png")) == tpl.print_sha


def test_print_image_problems_are_template_errors(layers, tmp_path):
    src = layers[synth_usage.T_LOADER]["dir"]
    ref = imread_gray(src / "reference.png")
    cases = {"missing": (None, "print.png", "인쇄 층 파일이 없습니다"),
             "size": (ref[:-10, :], "print.png", "크기"),
             "empty": (None, "", "파일 이름이어야"),
             "jpeg": (ref, "print.jpg", "PNG 파일이어야")}
    for case, (img, value, needle) in cases.items():
        d = tmp_path / case
        d.mkdir()
        tdir = _copy_built(layers, synth_usage.T_LOADER, d)
        (tdir / "print.png").unlink()
        _with_print(tdir, img, value)
        errs = check_template(tdir)
        assert len(errs) == 1 and needle in errs[0], (case, errs)
        with pytest.raises(TemplateError, match=needle):
            Template(tdir / "template.yaml")


def test_site_pack_with_a_key_but_no_file_does_not_load(usage_synth, tmp_path):
    """키가 파일보다 먼저 있으면 사이트 팩 전체가 읽히지 않는다 — 그래서 print-layer 는 키를 적지 않고 안내만 한다 (4.2)."""
    site = _plain_site(usage_synth.site, tmp_path / "site")
    _with_print(site / "templates" / synth_usage.T_USAGE)
    with pytest.raises(TemplateError, match="인쇄 층 파일이 없습니다"):
        SitePack(site)


def test_check_flags_value_cells_covered_by_the_print_layer(layers, tmp_path):
    # 맞는 층: 알릴 것이 없다 (두 양식)
    for name in USAGE:
        tdir = _copy_built(layers, name, tmp_path / "ok")
        assert check_template(_with_print(tdir)) == [], name
    # 일부러 틀린 층 — 쪽 전체에 8 px 마다 가로줄: 손으로 쓰는 칸 전부, 열까지 이름 뒤 나머지는 수로, 한 줄.
    # (온통 검은 그림은 적응 이진화에서 인쇄가 하나도 없다 — 주변보다 어두운 곳이 없으므로)
    name = synth_usage.T_LOADER
    tdir = _copy_built(layers, name, tmp_path / "lines")
    ref = imread_gray(tdir / "reference.png")
    _with_print(tdir, np.where(np.arange(ref.shape[0])[:, None] % 8 < 2, 0, 255).astype(np.uint8) + np.zeros_like(ref))
    hand = handwritten_boxes(Template(tdir / "template.yaml"), fields=False)     # 표의 칸만 — 필드의 잔상은 오류가 아니다
    errs = check_template(tdir)
    assert len(errs) == 1 and f"손으로 쓰는 칸 {len(hand)}개" in errs[0] and f"외 {len(hand) - 10}개" in errs[0], errs
    assert hand[0][0] in errs[0] and hand[10][0] not in errs[0]
    _no_names(errs[0])
    # 칸 하나에만 인쇄가 든 층: 그 칸만 (행 키가 아니라 "<표>/<열>/행 N")
    tdir = _copy_built(layers, name, tmp_path / "one")
    layer = imread_gray(tdir / "print.png")
    label, (x0, y0, x1, y1) = next((n, b) for n, b in hand if n == "meter/start/행 0")
    layer[y0:y1, (x0 + x1) // 2 - 40:(x0 + x1) // 2 + 40] = 0                  # 칸 가운데에 굵은 "인쇄"
    layer[y0 + 2:y1 - 2, x0 + 2:x1 - 2] = np.where(
        np.arange(x1 - x0 - 4)[None, :] % 6 < 4, 0, 255).astype(np.uint8)       # 칸을 세로줄 무늬로 — 넓히면 다 덮인다
    _with_print(tdir, layer)
    errs = check_template(tdir)
    assert len(errs) == 1 and "칸 1개" in errs[0] and errs[0].endswith(label), errs
    # 필드를 통째로 덮은 층 (늘 같은 자리에 같은 글씨로 쓰는 필드의 잔상): 오류가 아니다 — 필드는 인쇄 층을 쓰지 않는다 (4.3)
    tdir = _copy_built(layers, name, tmp_path / "field")
    layer = imread_gray(tdir / "print.png")
    fields = [b for n, b in handwritten_boxes(Template(tdir / "template.yaml")) if n.startswith("fields/")]
    assert fields
    for x0, y0, x1, y1 in fields:
        layer[y0:y1, x0:x1] = np.where(np.arange(x1 - x0)[None, :] % 6 < 4, 0, 255).astype(np.uint8)
    _with_print(tdir, layer)
    assert printlayer.coverage(Template(tdir / "template.yaml").print_mask, fields) == [1.0] * len(fields)
    assert check_template(tdir) == []
    # 머리(IHDR)는 맞는데 그림이 깨졌다: 템플릿은 읽히지만(크기만 본다) check 가 알린다
    tdir = _copy_built(layers, name, tmp_path / "corrupt")
    data = (tdir / "print.png").read_bytes()
    (tdir / "print.png").write_bytes(data[:len(data) // 3])
    _with_print(tdir)
    errs = check_template(tdir)
    assert len(errs) == 1 and "깨졌습니다" in errs[0], errs


def test_preview_print_draws_on_the_layer(layers, tmp_path, capsys):
    name = synth_usage.T_LOADER
    tdir = _copy_built(layers, name, tmp_path)
    # 인쇄 층이 없으면 거절 (한 줄)
    with pytest.raises(SystemExit) as e:
        main(["template", "preview", str(tdir), "--print", "--out", str(tmp_path / "prev")])
    assert "인쇄 층이 없습니다" in str(e.value.code) and "\n" not in str(e.value.code)
    _with_print(tdir)
    r = preview(tdir, tmp_path / "prev", print_layer=True)
    img = cv2.imread(r["out"])
    tpl = Template(tdir / "template.yaml")
    assert img.shape[:2] == tpl.reference.shape and Path(r["out"]).name == f"{name}__print.png"
    tinted = np.all(img == np.array(PRINT_COLOR, np.uint8), axis=2)
    b = printlayer.binary(tpl.print_layer)
    legend = np.zeros_like(b)
    legend[:220, :320] = True                                                  # 범례의 색 견본
    assert tinted.sum() > 0.5 * b.sum() and not tinted[~b & ~legend].any()     # 칠한 것은 인쇄 화소뿐
    assert np.array_equal(img, draw(tpl, tpl.print_layer, tint=b)[0])          # 바탕은 기준 이미지가 아니라 인쇄 층
    assert not np.array_equal(img, draw(tpl, tpl.reference, tint=b)[0])
    assert r["boxes"] == len(tpl.cells()) + len(tpl.field_cells())
    with pytest.raises(ValueError, match="같이 쓰지 않습니다"):
        preview(tdir, tmp_path / "prev", scan=tdir / "reference.png", print_layer=True)
    assert main(["template", "preview", str(tdir), "--print", "--out", str(tmp_path / "prev2")]) == 0
    assert "__print.png" in capsys.readouterr().out


def test_info_says_which_templates_have_a_print_layer(layers, usage_synth, tmp_path, capsys):
    """info 는 템플릿마다 인쇄 층과 해시, 그리고 그것으로 재는 칸이 있는지(print_used). 역할이 없는 표뿐인 양식(운반 일보)에
    print_image 를 적으면 쓰이지 않는다 (4.3 — role meter·shifts·tally 의 형식 있는 칸만) — info 와 template check 가 한 줄로
    알린다 (check 는 오류가 아니라 참고: 종료 코드 0)."""
    from minedocscan.tools.synth import T_LOG

    site = _plain_site(usage_synth.site, tmp_path / "site")
    shutil.copy(layers[synth_usage.T_LOADER]["dir"] / "print.png", site / "templates" / synth_usage.T_LOADER / "print.png")
    _with_print(site / "templates" / synth_usage.T_LOADER)
    haul = site / "templates" / T_LOG
    _with_print(haul, imread_gray(haul / Template(haul / "template.yaml").spec["reference_image"]))
    assert main(["info", "--site", str(site), "--work-root", str(tmp_path / "w"), "--json"]) == 0
    tpls = {t["name"]: t for t in json.loads(capsys.readouterr().out)["site"]["templates"]}
    assert tpls[synth_usage.T_LOADER]["print_image"] == "print.png" and tpls[synth_usage.T_LOADER]["print_used"]
    assert tpls[synth_usage.T_LOADER]["print_sha"] == layers[synth_usage.T_LOADER]["summary"]["sha"]
    assert tpls[T_LOG]["print_image"] == "print.png" and tpls[T_LOG]["print_sha"] and not tpls[T_LOG]["print_used"]
    assert all(t["print_image"] is None and t["print_sha"] is None and not t["print_used"]
               for n, t in tpls.items() if n not in (synth_usage.T_LOADER, T_LOG))
    assert main(["info", "--site", str(site), "--work-root", str(tmp_path / "w")]) == 0
    out = capsys.readouterr().out
    assert f"인쇄 층 print.png ({layers[synth_usage.T_LOADER]['summary']['sha']})" in out
    unused = [ln for ln in out.splitlines() if "쓰지 않음" in ln]
    assert len(unused) == 1 and T_LOG in unused[0]
    # template check: 참고 한 줄, 오류는 없다. 쓰이는 층에는 참고가 없다
    assert main(["template", "check", str(haul)]) == 0
    out = capsys.readouterr().out
    assert "오류 없음" in out and out.count("참고: ") == 1 and "값 유무에 쓰지 않습니다" in out
    assert main(["template", "check", str(site / "templates" / synth_usage.T_LOADER), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["notes"] == []


# ── 함수 단위 ──────────────────────────────────────────────────────────────────────
def test_estimate_in_row_blocks_equals_the_whole_stack_and_drops_minority_ink():
    rng = np.random.default_rng(1)
    pages = [rng.integers(0, 256, (150, 90), dtype=np.uint8) for _ in range(7)]
    whole = np.percentile(np.stack([cv2.erode(p, np.ones((3, 3), np.uint8)) for p in pages]), 75, axis=0)
    assert np.array_equal(printlayer.estimate(pages), np.clip(np.rint(whole), 0, 255).astype(np.uint8))
    assert printlayer.estimate(iter(pages)).tobytes() == printlayer.estimate(pages).tobytes()
    # 모든 쪽의 선(인쇄)은 남고, 두 쪽에만 있는 획(손글씨)은 빠진다. 선은 1 px 넓어진다 (erode 3×3)
    base = np.full((60, 60), 240, np.uint8)
    base[30, 5:55] = 0
    pages = [base.copy() for _ in range(6)]
    pages[0][10:20, 10] = 0
    pages[1][10:20, 10] = 0
    layer = printlayer.estimate(pages)
    assert layer[29:32, 5:55].max() == 0 and layer[10:20, 9:12].min() == 240
    with pytest.raises(ValueError):
        printlayer.estimate([base, base[:-1]])
    with pytest.raises(ValueError):
        printlayer.estimate([])
    assert printlayer.coverage(np.ones((10, 10), bool), [(0, 0, 5, 5), (8, 8, 20, 20), (12, 12, 15, 15)]) == [1.0, 1.0, 0.0]
    assert printlayer.sha(base) != printlayer.sha(base.reshape(30, 120))         # 크기가 해시에 들어간다


def test_image_size_reads_png_header_and_other_formats(tmp_path):
    img = np.full((37, 91), 200, np.uint8)
    imwrite(tmp_path / "a.png", img)
    imwrite(tmp_path / "a.jpg", img)
    assert image_size(tmp_path / "a.png") == image_size(tmp_path / "a.jpg") == (91, 37)
    (tmp_path / "bad.png").write_bytes(b"not an image")
    with pytest.raises(ValueError):
        image_size(tmp_path / "bad.png")


def test_readonly_db_is_not_created(tmp_path):
    from minedocscan.store.db import open_db, open_db_readonly

    with pytest.raises(FileNotFoundError):
        open_db_readonly(f"sqlite:///{(tmp_path / 'none.db').as_posix()}")
    assert not (tmp_path / "none.db").exists()
    open_db(f"sqlite:///{(tmp_path / '한글 db.db').as_posix()}").close()
    con = open_db_readonly(f"sqlite:///{(tmp_path / '한글 db.db').as_posix()}")
    with pytest.raises(sqlite3.OperationalError):
        con.execute("CREATE TABLE x (a)")

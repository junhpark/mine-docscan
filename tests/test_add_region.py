"""템플릿에 표 더하기 (tasks/0006 단계 6): `template add-region`.

수용 기준 1: 분류 전용 템플릿(칸 정의 없음)에서 print-layer(쪽을 직접 정합 — 4.2) → add-region 세 번 → 괘선이 생성기와 2 px 안.
수용 기준 2: 채워진 쪽(손글씨가 괘선 가까이)에서 잡으면 틀리는 괘선이 인쇄 층에서는 맞는다 — 인쇄 층에서 잡는 이유.
그 밖: template.yaml 을 글자로 고친다 — 주석·필드·다른 키는 그대로, 같은 이름은 거절, role 의 자리표시, `regions: []`.
쪽은 usage_classify_only(세션 픽스처 — synth10 의 첫 이틀, 분류 전용 실행과 그 인쇄 층)를 같이 쓴다. 파이프라인을 더 돌리지 않는다.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

import cv2
import numpy as np
import pytest
import yaml

from minedocscan.cli import main
from minedocscan.forms.template import Template
from minedocscan.imaging.grid import detect_grid_roi
from minedocscan.imaging.io import imread_gray, imwrite
from minedocscan.tools import mktemplate, synth_usage
from minedocscan.tools.mktemplate import AddRegionError, add_region
from minedocscan.tools.printlayer import build, candidate_pages, page_image
from minedocscan.tools.tpltools import check_template

LOADER = synth_usage.T_LOADER
MARGIN = 20                    # 표 둘레의 여유 (px) — 사람이 표를 조금 넉넉히 감싸 그린 영역


def _roi(reg: dict, m: int = MARGIN) -> tuple[int, int, int, int]:
    ys, xs = reg["grid"]["ys"], reg["grid"]["xs"]
    return xs[0] - m, ys[0] - m, xs[-1] + m, ys[-1] + m


def _same(found: tuple[list[int], list[int]], reg: dict, tol: int = 2) -> bool:
    """잡은 괘선이 생성기의 괘선(grid.ys·xs — 나눔 선 split_ys 는 인쇄되지 않아 빼고)과 개수가 같고 tol px 안인가."""
    ys, xs = reg["grid"]["ys"], reg["grid"]["xs"]
    fy, fx = found
    return len(fy) == len(ys) and len(fx) == len(xs) and all(abs(a - b) <= tol for a, b in zip([*fy, *fx], [*ys, *xs],
                                                                                                 strict=True))


@pytest.fixture(scope="module")
def loader_gen() -> dict:
    """생성기의 로우더 작업일보: {표 이름: 표} (괘선의 정답)."""
    return {r["name"]: r for r in synth_usage.build_loader_log()[1]["regions"]}


# ── 수용 기준 1: 분류 전용 → print-layer → add-region 세 번 ──────────────────────────
def test_classification_only_template_to_three_tables(usage_classify_only, loader_gen, tmp_path, capsys):
    """사이트 팩의 분류 전용 템플릿(regions: [], fields: [] — 쪽은 classified_only) 그대로에서 시작한다: 그 템플릿에
    `template print-layer`(쪽을 직접 정합 — 4.2, 칸 정의가 없어도 만든다)를 돌리고 사람이 `print_image: print.png` 를 적은 뒤,
    생성기의 세 표를 조금 넉넉히 감싼 영역으로 add-region 세 번 (명령으로). 괘선(grid.ys·xs)이 생성기와 2 px 안에서 같고,
    나눔 선(split_ys)은 잡지 않는다."""
    s = usage_classify_only["settings"]
    tdir = tmp_path / LOADER
    shutil.copytree(s.site / "templates" / LOADER, tdir)
    assert Template(tdir / "template.yaml").regions == []                    # 분류 전용 (칸 정의 없음)
    assert main(["template", "print-layer", str(tdir), "--work-root", str(s.work_root), "--archive-root",
                 str(s.archive_root), "--json"]) == 0
    r = json.loads(capsys.readouterr().out)
    assert r["by_source"] == {"aligned_now": 4} and r["covered"] == []        # 덮인 칸을 잴 칸이 없다
    assert r["sha"] == usage_classify_only["layers"][LOADER]["summary"]["sha"]  # 칸 정의가 있는 템플릿에서 만든 층과 같다
    with open(tdir / "template.yaml", "a", encoding="utf-8") as f:           # 사람이 적는 키 (4.2)
        f.write("print_image: print.png\n")

    for name in ("tally", "shifts", "meter"):
        roi = ",".join(map(str, _roi(loader_gen[name])))
        assert main(["template", "add-region", str(tdir), "--roi", roi, "--name", name, "--role", name]) == 0
        out = capsys.readouterr().out
        assert "인쇄 층에서" in out and "split_ys" in out                    # 나눔 선은 사람이 넣는다고 알린다
    tpl = Template(tdir / "template.yaml", validate=False)
    assert [r["name"] for r in tpl.regions] == ["tally", "shifts", "meter"]
    for reg in tpl.regions:
        g = loader_gen[reg["name"]]
        assert _same((reg["grid"]["ys"], reg["grid"]["xs"]), g), (reg["name"], reg["grid"], g["grid"])
        assert "split_ys" not in reg["grid"] and reg["header_rows"] == 1
        assert len(reg["rows"]) == len(g["grid"]["ys"]) - 2 and len(reg["columns"]) == len(g["grid"]["xs"]) - 1
    # role 의 자리표시는 그 역할의 규칙을 채운다 — 이 셋은 열·행을 고치기 전에도 template check 를 통과한다
    assert [c["name"] for c in tpl.region("meter")["columns"]] == ["start", "end", "total"]
    assert check_template(tdir) == []


# ── 수용 기준 2: 채워진 쪽에서 잡으면 틀리는 괘선이 인쇄 층에서는 맞는다 ─────────────────────
@pytest.fixture(scope="module")
def filled_loader_pages(usage_classify_only) -> list[np.ndarray]:
    """분류 전용 실행의 로우더 쪽들을 기준 이미지에 정합한 그림 (print-layer 가 층을 만들 때와 같은 경로 — page_image 의
    aligned_now). 손글씨가 있는 채워진 쪽이다."""
    tdir = usage_classify_only["layers"][LOADER]["dir"]
    tpl = Template(tdir / "template.yaml", validate=False)
    con = sqlite3.connect(usage_classify_only["db"])
    con.row_factory = sqlite3.Row
    rows = candidate_pages(con, LOADER)
    con.close()
    out = []
    for r in rows:
        img, how = page_image(r, tpl, usage_classify_only["settings"], tpl.reference.shape)
        assert how == "aligned_now", how
        out.append(img)
    assert len(out) >= 2
    return out


def test_print_layer_gives_the_lines_a_filled_page_gets_wrong(usage_classify_only, filled_loader_pages, loader_gen, tmp_path):
    """같은 영역·같은 함수(detect_grid_roi)로: 인쇄 층에서는 세 표가 다 맞고, 채워진 쪽에서는 틀리는 표가 있다 — 계기 칸에 쓴
    값(1234.5)의 세로획이 짧은 계기 표(괘선 사이 42–46 px)에서 세로 괘선으로 섞여 나온다. 같은 쪽을 기준 이미지로 둔 템플릿
    (인쇄 층 없이)에 add-region 하면 그 틀린 괘선이 들어가고, 인쇄 층을 적으면 맞는다."""
    layer_dir = usage_classify_only["layers"][LOADER]["dir"]
    layer = imread_gray(layer_dir / "print.png")
    for name, reg in loader_gen.items():
        assert _same(detect_grid_roi(layer, _roi(reg)), reg), name
    wrong = [(i, name) for i, page in enumerate(filled_loader_pages) for name, reg in loader_gen.items()
             if not _same(detect_grid_roi(page, _roi(reg)), reg)]
    assert wrong, "채워진 쪽에서도 모든 표의 괘선이 맞았다 — 이 시험이 막는 실패가 합성에 없다"
    assert "meter" in {name for _i, name in wrong} <= {"meter", "shifts"}, wrong   # 손으로 쓴 값의 세로획 (작업량 표는 맞다)

    i = wrong[0][0]
    meter = loader_gen["meter"]
    base = tmp_path / "filled"
    shutil.copytree(layer_dir, base, ignore=shutil.ignore_patterns("print.png"))
    spec = yaml.safe_load((base / "template.yaml").read_text(encoding="utf-8"))
    spec["regions"], spec["fields"] = [], []
    (base / "template.yaml").write_text(yaml.safe_dump(spec, allow_unicode=True, sort_keys=False), encoding="utf-8")
    imwrite(base / "reference.png", filled_loader_pages[i])                 # 기준 이미지 = 채워진 스캔 (실제 현장처럼)
    with_print = tmp_path / "with_print"
    shutil.copytree(base, with_print)
    shutil.copy(layer_dir / "print.png", with_print / "print.png")
    with open(with_print / "template.yaml", "a", encoding="utf-8") as f:
        f.write("print_image: print.png\n")

    r = add_region(base, _roi(meter), "meter", role="meter")
    assert r["source"] == "reference" and any("기준 이미지에서 잡았습니다" in n for n in r["notes"])
    got = Template(base / "template.yaml", validate=False).region("meter")["grid"]
    assert not _same((got["ys"], got["xs"]), meter), got
    r = add_region(with_print, _roi(meter), "meter", role="meter")
    assert r["source"] == "print" and r["notes"] == []
    got = Template(with_print / "template.yaml", validate=False).region("meter")["grid"]
    assert _same((got["ys"], got["xs"]), meter), got


# ── template.yaml 을 글자로 고친다 ──────────────────────────────────────────────
GRID_YS, GRID_XS = [40, 80, 120, 160], [30, 130, 230, 330]          # 작은 합성 표 (머리 1 행 + 데이터 2 행, 열 3)


def _tiny(tmp_path: Path, body: str, name: str = "tiny") -> Path:
    """작은 템플릿 폴더: 괘선만 그린 기준 이미지(400×220)와 주어진 template.yaml 글자."""
    d = tmp_path / name
    d.mkdir()
    img = np.full((220, 400), 255, np.uint8)
    for y in GRID_YS:
        cv2.line(img, (GRID_XS[0], y), (GRID_XS[-1], y), 0, 2)
    for x in GRID_XS:
        cv2.line(img, (x, GRID_YS[0]), (x, GRID_YS[-1]), 0, 2)
    imwrite(d / "reference.png", img)
    (d / "template.yaml").write_text(body, encoding="utf-8")
    return d


ROI = "10,20,350,180"
HEAD = "name: tiny\nreference_image: reference.png\ndpi: 200\npage_size: [400, 220]\nhandler: usage\n"
FIELDS = ("# 필드 앞의 주석 — 필드의 것\nfields:\n- name: note\n  kind: handwritten_text\n  bbox: [10, 180, 300, 215]  # 비고 칸\n"
          "- {name: sig, kind: signature, bbox: [310, 180, 390, 215]}\n")
EXISTING = ("regions:\n# 표 앞의 주석\n- name: first\n  grid: {ys: [40, 80, 120], xs: [30, 130]}  # 손으로 고친 괘선\n"
            "  header_rows: 1\n  columns: [{idx: 0, name: a, kind: handwritten_text}]\n  rows: [{row: 0, key: r0}]\n\n")


def _load(d: Path) -> dict:
    return yaml.safe_load((d / "template.yaml").read_text(encoding="utf-8"))


def _add(d: Path, *args: str) -> int:
    return main(["template", "add-region", str(d), "--roi", ROI, *args])


def test_insert_keeps_comments_fields_and_other_keys(tmp_path, capsys):
    d = _tiny(tmp_path, "# 맨 위의 주석\n" + HEAD + EXISTING + FIELDS + "handler_options: {}  # 끝의 키\n")
    text0, spec0 = (d / "template.yaml").read_text(encoding="utf-8"), _load(d)
    assert _add(d, "--name", "second") == 0
    text1, spec1 = (d / "template.yaml").read_text(encoding="utf-8"), _load(d)
    assert {k: v for k, v in spec1.items() if k != "regions"} == {k: v for k, v in spec0.items() if k != "regions"}
    assert spec1["regions"][0] == spec0["regions"][0] and [r["name"] for r in spec1["regions"]] == ["first", "second"]
    new = spec1["regions"][1]
    assert new["grid"] == {"ys": GRID_YS, "xs": GRID_XS} and new["header_rows"] == 1 and "role" not in new
    assert new["columns"] == [{"idx": i, "name": f"col_{i}", "kind": "handwritten_text"} for i in range(3)]
    assert new["rows"] == [{"row": i, "key": f"row_{i}"} for i in range(2)]
    # 원래의 줄은 하나도 빠지지 않고 차례도 그대로 — 새 줄은 regions 블록 끝(필드 앞의 주석 위)에만 들어갔다
    old_lines, new_lines = text0.splitlines(), text1.splitlines()
    at = new_lines.index("- name: second")
    assert new_lines[:at] == old_lines[:at] and new_lines[-(len(old_lines) - at):] == old_lines[at:]
    assert old_lines[at] == "" and old_lines[at + 1].startswith("# 필드 앞의 주석") and old_lines[at + 2] == "fields:"
    out = capsys.readouterr().out
    assert "기준 이미지에서" in out and "split_ys" in out


def test_empty_regions_null_regions_and_missing_regions(tmp_path):
    """`regions: []`(줄 끝 주석은 둔다)·`regions:`(빈 값)은 그 자리의 블록이 되고, regions 가 없으면 파일 끝에 더한다."""
    for i, (body, keep) in enumerate([(HEAD + "regions: []  # 아직 없음\n" + FIELDS, "regions:  # 아직 없음"),
                                      (HEAD + "regions:\n" + FIELDS, "regions:"),
                                      (HEAD + FIELDS.rstrip("\n"), None)]):            # 끝 줄바꿈도 없다
        d = _tiny(tmp_path, body, f"t{i}")
        spec0 = _load(d)
        add_region(d, (10, 20, 350, 180), "main", role="tally")
        spec1 = _load(d)
        assert spec1["fields"] == spec0["fields"] and [r["name"] for r in spec1["regions"]] == ["main"], i
        lines = (d / "template.yaml").read_text(encoding="utf-8").splitlines()
        if keep is not None:
            assert keep in lines and lines.index(keep) < lines.index("fields:"), i
        assert Template(d / "template.yaml").regions[0]["role"] == "tally"     # 검증하며 읽어도 오류가 없다
    # CRLF 파일(현장 PC 에서 고친 템플릿)은 끼워 넣은 줄도 CRLF — 맨 \n 줄이 섞이지 않는다
    for i, body in enumerate([HEAD + "regions: []\n" + FIELDS, HEAD + EXISTING + FIELDS]):
        d = _tiny(tmp_path, "", f"crlf{i}")
        (d / "template.yaml").write_bytes(body.replace("\n", "\r\n").encode("utf-8"))
        add_region(d, (10, 20, 350, 180), "added")
        data = (d / "template.yaml").read_bytes()
        assert data.count(b"\n") == data.count(b"\r\n") and b"- name: added\r\n" in data, i
        assert [r["name"] for r in _load(d)["regions"]][-1] == "added"


def test_indented_items_keep_their_indent(tmp_path):
    body = HEAD + "regions:\n  - name: first\n    grid: {ys: [40, 80, 120], xs: [30, 130]}\n    header_rows: 1\n" \
           "    columns: [{idx: 0, name: a, kind: handwritten_text}]\n    rows: [{row: 0, key: r0}]\n" + FIELDS
    d = _tiny(tmp_path, body)
    add_region(d, (10, 20, 350, 180), "second")
    assert "  - name: second" in (d / "template.yaml").read_text(encoding="utf-8").splitlines()
    assert [r["name"] for r in _load(d)["regions"]] == ["first", "second"]


def test_refusals_are_one_line_and_leave_the_file(tmp_path, capsys):
    d = _tiny(tmp_path, HEAD + EXISTING + FIELDS)
    before = (d / "template.yaml").read_bytes()
    cases = [(["--name", "first"], "같은 이름의 표"),
             (["--name", "fields"], "'fields'"),
             (["--name", "x", "--role", "gauge"], "알 수 없는 --role"),
             (["--name", "x", "--header-rows", "3"], "괘선이 모자랍니다")]
    for args, want in cases:
        with pytest.raises(SystemExit) as e:
            _add(d, *args)
        assert want in str(e.value.code) and "\n" not in str(e.value.code), (args, e.value.code)
    for roi in ("10,20,500,180", "350,20,10,180", "1,2,3"):
        with pytest.raises(SystemExit) as e:
            main(["template", "add-region", str(d), "--roi", roi, "--name", "x"])
        assert "--roi" in str(e.value.code) and "\n" not in str(e.value.code), roi
    assert (d / "template.yaml").read_bytes() == before
    assert capsys.readouterr().out == ""
    # print_image 가 가리키는 인쇄 층이 없거나 기준 이미지와 크기가 다르면 (좌표계가 다르다) 잡지 않는다
    with open(d / "template.yaml", "a", encoding="utf-8") as f:
        f.write("print_image: print.png\n")
    before = (d / "template.yaml").read_bytes()
    for img in (None, np.full((110, 200), 255, np.uint8)):
        if img is not None:
            imwrite(d / "print.png", img)
        with pytest.raises(SystemExit) as e:
            _add(d, "--name", "x")
        assert "인쇄 층" in str(e.value.code) and "\n" not in str(e.value.code), e.value.code
    assert (d / "template.yaml").read_bytes() == before
    # 흐름 형식의 regions 는 글자로 끼워 넣지 않는다
    flow = _tiny(tmp_path, HEAD + "regions: [{name: first, grid: {ys: [40, 80], xs: [30, 130]}, columns: [], rows: []}]\n"
                 + FIELDS, "flow")
    before = (flow / "template.yaml").read_bytes()
    with pytest.raises(AddRegionError, match="흐름 형식"):
        add_region(flow, (10, 20, 350, 180), "second")
    assert (flow / "template.yaml").read_bytes() == before
    # 목록이 아닌 regions (스칼라·매핑)도 한 줄로
    for i, bad in enumerate(("regions: 5\n", "regions: {a: 1}\n")):
        t = _tiny(tmp_path, HEAD + bad + FIELDS, f"bad{i}")
        before = (t / "template.yaml").read_bytes()
        with pytest.raises(SystemExit) as e:
            _add(t, "--name", "x")
        assert "regions 의 모양" in str(e.value.code) and "\n" not in str(e.value.code), e.value.code
        assert (t / "template.yaml").read_bytes() == before
    # git 작업 트리 안: --allow-in-repo 없이는 거절, 있으면 고친다 (합성 팩)
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    t = _tiny(repo, HEAD + "regions: []\n" + FIELDS)
    before = (t / "template.yaml").read_bytes()
    with pytest.raises(SystemExit) as e:
        _add(t, "--name", "x")
    assert "git 작업 트리" in str(e.value.code) and (t / "template.yaml").read_bytes() == before
    assert _add(t, "--name", "x", "--allow-in-repo") == 0 and [r["name"] for r in _load(t)["regions"]] == ["x"]
    capsys.readouterr()
    # 저장소 안의 템플릿은 읽기 전에 거절한다 (현장 양식이다)
    inside = Path(__file__).parent / "add-region-should-not-exist"
    with pytest.raises(SystemExit) as e:
        main(["template", "add-region", str(inside), "--roi", ROI, "--name", "x"])
    assert "git 작업 트리" in str(e.value.code) and "\n" not in str(e.value.code) and not inside.exists()


def test_failed_verification_restores_the_file(tmp_path, monkeypatch):
    """다시 읽어 표가 하나 늘고 나머지가 그대로가 아니면 원래 파일로 되돌린다."""
    d = _tiny(tmp_path, "# 주석\n" + HEAD + EXISTING + FIELDS)
    before = (d / "template.yaml").read_bytes()
    real = mktemplate._insert_region
    for broken in (lambda text, region: text.replace("name: note", "name: other"),          # 표가 늘지 않았다
                   lambda text, region: real(text, region).replace("name: note", "name: other"),  # 표는 늘었는데 필드가 바뀌었다
                   lambda text, region: real(text, region).replace("dpi: 200", "dpi: 300")):      # 표는 늘었는데 다른 키가
        monkeypatch.setattr(mktemplate, "_insert_region", broken)
        with pytest.raises(AddRegionError, match="되돌렸습니다"):
            add_region(d, (10, 20, 350, 180), "second")
        assert (d / "template.yaml").read_bytes() == before
    monkeypatch.setattr(mktemplate, "_insert_region", real)
    # 쓰다 실패하면(디스크 …) 원래 파일이 그대로고 임시 파일도 남지 않는다
    def fail(src, dst):
        raise OSError("disk full")
    monkeypatch.setattr(mktemplate.os, "replace", fail)
    with pytest.raises(OSError):
        add_region(d, (10, 20, 350, 180), "second")
    assert (d / "template.yaml").read_bytes() == before and sorted(p.name for p in d.iterdir()) == ["reference.png",
                                                                                                  "template.yaml"]


def test_role_placeholders(tmp_path, capsys):
    """--role 의 자리표시는 그 역할의 규칙(forms/template._role_problems)을 채운다 — 열이 셋인 표에서 meter·shifts·tally·
    activities 모두 template check 의 오류가 없다. 열이 하나인 세로 계기 표는 행 키 start·end·total. handler 가 usage 가
    아니면 알린다."""
    for role in ("meter", "shifts", "tally", "activities"):
        d = _tiny(tmp_path, HEAD + "regions: []\n" + FIELDS, role)
        r = add_region(d, (10, 20, 350, 180), "t", role=role)
        assert r["source"] == "reference" and not any("usage 핸들러" in n for n in r["notes"])
        reg = Template(d / "template.yaml", validate=False).region("t")
        cols = [(c["name"], c["kind"], c.get("format")) for c in reg["columns"]]
        assert reg["role"] == role
        assert cols == {"meter": [("start", "handwritten_number", "reading"), ("end", "handwritten_number", "reading"),
                                  ("total", "handwritten_number", "reading")],
                        "shifts": [("shift", "printed", None), ("col_1", "handwritten_text", None),
                                   ("range", "handwritten_number", "time_range")],
                        "tally": [(f"col_{i}", "handwritten_number", "integer") for i in range(3)],
                        "activities": [(f"col_{i}", "handwritten_text", None) for i in range(3)]}[role], role
        assert check_template(d) == [], role
    # 세로 계기 표: 열 하나, 행 셋
    reg = mktemplate.region_skeleton("m", [10, 20, 30, 40, 50], [5, 60], role="meter")
    assert [r["key"] for r in reg["rows"]] == ["start", "end", "total"] and reg["columns"][0]["format"] == "reading"
    # handler 가 usage 가 아니면 알린다 (쓰기는 한다 — template check 가 오류로 낸다)
    d = _tiny(tmp_path, HEAD.replace("handler: usage", "handler: generic") + "regions: []\n" + FIELDS, "generic")
    assert _add(d, "--name", "t", "--role", "shifts") == 0
    assert "usage 핸들러의 표에만" in capsys.readouterr().out
    assert any("role 은 usage 핸들러의 표에만" in e for e in check_template(d))
    # 계열의 동시 판이면 다른 판에도 같은 표를 더하라고 알린다 (4.6 — 키가 다르면 사이트 팩이 읽히지 않는다)
    d = _tiny(tmp_path, HEAD + "family: tiny\nconcurrent: true\nregions: []\n" + FIELDS, "variant")
    assert any("동시 판" in n for n in add_region(d, (10, 20, 350, 180), "t", role="tally")["notes"])
    assert not any("동시 판" in n for n in add_region(_tiny(tmp_path, HEAD + "regions: []\n" + FIELDS, "single"),
                                                    (10, 20, 350, 180), "t", role="tally")["notes"])


# ── 계기 표의 자리표시와 쪽이 적은 인쇄 층의 잔상: 합성 운행일보의 계기 표 ─────────────────────────
def test_usage_log_meter_placeholders_and_few_page_layer_residue(synth10, usage_classify_only, tmp_path, capsys):
    """운행일보의 계기 표는 첫 열이 인쇄된 이름 칸이고 시작·종료·총이 뒤의 세 열이다 — 자리표시도 뒤의 세 열에 붙고(앞 열은
    printed), 요약에 그 자리를 적는다 (template check 는 자리를 보지 않는다).
    같은 영역에서: 빈 양식, 10일치로 만든 층, 쪽이 적은 층(3·4장 — print-layer 가 5장 미만이라 경고하는 층)이 다 생성기의 괘선이다.
    백분위를 보간하던 때는 4장의 층에 같은 자리에 쓴 계기 값의 잔상이 남아 시작 칸 안의 세로 괘선으로 잡혔다 (보간 없이(higher)
    3·4장의 75 백분위는 가장 밝은 쪽 — 모든 쪽에 있는 것만 남는다)."""
    from minedocscan.tools.printlayer import WARN_PAGES

    img, spec = synth_usage.build_usage_log()
    meter = next(r for r in spec["regions"] if r["name"] == "meter")
    ten = synth10.site / "templates" / synth_usage.T_USAGE / "print.png"
    assert _same(detect_grid_roi(img, _roi(meter)), meter)
    assert _same(detect_grid_roi(imread_gray(ten), _roi(meter)), meter)
    s = usage_classify_only["settings"]
    for k in (3, 4):
        few = tmp_path / f"few{k}"
        shutil.copytree(usage_classify_only["layers"][synth_usage.T_USAGE]["dir"], few, ignore=shutil.ignore_patterns("print.png"))
        r = build(few, s, max_pages=k)
        assert r["pages"] == k < WARN_PAGES and r["warnings"]
        assert _same(detect_grid_roi(imread_gray(few / "print.png"), _roi(meter)), meter), k

    d = tmp_path / synth_usage.T_USAGE
    d.mkdir()
    imwrite(d / "reference.png", img)
    shutil.copy(ten, d / "print.png")
    head = {k: spec[k] for k in ("name", "title", "reference_image", "dpi", "page_size", "handler")}
    (d / "template.yaml").write_text(yaml.safe_dump(head, allow_unicode=True, sort_keys=False)
                                     + "print_image: print.png\nregions: []\nfields: []\n", encoding="utf-8")
    assert main(["template", "add-region", str(d), "--roi", ",".join(map(str, _roi(meter))), "--name", "meter",
                 "--role", "meter"]) == 0
    out = capsys.readouterr().out
    reg = Template(d / "template.yaml", validate=False).region("meter")
    assert _same((reg["grid"]["ys"], reg["grid"]["xs"]), meter)
    got = [(c["idx"], c["name"], c["kind"], c.get("format")) for c in reg["columns"]]
    assert got[0] == (0, "col_0", "printed", None)
    assert got[1:] == [(c["idx"], c["name"], c["kind"], c["format"]) for c in meter["columns"]]    # 생성기의 열 그대로
    assert "열 1 start handwritten_number/reading" in out and "잔상" in out
    assert check_template(d) == []


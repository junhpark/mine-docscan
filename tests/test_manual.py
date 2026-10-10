"""사용 설명서 (tasks/0009 4.8): 부록 명령.md 가 지금의 명령과 같다, 설정 부록이 설정 키를 다 적는다, 링크·그림이 있고 그림은 3 MB 안,
manual.html 이 그림을 담은 한 파일이다, 그리고 (slow) 설명서의 명령 보기가 합성 묶음에서 실제로 돈다.

명령 보기를 돌리는 규칙 — 설명서의 코드 블록에서 `minedocscan` 으로 시작하는 줄을 차례로 돌린다 (종료 코드 0 이어야 한다):
- 코드 블록 바로 앞에 `<!-- 시험: 돌리지 않음 -->` 이 있으면 그 블록은 돌리지 않는다 (오래 도는 serve, 실데이터·통합 DB 가 있어야 하는 것,
  사람이 YAML 을 고친 뒤의 명령 …). 읽는 사람에게는 보이지 않는다.
- 줄 끝의 `# …` 는 주석이다. `<…>` 자리표시는 PLACEHOLDERS 의 것만 쓴다 (모르는 자리표시는 실패 — 설명서가 바뀌면 여기도).
- 합성 묶음: synth --intake (넷째 날까지) + 가동 일보·인쇄 층·표시 이름·V2 양식, 설정 파일(MINEDOCSCAN_CONFIG)에 사이트 팩·보관·작업·접수·
  엑셀 폴더. 돌리기 전에 watch --once 한 바퀴 — 그래서 날짜를 정할 문서가 있다.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MANUAL = ROOT / "docs" / "manual"
SCRIPTS = ROOT / "scripts"
SKIP_MARK = "<!-- 시험: 돌리지 않음 -->"
IMG_LIMIT = 3 * 2**20
NEW_ROI = "90,410,1564,1030"            # 합성 유류일지(빈 양식)의 표 둘레 — 7장의 add-region


def chapters() -> list[Path]:
    return sorted(p for p in MANUAL.glob("*.md"))


def _script(name: str):
    import importlib.util

    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_command_appendix_is_the_current_parser():
    cli_reference = _script("cli_reference")
    assert (MANUAL / "명령.md").read_text(encoding="utf-8") == cli_reference.render(), \
        "docs/manual/명령.md 가 지금의 명령과 다르다 — python scripts/cli_reference.py"


def example_keys() -> set[str]:
    """예시 설정 파일의 [표] 와 "[표] 키" — 주석으로 둔 것까지."""
    keys, table = set(), ""
    for line in (ROOT / "config" / "minedocscan.example.toml").read_text(encoding="utf-8").splitlines():
        m = re.match(r"#?\s*\[([a-z_.]+)\]", line)
        if m:
            table = m.group(1)
            keys.add(f"[{table}]")
            continue
        m = re.match(r'#?\s*"?([a-z_.]+)"?\s*=', line)
        if m and table:
            keys.add(f"[{table}] {m.group(1)}")
    return keys


def read_keys() -> set[str]:
    """config.py 가 읽는 키 — 틀렸을 때 오류에 적는 "[표] 키"."""
    config_py = (ROOT / "src" / "minedocscan" / "config.py").read_text(encoding="utf-8")
    return {f"[{t}] {k}" for t, k in re.findall(r'"\[([a-z_.]+)\] ([a-z_]+)', config_py)}


def test_the_example_config_has_every_key_config_py_reads():
    assert sorted(read_keys() - example_keys()) == []


def test_the_settings_appendix_names_every_key_and_variable():
    text = (MANUAL / "설정.md").read_text(encoding="utf-8")
    missing = []
    for key in sorted(example_keys() | read_keys()):
        table, _, name = key.partition(" ")
        if table not in text or (name and f"`{name}`" not in text):
            missing.append(key)
    config_py = (ROOT / "src" / "minedocscan" / "config.py").read_text(encoding="utf-8")
    missing += [v for v in sorted(set(re.findall(r"\bMINEDOCSCAN_[A-Z_]+\b", config_py))) if v not in text]
    assert missing == []


def test_links_and_images_exist_and_the_images_are_small():
    bad = []
    for p in chapters():
        for target in re.findall(r"\]\(([^)\s]+)\)", p.read_text(encoding="utf-8")):
            path = target.split("#", 1)[0]
            if not path or re.match(r"[a-z]+:", path):
                continue
            if not (p.parent / path).exists():
                bad.append(f"{p.name}: {target}")
    assert bad == []
    images = [p for p in (MANUAL / "img").rglob("*") if p.is_file()]
    assert images and sum(p.stat().st_size for p in images) <= IMG_LIMIT


def test_every_chapter_is_in_the_contents():
    manual_html = _script("manual_html")
    listed = {p.name for p in manual_html.chapters(MANUAL)}
    assert listed == {p.name for p in chapters()}


def test_manual_html_is_one_file_with_the_pictures(tmp_path):
    pytest.importorskip("markdown")
    manual_html = _script("manual_html")
    out = tmp_path / "manual.html"
    assert manual_html.main(["--out", str(out)]) == 0
    text = out.read_text(encoding="utf-8")
    assert "data:image/png;base64," in text
    assert not re.search(r'src="(?!data:)', text)                       # 바깥 그림이 없다
    for m in re.finditer(r'href="#([^"]+)"', text):                     # 안의 링크는 다 자리가 있다
        assert f'id="{m.group(1)}"' in text, m.group(1)
    assert manual_html.render() == text                                 # 같은 설명서면 같은 바이트


# ── 명령 보기를 돌린다 ──────────────────────────────────────────────────────
def command_lines() -> list[tuple[str, int, str]]:
    """(장, 줄 번호, 명령 줄) — 표시가 없는 코드 블록의 minedocscan 줄."""
    out = []
    for p in chapters():
        if p.name == "명령.md":
            continue
        lines = p.read_text(encoding="utf-8").splitlines()
        i = 0
        while i < len(lines):
            if lines[i].lstrip().startswith("```"):
                prev = next((x.strip() for x in reversed(lines[:i]) if x.strip()), "")
                skip = prev == SKIP_MARK
                j = i + 1
                while j < len(lines) and not lines[j].lstrip().startswith("```"):
                    s = lines[j].strip()
                    if not skip and re.match(r"minedocscan(\s|$)", s):
                        out.append((p.name, j + 1, s))
                    j += 1
                i = j + 1
            else:
                i += 1
    return out


def test_command_lines_use_known_placeholders():
    unknown = [(c, n, m) for c, n, line in command_lines() for m in re.findall(r"<[^<>]+>", line) if m not in PLACEHOLDER_NAMES]
    assert unknown == []


PLACEHOLDER_NAMES = {"<문서 ID>", "<쪽 ID>", "<날짜>", "<달>", "<사이트 팩>", "<보관 폴더>", "<작업 폴더>", "<접수 폴더>", "<엑셀 폴더>",
                     "<출력 폴더>", "<양식>", "<새 양식>", "<기준 이미지>", "<검수자>", "<표 둘레>"}


@pytest.fixture(scope="module")
def demo(tmp_path_factory):
    from minedocscan.imaging.io import imwrite
    from minedocscan.tools.synth import generate
    from minedocscan.tools.synth_v2 import build_fuel_log

    root = tmp_path_factory.mktemp("manual")
    syn = generate(root / "합성", days=4, seed=0, intake=True, usage_logs=True, print_layers=True, display_names=True, v2_forms=True)
    folders = {"archive_root": root / "보관", "work_root": root / "작업", "excel_dir": root / "엑셀"}
    for p in folders.values():
        p.mkdir()
    cfg = root / "minedocscan.toml"
    cfg.write_text(
        "[paths]\n" + "".join(f"{k} = {json.dumps(str(v))}\n" for k, v in (
            ("site", syn.site), ("archive_root", folders["archive_root"]), ("work_root", folders["work_root"]),
            ("reviews", root / "기록" / "reviews.jsonl"), ("inbox", syn.root / "inbox")))
        + f"\n[export]\nexcel_dir = {json.dumps(str(folders['excel_dir']))}\n\n[intake]\nsettle_seconds = 0\ngive_up_seconds = 0\n",
        encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith("MINEDOCSCAN_")}
    env.update(MINEDOCSCAN_CONFIG=str(cfg), PYTHONIOENCODING="utf-8")
    run = lambda *a: subprocess.run([sys.executable, "-m", "minedocscan.cli", *a], env=env, cwd=root, capture_output=True,   # noqa: E731
                                    text=True, encoding="utf-8", errors="replace", timeout=600)
    r = run("watch", "--once")
    assert r.returncode == 0, r.stderr
    import sqlite3

    con = sqlite3.connect(folders["work_root"] / "minedocscan.db")
    doc = con.execute("SELECT document_id FROM doc_document WHERE status = 'needs_date'").fetchone()[0]
    page, day = con.execute("SELECT page_id, work_date FROM doc_page WHERE status = 'loaded' ORDER BY page_id").fetchone()
    con.close()
    blank, _gen = build_fuel_log()
    imwrite(root / "빈-유류일지.png", blank)
    outs = iter(range(1000))
    values = {"<문서 ID>": doc, "<쪽 ID>": page, "<날짜>": day, "<달>": day[:7], "<사이트 팩>": str(syn.site),
              "<보관 폴더>": str(folders["archive_root"]), "<작업 폴더>": str(folders["work_root"]), "<접수 폴더>": str(syn.root / "inbox"),
              "<엑셀 폴더>": str(folders["excel_dir"]), "<양식>": "fuel_log", "<새 양식>": "fuel_log_draft",
              "<기준 이미지>": str(root / "빈-유류일지.png"), "<검수자>": "demo", "<표 둘레>": NEW_ROI}
    return {"root": root, "run": run, "values": values, "outs": outs}


@pytest.mark.slow
def test_the_command_examples_run_on_the_synthetic_demo(demo):
    failed = []
    for chapter, n, line in command_lines():
        text = line
        for k, v in demo["values"].items():
            text = text.replace(k, shlex.quote(v) if k != "<표 둘레>" else v)
        while "<출력 폴더>" in text:
            text = text.replace("<출력 폴더>", shlex.quote(str(demo["root"] / f"출력-{next(demo['outs'])}")), 1)
        args = shlex.split(text, comments=True)
        r = demo["run"](*args[1:])
        if r.returncode != 0:
            failed.append(f"{chapter}:{n} [{r.returncode}] {line} — {(r.stderr or r.stdout).strip().splitlines()[-1:]}")
    assert failed == []

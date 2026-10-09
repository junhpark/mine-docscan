"""설명서의 갈무리 (tasks/0009 4.8) — **합성 데모로만**. 합성 접수 폴더를 한 바퀴 처리하고 운영 화면(serve — 감시 켠 채)을 띄워 Playwright 로 찍는다.

    pip install -e ".[docs]"          # markdown, playwright (런타임·묶음에는 들어가지 않는다)
    python scripts/manual_shots.py [--out docs/manual/img] [--chromium /path/to/chrome]

찍는 것: 홈, 날짜를 정할 문서, 다시 스캔 의심 쪽, 검수 대기열 둘(운반 숫자·가동 시간 칸), 새 양식(유류일지)의 template preview.
실데이터 화면은 어디에도 찍지 않는다 (CLAUDE.md) — 이 도구는 합성 묶음을 스스로 만들고 그 밖의 폴더를 읽지 않는다.
환경 변수 MINEDOCSCAN_* 는 지우고 띄운다 (이 PC 의 설정·통합 DB 를 쓰지 않는다). 그림은 PNG, 뷰포트만 (전체 3 MB 안 — 시험이 잰다).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "manual" / "img"
VIEW = {"width": 1280, "height": 820}
REVIEWER = "demo"


def _cli(*args: str, env: dict) -> str:
    r = subprocess.run([sys.executable, "-m", "minedocscan.cli", *args], env=env, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise SystemExit(f"minedocscan {args[0]} 실패 ({r.returncode}): {r.stderr.strip().splitlines()[-1:] or ''}")
    return r.stdout


def _toml(p: Path) -> str:
    return json.dumps(str(p))


def demo(root: Path, env: dict) -> Path:
    """합성 접수 폴더(넷째 날까지, 가동 일보·인쇄 층·표시 이름) → 한 바퀴. 설정 파일을 돌려준다."""
    from minedocscan.tools.synth import generate

    syn = generate(root / "합성", days=4, seed=0, intake=True, usage_logs=True, print_layers=True, display_names=True)
    folders = {k: root / n for k, n in (("archive_root", "보관"), ("work_root", "작업"), ("excel_dir", "엑셀"))}
    for p in folders.values():
        p.mkdir(parents=True)
    cfg = root / "minedocscan.toml"
    cfg.write_text(
        "[paths]\n" + "".join(f"{k} = {_toml(v)}\n" for k, v in (
            ("site", syn.site), ("archive_root", folders["archive_root"]), ("work_root", folders["work_root"]),
            ("reviews", root / "기록" / "reviews.jsonl"), ("inbox", syn.root / "inbox")))
        + f"\n[export]\nexcel_dir = {_toml(folders['excel_dir'])}\n\n[intake]\nsettle_seconds = 0\ngive_up_seconds = 0\n",
        encoding="utf-8")
    _cli("watch", "--once", "--config", str(cfg), env=env)
    return cfg


def serve(cfg: Path, logs: Path, env: dict) -> tuple[subprocess.Popen, int]:
    proc = subprocess.Popen([sys.executable, "-m", "minedocscan.cli", "serve", "--config", str(cfg), "--reviewer", REVIEWER,
                             "--port", "0", "--log-dir", str(logs)], env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        for p in sorted(logs.glob("serve-*.log")) if logs.is_dir() else []:
            m = re.search(r"http://127\.0\.0\.1:(\d+)/", p.read_text(encoding="utf-8", errors="replace"))
            if m:
                return proc, int(m.group(1))
        if proc.poll() is not None:
            break
        time.sleep(0.2)
    proc.kill()
    raise SystemExit("serve 가 뜨지 않았다")


def _json(url: str) -> dict:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def shots(base: str, out: Path, chromium: str | None) -> list[Path]:
    from playwright.sync_api import sync_playwright

    todo = _json(base + "/api/home")["todo"]
    needs = next(iter(todo["needs_date"]), None)
    dup = next(iter(todo["duplicates"]), None)
    made = []
    with sync_playwright() as pw:
        b = pw.chromium.launch(**({"executable_path": chromium} if chromium else {}))
        page = b.new_page(viewport=VIEW, device_scale_factor=1, locale="ko-KR")
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))

        def snap(name: str, path: str, wait: str | None = None) -> None:
            page.goto(base + path, wait_until="networkidle")
            if wait:
                page.wait_for_selector(wait, timeout=15000)
            page.wait_for_timeout(300)
            p = out / f"{name}.png"
            page.screenshot(path=str(p))
            made.append(p)

        snap("home", "/", "#view *")
        if needs:
            snap("doc-needs-date", f"/doc?id={needs['document_id']}", "#view *")
        if dup:
            snap("doc-rescan", f"/doc?id={dup['document_id']}#p{dup.get('page_no', 1)}", "#view *")
        snap("queue-haul-numbers", "/?name=haul-numbers", "input.val")
        snap("queue-readings", "/?name=readings", "input")
        b.close()
    if errors:
        raise SystemExit(f"화면의 스크립트 오류 {len(errors)}개 — 첫째: {errors[0][:200]}")
    return made


def preview(root: Path, out: Path, env: dict) -> Path:
    """7장(새 양식 더하기): 합성 유류일지의 template preview — 칸·필드·형식을 그린 그림, 폭 900 으로."""
    import cv2
    import numpy as np

    from minedocscan.imaging.io import imwrite
    from minedocscan.tools.synth import generate

    syn = generate(root / "v2", days=1, seed=0, v2_forms=True)
    work = root / "v2-작업"
    _cli("template", "preview", str(syn.site / "templates" / "fuel_log"), "--work-root", str(work), env=env)
    src = sorted((work / "template-preview").glob("*.png"))[0]
    img = cv2.imdecode(np.frombuffer(src.read_bytes(), np.uint8), cv2.IMREAD_COLOR)   # 색 그대로 (한글 경로 — imread 를 쓰지 않는다)
    h, w = img.shape[:2]
    small = cv2.resize(img, (900, round(h * 900 / w)), interpolation=cv2.INTER_AREA)
    p = out / "template-preview.png"
    imwrite(p, small)
    return p


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, default=OUT)
    ap.add_argument("--chromium", help="Chromium 실행 파일 (Playwright 가 받아 둔 것이 없을 때)")
    ap.add_argument("--keep", action="store_true", help="합성 데모 폴더를 남긴다 (자리를 찍는다)")
    a = ap.parse_args(argv)
    env = {k: v for k, v in os.environ.items() if not k.startswith("MINEDOCSCAN_")}
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    sys.path.insert(0, str(ROOT / "src"))
    root = Path(tempfile.mkdtemp(prefix="minedocscan-shots-"))
    a.out.mkdir(parents=True, exist_ok=True)
    try:
        cfg = demo(root, env)
        proc, port = serve(cfg, root / "logs", env)
        try:
            made = shots(f"http://127.0.0.1:{port}", a.out, a.chromium)
        finally:
            proc.terminate()
            proc.wait(15)
        made.append(preview(root, a.out, env))
    finally:
        if a.keep:
            print(f"데모 폴더: {root}")
        else:
            shutil.rmtree(root, ignore_errors=True)
    total = sum(p.stat().st_size for p in made)
    for p in made:
        print(f"{p.relative_to(ROOT) if ROOT in p.parents else p}  {p.stat().st_size:,} B")
    print(f"그림 {len(made)}장, {total:,} 바이트")
    return 0


if __name__ == "__main__":
    sys.exit(main())

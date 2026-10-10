"""다시 스캔 서명의 민감도를 잰다 (tasks/0010 4.3 가) — 지금의 서명과 후보를 한 번에. 수만 찍는다 (값·그림·이름·경로 없이).

  python scripts/sig_probe.py --synthetic [--out probe.json] [--days 3]
      합성 다시 스캔 묶음(synth --rescans — tests/test_rescans.py 와 같은 쌍)을 임시 폴더에서 처리하고, 그 정합 그림으로
      ① 같은 그림을 옮기고 돌린 것과의 유사도 ② 합성 다시 스캔(같은 종이)의 유사도 ③ 같은 날·같은 계열의 다른 종이의 유사도.
      요약을 CI 의 알림(::notice — scripts/ci_notice.py)으로 찍는다 (slow·windows 작업).
  python scripts/sig_probe.py --site … --work-root … [--archive-root …] [--out probe.json]
      있는 작업 DB 의 정합 그림으로 ①과 ③ (실데이터 — 사람이 돌린다, tasks/0010 8절 2). 알림은 찍지 않는다.

옮기는 법 (①): 정합한 회색 그림에 cv2.warpAffine (INTER_LINEAR, borderValue=255) — 옮김 (dx, dy) = (1, 0)·(2, 1)·(3, 2) px, 그림의
가운데를 축으로 0.2° 돌림, (2, 1) px + 0.2° 를 같이. 서명은 같은 템플릿의 마스크로.
기준 (4.3 나 — 합성, 리눅스와 윈도우 둘 다): (2, 1) px·0.2°·둘을 같이, 이 셋마다 5 % ≥ 0.93·최소 ≥ 0.85. 합성 다시 스캔 전부 ≥ 0.95.
같은 날 다른 종이의 최대가 판 1 보다 0.02 넘게 오르지 않는다. 쪽 하나의 서명 시간과 글자열 길이가 판 1 의 2배를 넘지 않는다.
"""
from __future__ import annotations

import argparse
import json
import platform
import sys
import tempfile
import time
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from ci_notice import notice  # noqa: E402

from minedocscan.imaging import signature as sigs  # noqa: E402
from minedocscan.imaging.grid import binarize  # noqa: E402

CASES = {"s10": (1, 0, 0.0), "s21": (2, 1, 0.0), "s32": (3, 2, 0.0), "r02": (0, 0, 0.2), "s21r02": (2, 1, 0.2)}
JUDGED = ("s21", "r02", "s21r02")                   # 4.3 나의 기준이 걸리는 셋
P5_MIN, MIN_MIN, RESCAN_MIN, OTHER_RISE, COST = 0.93, 0.85, 0.95, 0.02, 2.0


# ── 후보 ───────────────────────────────────────────────────────────────────
# 판 1 (0007 4.6 — 이 지시서 전의 서명)은 여기에 얼려 둔다: 인쇄(인쇄 층이 있으면 그 마스크, 없으면 기준 이미지를 인쇄 층으로 본 것)와
# 표 밖 필드의 칸을 넓히지 않고 지운다. 후보는 그 마스크(base)에서 시작한다. 지금의 서명(sigs.signature, 판 sigs.VERSION)은 따로 잰다.
def _counts(b: np.ndarray) -> np.ndarray:
    b = cv2.morphologyEx(b, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
    h, w = b.shape
    hb, wb = h // sigs.BLOCK, w // sigs.BLOCK
    c = (b[:hb * sigs.BLOCK, :wb * sigs.BLOCK] > 0).reshape(hb, sigs.BLOCK, wb, sigs.BLOCK).sum(axis=(1, 3))
    return np.minimum(c, 255).astype(np.uint8)


_MASKS: dict = {}


def base_mask(tpl) -> np.ndarray:
    """판 1 의 마스크 — 넓히기 전 (Template.signature_mask 의 tasks/0010 전 모습)."""
    key = (tpl.name, 0)
    if key not in _MASKS:
        from minedocscan.imaging import printlayer

        m = (tpl.print_mask if tpl.print_path is not None else printlayer.mask(tpl.reference)).copy()
        h, w = m.shape
        for f in tpl.spec.get("fields") or []:
            x0, y0, x1, y1 = (int(v) for v in f["bbox"])
            m[max(0, y0):min(h, y1), max(0, x0):min(w, x1)] = True
        _MASKS[key] = m
    return _MASKS[key]


def _dilated(tpl, k: int) -> np.ndarray:
    key = (tpl.name, k)
    if key not in _MASKS:
        _MASKS[key] = cv2.dilate(base_mask(tpl).astype(np.uint8), np.ones((k, k), np.uint8)).astype(bool)
    return _MASKS[key]


def _no_lines(b: np.ndarray, n: int) -> np.ndarray:
    lines = cv2.morphologyEx(b, cv2.MORPH_OPEN, np.ones((1, n), np.uint8)) | \
        cv2.morphologyEx(b, cv2.MORPH_OPEN, np.ones((n, 1), np.uint8))
    return cv2.subtract(b, lines)


def cand_v1(b, tpl):
    b = b.copy()
    b[base_mask(tpl)] = 0
    return _counts(b)


def cand_dilate(k):
    def f(b, tpl):
        b = b.copy()
        b[_dilated(tpl, k)] = 0
        return _counts(b)
    return f


def cand_lines(n, k=None):
    def f(b, tpl):
        b = _no_lines(b, n)
        b[_dilated(tpl, k) if k else base_mask(tpl)] = 0
        return _counts(b)
    return f


def cand_production(b, tpl):                         # 지금의 서명 (sigs.signature, 템플릿의 넓힌 마스크)
    return sigs.signature(None, tpl.signature_mask, binary=b)


# 1절 다의 둘(넓힌 마스크 7 × 7, 긴 선 41 px), 둘을 같이, 넓히는 폭의 이웃(5·9 — 9 × 9 가 판 2), 그리고 지금의 서명
CANDIDATES = {"v1": cand_v1, "dilate5": cand_dilate(5), "dilate7": cand_dilate(7), "dilate9": cand_dilate(9),
              "lines41": cand_lines(41), "dilate7_lines41": cand_lines(41, 7)}
if sigs.VERSION != 1:
    CANDIDATES[f"v{sigs.VERSION}"] = cand_production


# ── 그림 ───────────────────────────────────────────────────────────────────
def moved(gray: np.ndarray, dx: float, dy: float, deg: float) -> np.ndarray:
    h, w = gray.shape
    m = cv2.getRotationMatrix2D((w / 2.0, h / 2.0), deg, 1.0)
    m[0, 2] += dx
    m[1, 2] += dy
    return cv2.warpAffine(gray, m, (w, h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=255)


def stats(v: list[float]) -> dict:
    if not v:
        return {"n": 0, "median": None, "p5": None, "min": None, "max": None}
    a = np.asarray(v, np.float64)
    return {"n": len(v), "median": round(float(np.median(a)), 4), "p5": round(float(np.percentile(a, 5)), 4),
            "min": round(float(a.min()), 4), "max": round(float(a.max()), 4)}


def _sim(a, b) -> float:
    s = sigs.similarity(a, b)
    return 0.0 if s is None else s


def measure(pages: list[dict], rescans: list[tuple[int, int]], others: list[tuple[int, int]]) -> dict:
    """pages: [{"gray", "tpl"}]. rescans·others: pages 의 번호 쌍. 돌려주는 값: 후보마다 {경우: 분포, rescan, other, ms, chars}."""
    out = {}
    bins = [binarize(p["gray"]) for p in pages]
    for name, fn in CANDIDATES.items():
        t0 = time.perf_counter()
        base = [fn(b, p["tpl"]) for b, p in zip(bins, pages, strict=True)]
        ms = (time.perf_counter() - t0) * 1000 / max(1, len(pages))
        r = {"ms": round(ms, 2), "chars": int(np.mean([len(sigs.encode(s)) for s in base])) if base else 0}
        for case, (dx, dy, deg) in CASES.items():
            r[case] = stats([_sim(base[i], fn(binarize(moved(p["gray"], dx, dy, deg)), p["tpl"]))
                             for i, p in enumerate(pages) if p.get("original", True)])
        r["rescan"] = stats([_sim(base[i], base[j]) for i, j in rescans])
        r["other"] = stats([_sim(base[i], base[j]) for i, j in others])
        out[name] = r
    v1 = out["v1"]
    for r in out.values():
        r["ok"] = bool(all(r[c]["n"] and r[c]["p5"] >= P5_MIN and r[c]["min"] >= MIN_MIN for c in JUDGED)
                       and (not r["rescan"]["n"] or r["rescan"]["min"] >= RESCAN_MIN)
                       and (not r["other"]["n"] or r["other"]["max"] <= v1["other"]["max"] + OTHER_RISE)
                       and r["ms"] <= COST * v1["ms"] + 1.0 and r["chars"] <= COST * v1["chars"])
    return out


# ── 합성 ───────────────────────────────────────────────────────────────────
def synthetic(days: int) -> tuple[list[dict], list, list]:
    from minedocscan.config import Settings
    from minedocscan.forms.sitepack import SitePack
    from minedocscan.imaging.io import imread_gray
    from minedocscan.pipeline import Pipeline
    from minedocscan.tools.synth import generate

    tmp = Path(tempfile.mkdtemp(prefix="minedocscan-sigprobe-"))
    g = generate(tmp / "data", days=days, seed=0, rescans=True)
    st = Settings(site=g.site, archive_root=g.scans, work_root=tmp / "work", reviews=tmp / "rv" / "reviews.jsonl",
                  save_aligned=True, recognizer="null")
    site = SitePack(g.site)
    pipe = Pipeline(st, site=site)
    pipe.run([g.scans])
    rows = pipe.con.execute("SELECT d.source_name, p.page_no, p.template_name, p.work_date, p.aligned_image, p.status "
                            "FROM doc_page p JOIN doc_document d ON p.document_id = d.document_id "
                            "WHERE p.aligned_image IS NOT NULL AND p.status IN ('loaded', 'duplicate')").fetchall()
    pages, index = [], {}
    for r in sorted(rows, key=lambda r: (r["source_name"], r["page_no"])):
        tpl = site.templates[r["template_name"]]
        index[(r["source_name"], r["page_no"])] = len(pages)
        pages.append({"gray": imread_gray(st.work_root / r["aligned_image"]), "tpl": tpl, "day": r["work_date"],
                      "family": tpl.sig_family, "original": not r["source_name"].endswith("_rescan")})
    first = sorted(p.stem for p in g.scans.glob("*.pdf") if not p.stem.endswith("_rescan"))[0]
    rescans = []
    for t in g.truth["rescans"]:
        src, no = t["of"].split("#")
        a, b = index.get((src, int(no))), index.get((f"{first}_rescan", t["page"]))
        if a is not None and b is not None:
            rescans.append((a, b))
    same = {frozenset(x) for x in rescans}
    others = [(i, j) for i in range(len(pages)) for j in range(i + 1, len(pages))
              if pages[i]["original"] and pages[j]["original"] and pages[i]["day"] == pages[j]["day"]
              and pages[i]["family"] == pages[j]["family"] and frozenset((i, j)) not in same]
    pipe.con.close()
    return pages, rescans, others


# ── 있는 작업 DB ─────────────────────────────────────────────────────────────
def from_work_db(a) -> tuple[list[dict], list, list]:
    from minedocscan.config import load_settings
    from minedocscan.forms.sitepack import SitePack
    from minedocscan.store.db import open_db_readonly
    from minedocscan.tools.printlayer import page_image

    st = load_settings(a.config, site=a.site, archive_root=a.archive_root, work_root=a.work_root)
    site = SitePack(st.site)
    con = open_db_readonly(st.resolved_db_url)
    rows = con.execute("SELECT p.*, d.source_path, d.source_rel FROM doc_page p JOIN doc_document d "
                       "ON p.document_id = d.document_id WHERE p.status = 'loaded' AND p.template_name IS NOT NULL").fetchall()
    pages = []
    for r in rows:
        tpl = site.templates.get(r["template_name"])
        if tpl is None or not tpl.has_cells:
            continue
        img, _how = page_image(r, tpl, st, tpl.reference.shape)
        if img is not None:
            pages.append({"gray": img, "tpl": tpl, "day": r["work_date"], "family": tpl.sig_family, "original": True})
    others = [(i, j) for i in range(len(pages)) for j in range(i + 1, len(pages))
              if pages[i]["day"] == pages[j]["day"] and pages[i]["family"] == pages[j]["family"]]
    return pages, [], others


def lines(res: dict, head: dict) -> list:
    out = [head]
    for name, r in res.items():
        row = [("cand", name), ("ok", r["ok"])]
        for c in CASES:
            row += [(f"{c}_p5", r[c]["p5"]), (f"{c}_min", r[c]["min"])]
        row += [("rescan_min", r["rescan"]["min"]), ("other_max", r["other"]["max"]), ("ms", r["ms"]), ("chars", r["chars"])]
        out.append(row)
    return out


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):                 # 윈도우 러너의 표준 출력은 cp1252 — 한글 오류 글을 찍다 죽지 않게
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--synthetic", action="store_true")
    ap.add_argument("--days", type=int, default=3)
    ap.add_argument("--config")
    ap.add_argument("--site")
    ap.add_argument("--archive-root")
    ap.add_argument("--work-root")
    ap.add_argument("--out", type=Path)
    a = ap.parse_args(argv)
    if a.synthetic:
        pages, rescans, others = synthetic(a.days)
    elif a.work_root or a.site or a.config:
        pages, rescans, others = from_work_db(a)
    else:
        ap.error("--synthetic 또는 --site/--work-root")
    res = measure(pages, rescans, others)
    head = {"platform": sys.platform, "opencv": cv2.__version__, "python": platform.python_version(),
            "pages": sum(p["original"] for p in pages), "rescans": len(rescans), "other_pairs": len(others),
            "production": f"v{sigs.VERSION}"}
    doc = {"head": head, "criteria": {"judged": list(JUDGED), "p5_min": P5_MIN, "min_min": MIN_MIN, "rescan_min": RESCAN_MIN,
                                      "other_rise": OTHER_RISE, "cost": COST}, "candidates": res}
    if a.out:
        a.out.parent.mkdir(parents=True, exist_ok=True)
        a.out.write_text(json.dumps(doc, indent=1), encoding="utf-8")
    for name, r in res.items():
        print(f"{name:16s} ok={int(r['ok'])} " + " ".join(f"{c}={r[c]['p5']}/{r[c]['min']}" for c in CASES)
              + f" rescan_min={r['rescan']['min']} other_max={r['other']['max']} ms={r['ms']} chars={r['chars']}")
    if a.synthetic:
        print(notice(f"sig_probe {sys.platform}", *lines(res, {k: str(v).replace(" ", "") for k, v in head.items()})))
    return 0


if __name__ == "__main__":
    sys.exit(main())

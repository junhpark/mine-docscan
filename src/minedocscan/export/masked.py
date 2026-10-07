"""가린 쪽 그림 (tasks/0008 4.9): 정합 그림(템플릿 좌표) 위에서 **템플릿이 아는 자리만** 한 색으로 채운다 — 흐리게 하지 않는다.

가리는 것:
  signature  kind: signature 인 표 밖 필드와 표의 칸 (표의 칸은 괘선까지)
  meta       표 밖 필드 중 meta_key 가 가릴 목록에 든 것 (기본 operator·vehicle_no, site.toml [redact] meta_keys)
  redact     템플릿의 redact 상자 (결재란, 인쇄된 이름·등록번호 열 — 판마다 따로)
  text       글자 칸 전부: 표의 handwritten_text 열(칸은 괘선까지 — cells(inset=0))과 표 밖의 글자 필드. --keep-text 면 남긴다
             (그때는 meta_key 가 없는 이름 필드도 남는다)
표 밖 필드와 redact 상자는 pad_px 만큼 넓힌다 (글씨가 상자를 넘는다). 수 칸·✓ 칸·인쇄는 남는다.

**템플릿이 아는 자리만 가린다** — 표 위에 걸쳐 쓴 메모, 수 칸에 적은 이름, 템플릿에 적지 않은 인쇄는 남는다. 내보낸 그림은 사람이 보고
나서 쓴다. 내보낸 폴더도 현장 데이터다 (저장소 밖에 — 명령이 저장소 안을 거절한다). 작업 DB 는 읽기만 한다.
"""
from __future__ import annotations

import sqlite3
from collections import Counter
from pathlib import Path

import numpy as np

from ..forms.template import Template
from ..imaging.io import imwrite

DEFAULT_META_KEYS = ("operator", "vehicle_no")
# 표 밖 필드·redact 상자를 넓히는 폭 (px, 200 dpi 템플릿 좌표). 합성 세 묶음(기본·메타 필드·가동 일보, 사흘씩)의 표 밖 필드 206개에서
# 그 칸의 글씨(기준 이미지에 없는 잉크의 연결 성분 중 절반 넘게 상자 안인 것)가 상자를 넘은 거리: 최대 11 px(작성자), 99 % 9 px,
# 95 % 5 px. 16 px(약 2 mm)로 둔다. 실제 글씨는 더 넘을 수 있다 — 현장의 값은 site.toml [redact] pad_px (8절 5).
DEFAULT_PAD_PX = 16
FILL = 0                         # 채우는 색 (검정 — 가린 자리가 가린 것으로 보이게)
KINDS = ("signature", "meta", "redact", "text")


def page_boxes(tpl: Template, meta_keys, pad: int, keep_text: bool = False) -> list[tuple[str, tuple[int, int, int, int]]]:
    """(종류, 상자) — 템플릿 좌표. 한 필드는 한 번만 센다 (서명 > 메타 > 글자)."""
    out = []
    keys = set(meta_keys)
    for f in tpl.fields:
        x0, y0, x1, y1 = (int(v) for v in f["bbox"])
        box = (x0 - pad, y0 - pad, x1 + pad, y1 + pad)
        if f.get("kind") == "signature":
            out.append(("signature", box))
        elif f.get("meta_key") in keys:
            out.append(("meta", box))
        elif f.get("kind") == "handwritten_text" and not keep_text:
            out.append(("text", box))
    for r in tpl.redact:
        x0, y0, x1, y1 = (int(v) for v in r["bbox"])
        out.append(("redact", (x0 - pad, y0 - pad, x1 + pad, y1 + pad)))
    cells = tpl.cells(inset=0)
    out += [("signature", c.bbox) for c in cells if c.kind == "signature"]      # 표 안의 서명 열 (괘선까지)
    if not keep_text:
        out += [("text", c.bbox) for c in cells if c.kind == "handwritten_text"]
    return out


def mask(gray: np.ndarray, boxes) -> np.ndarray:
    """상자들을 FILL 로 채운 사본 (쪽 밖은 잘라서)."""
    out = gray.copy()
    h, w = out.shape[:2]
    for _kind, (x0, y0, x1, y1) in boxes:
        x0, y0, x1, y1 = max(0, x0), max(0, y0), min(w, x1), min(h, y1)
        if x1 > x0 and y1 > y0:
            out[y0:y1, x0:x1] = FILL
    return out


def export_masked(con: sqlite3.Connection, site, settings, out: Path, date: str | None = None,
                  page_ids: list[str] | None = None, keep_text: bool = False) -> dict:
    """적재된 쪽만 OUT/<쪽 ID>.png 로 (원래 파일명을 쓰지 않는다). 정합 그림이 없으면 원본을 호모그래피로 다시 편다
    (tools/printlayer.page_image — 파이프라인의 정합 그림과 같은 함수). 돌려주는 값: 수만 (이름·값·파일명 없이)."""
    from ..tools.printlayer import page_image

    cfg = getattr(site, "redact", None) or {}
    meta_keys = cfg.get("meta_keys") if cfg.get("meta_keys") is not None else DEFAULT_META_KEYS
    pad = cfg.get("pad_px") if cfg.get("pad_px") is not None else DEFAULT_PAD_PX
    sql = ("SELECT p.page_id, p.document_id, p.page_no, p.status, p.work_date, p.template_name, p.aligned_image, p.homography, "
           "p.render_dpi, d.source_path, d.source_rel FROM doc_page p JOIN doc_document d ON p.document_id = d.document_id ")
    if page_ids:
        rows = {r["page_id"]: r for r in con.execute(sql + f"WHERE p.page_id IN ({','.join('?' * len(page_ids))})", page_ids)}
        missing = len(set(page_ids) - set(rows))
        rows = [rows[p] for p in dict.fromkeys(page_ids) if p in rows]
    else:
        rows = con.execute(sql + "WHERE p.work_date = ? ORDER BY p.page_id", (date,)).fetchall()
        missing = 0
    boxes, skipped = Counter(), Counter()
    if missing:
        skipped["missing"] = missing
    written = 0
    out.mkdir(parents=True, exist_ok=True)
    for r in rows:
        tpl = site.templates.get(r["template_name"]) if r["template_name"] else None
        if r["status"] != "loaded":
            skipped["not_loaded"] += 1
            continue
        if tpl is None:
            skipped["no_template"] += 1
            continue
        img, how = page_image(r, tpl, settings, tpl.reference.shape)
        if img is None:
            skipped[how] += 1
            continue
        bx = page_boxes(tpl, meta_keys, pad, keep_text)
        imwrite(out / f"{r['page_id']}.png", mask(img, bx))
        boxes.update(k for k, _b in bx)
        written += 1
    return {"pages": written, "boxes": {k: boxes.get(k, 0) for k in KINDS}, "skipped": dict(skipped), "pad_px": pad,
            "keep_text": keep_text, "meta_keys": len(meta_keys)}


def format_summary(r: dict, out: Path) -> str:
    b, sk = r["boxes"], r["skipped"]
    reasons = {"not_loaded": "적재되지 않은 쪽", "missing": "없는 쪽 ID", "no_template": "템플릿이 없는 쪽", "no_source": "원본에 닿지 않는 쪽",
               "no_homography": "호모그래피가 없는 쪽", "unreadable": "원본을 읽지 못한 쪽"}
    lines = [f"가린 쪽 {r['pages']}장 → {out} (파일 이름은 쪽 ID)",
             f"  가린 상자: 서명 {b['signature']}, 메타 필드 {b['meta']}, redact {b['redact']}, 글자 칸 {b['text']} "
             f"(표 밖 필드·redact 는 {r['pad_px']} px 넓혀서)"]
    if sk:
        lines.append("  내지 않은 쪽: " + ", ".join(f"{reasons.get(k, k)} {n}" for k, n in sorted(sk.items())))
    if r["keep_text"]:
        lines.append("  --keep-text: 글자 칸을 남겼습니다 — meta_key 가 없는 이름 필드도 남습니다")
    lines.append("템플릿이 아는 자리만 가렸습니다 — 표 위에 걸쳐 쓴 메모, 수 칸에 적은 이름, 템플릿에 적지 않은 인쇄는 남습니다. "
                 "쓰기 전에 사람이 한 장씩 봅니다. 이 폴더도 현장 데이터입니다 (저장소 밖에).")
    return "\n".join(lines)

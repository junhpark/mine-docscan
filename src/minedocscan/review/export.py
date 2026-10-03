"""검수값을 인식기에 넘길 형태로 내보낸다: 셀 이미지 + 라벨 (tasks/0002 단계 6).

  OUT/<split>/<kind>/<field_id>.png      셀 크롭 (기본: 원본 해상도, 템플릿 좌표의 1.5배 = 300 dpi 원본 그대로)
  OUT/<split>/labels.jsonl               field_id, 값, 판정, 양식, 열, 행 키, 날짜, 해상도(source|aligned), 검수자

illegible 은 뺀다. empty 는 빈 칸의 예로 넣는다. test 와 train 을 섞지 않는다 (날짜 분할, ADR 0009).
크롭과 라벨에는 현장의 글씨가 들어 있다. 대상이 git 작업 트리 안이면 거절한다 (--allow-in-repo 로만).
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from ..imaging.io import imwrite
from .crops import _pad, crop_region, field_info
from .store import effective_with_split


class ExportError(RuntimeError):
    pass


def inside_git_tree(path: str | Path) -> bool:
    p = Path(path).resolve()
    return any((q / ".git").exists() for q in (p, *p.parents))


def export_crops(con: sqlite3.Connection, site, settings, out: str | Path, split: str = "all", kind: str | None = None,
                 res: str = "auto", out_scale: float = 1.5, pad: int | None = None, allow_in_repo: bool = False) -> dict:
    """pad: 셀 둘레 여유(템플릿 px). None 이면 화면과 같이 행 높이의 절반(최소 8) — 칸 선을 넘은 획이 잘리지 않게.
    돌려주는 값: {"written": n, "by_split": {split: n}, "by_source": {source|aligned: n}, "skipped_illegible": n}."""
    out = Path(out)
    if inside_git_tree(out) and not allow_in_repo:
        raise ExportError(f"{out} 은 git 작업 트리 안입니다. 크롭에는 현장의 글씨가 들어 있으므로 저장소 밖에 내보내세요 "
                          "(정말 필요하면 --allow-in-repo)")
    if split not in ("all", "test", "train"):
        raise ValueError(f"split 은 all | test | train: {split}")
    written = 0
    by_split: dict[str, int] = {}
    by_source: dict[str, int] = {}
    skipped = 0
    handles: dict[str, object] = {}
    try:
        for rv, sp, d in sorted(effective_with_split(con, site), key=lambda t: (t[1], t[2] or "", t[0].field_id)):
            if rv.verdict == "illegible":
                skipped += 1
                continue
            if split != "all" and sp != split:
                continue
            r = field_info(con, rv.field_id)
            if r is None or (kind and r["kind"] != kind):
                continue
            pd = _pad(r, pad)
            img, src = crop_region(settings, r, (r["x0"] - pd, r["y0"] - pd, r["x1"] + pd, r["y1"] + pd), out_scale, res)
            rel = Path(sp) / r["kind"] / f"{rv.field_id}.png"
            imwrite(out / rel, img)
            if sp not in handles:
                (out / sp).mkdir(parents=True, exist_ok=True)
                handles[sp] = open(out / sp / "labels.jsonl", "w", encoding="utf-8")   # noqa: SIM115
            handles[sp].write(json.dumps({
                "field_id": rv.field_id, "file": rel.as_posix(), "text": rv.value if rv.verdict == "value" else "",
                "verdict": rv.verdict, "template": r["template_name"], "region": r["region"], "field_name": r["field_name"],
                "row_key": r["row_key"], "kind": r["kind"], "work_date": d, "split": sp, "resolution": src,
                "out_scale": out_scale, "pad": pd, "bbox": [r["x0"], r["y0"], r["x1"], r["y1"]],
                "reviewer": rv.reviewer, "reviewed_at": rv.reviewed_at}, ensure_ascii=False) + "\n")
            written += 1
            by_split[sp] = by_split.get(sp, 0) + 1
            by_source[src] = by_source.get(src, 0) + 1
    finally:
        for h in handles.values():
            h.close()
    return {"out": str(out), "written": written, "by_split": by_split, "by_source": by_source, "skipped_illegible": skipped}

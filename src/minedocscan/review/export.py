"""검수값을 인식기에 넘길 형태로 내보낸다: 셀 이미지 + 라벨 (tasks/0002 단계 6).

  OUT/<split>/<kind>/<이름>.png          셀 크롭 (기본: 원본 해상도, 템플릿 좌표의 1.5배 = 300 dpi 원본 그대로).
                                         이름은 field_id 에서 파일 이름에 못 쓰는 글자(: 등)를 바꾼 것 — 읽는 쪽은
                                         labels.jsonl 의 file 만 본다 (이름에서 field_id 를 되살리지 않는다)
  OUT/<split>/labels.jsonl               field_id, 값, 판정, 양식, 열, 행 키, 날짜, 규격(spec: res·scale·pad), 검수자

크롭은 파이프라인이 인식기에 넘기는 것과 같은 구현으로 뜬다(imaging/cropspec.py) — 같은 셀이면 화소까지 같다.

메타 필드 (--meta, tasks/0004 단계 2): meta_key 가 있는 자유 필드(차량번호·작성자·날짜의 월·일)의 크롭과 정답.
  OUT/<split>/meta/<키>/<이름>.png, OUT/<split>/meta/labels.jsonl (file 은 OUT 기준 경로 — 셀의 labels.jsonl 과 섞이지 않는다)
  정답 = 그 쪽 메타의 **사람·파일명** 값(검수 > 라벨 > 파일명, doc_page_meta). 기계가 읽은 값은 정답이 아니다. 줄마다 출처(label_source).
  값이 없는 쪽은 내보내지 않는다. illegible 로 검수된 필드는 --include-illegible 일 때만 (verdict illegible).
  규격의 여유는 고정 8 px — 필드는 표의 칸이 아니라 "행 높이의 절반"이 맞지 않는다 (60 px 높이의 필드에 30 px).

illegible 은 기본으로 뺀다 (--include-illegible 이면 넣는다: 숫자 인식기의 "거절"로 학습한다 — labels.jsonl 의 verdict 로 구분).
empty 는 빈 칸의 예로 넣는다. test 와 train 을 섞지 않는다 (날짜 분할, ADR 0009).
크롭과 라벨에는 현장의 글씨가 들어 있다. 대상이 git 작업 트리 안이면 거절한다 (--allow-in-repo 로만).
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path

from ..imaging.io import imwrite
from ..pagemeta import HUMAN_SOURCES  # 사람의 출처 목록은 한 곳 (tasks/0007 4.2)
from .crops import field_info, spec_crop
from .store import effective_with_split


class ExportError(RuntimeError):
    pass


_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
MAX_SCALE = 6.0
META_PAD = 8                 # 메타 필드 크롭의 여유(템플릿 px, 고정) — tasks/0004 9절 기본값


def safe_name(field_id: str) -> str:
    """field_id → 어느 OS 에서나 쓸 수 있는 파일 이름 (확장자 없음). Windows 가 받지 않는 < > : " / \\ | ? * 를 바꾼다."""
    return _UNSAFE.sub(".", field_id).rstrip(". ")


def check_spec_args(out_scale: float, pad: int | None) -> None:
    """배율은 0 초과 MAX_SCALE 이하, 여유는 0 이상. 어긋나면 아무것도 쓰기 전에 멈춘다."""
    if not (out_scale > 0 and out_scale <= MAX_SCALE):
        raise ValueError(f"--scale 은 0 초과 {MAX_SCALE:g} 이하: {out_scale}")
    if pad is not None and pad < 0:
        raise ValueError(f"--pad 는 0 이상: {pad}")


def inside_git_tree(path: str | Path) -> bool:
    p = Path(path).resolve()
    return any((q / ".git").exists() for q in (p, *p.parents))


def inside_intake_folders(out: str | Path, settings, what: str = "내보낸 파일") -> str | None:
    """접수 폴더·보관 폴더 안이면 거절하는 까닭 한 줄 (아니면 None). 엑셀·가린 그림·크롭·틀린 칸 모아 보기가 같이 쓴다
    (tasks/0009 4.1 마 — 그 전에는 크롭·모아 보기가 저장소 안만 보았다)."""
    from ..intake.inbox import _inside, _norm

    for key, why in (("inbox", f"접수 폴더 안입니다 — {what}을(를) 스캔으로 접수하게 됩니다"),
                     ("archive_root", "보관 폴더(스캔 원본) 안입니다 — 보관 폴더에는 intake/ 아래에만 씁니다")):
        other = getattr(settings, key, None) if settings is not None else None
        if other is not None and (_norm(out) == _norm(other) or _inside(_norm(out), _norm(other))):
            return f"{out} 은 {why}"
    return None


def export_crops(con: sqlite3.Connection, site, settings, out: str | Path, split: str = "all", kind: str | None = None,
                 res: str = "auto", out_scale: float = 1.5, pad: int | None = None, allow_in_repo: bool = False,
                 include_illegible: bool = False) -> dict:
    """pad: 셀 둘레 여유(템플릿 px). None 이면 화면과 같이 행 높이의 절반(최소 8) — 칸 선을 넘은 획이 잘리지 않게.
    돌려주는 값: {"written": n, "by_split": {split: n}, "by_source": {source|aligned: n}, "skipped_illegible": n}."""
    out = Path(out)
    check_spec_args(out_scale, pad)
    if inside_git_tree(out) and not allow_in_repo:
        raise ExportError(f"{out} 은 git 작업 트리 안입니다. 크롭에는 현장의 글씨가 들어 있으므로 저장소 밖에 내보내세요 "
                          "(정말 필요하면 --allow-in-repo)")
    if why := inside_intake_folders(out, settings, "크롭"):
        raise ExportError(why)
    if split not in ("all", "test", "train"):
        raise ValueError(f"split 은 all | test | train: {split}")
    written = 0
    by_split: dict[str, int] = {}
    by_source: dict[str, int] = {}
    skipped = 0
    handles: dict[str, object] = {}
    used: set[str] = set()
    try:
        for rv, sp, d in sorted(effective_with_split(con, site), key=lambda t: (t[1], t[2] or "", t[0].field_id)):
            if rv.verdict == "illegible" and not include_illegible:
                skipped += 1
                continue
            if split != "all" and sp != split:
                continue
            r = field_info(con, rv.field_id)
            if r is None or (kind and r["kind"] != kind):
                continue
            img, spec = spec_crop(settings, r, res, out_scale, pad)
            src, pd = spec.res, spec.pad_for((r["x0"], r["y0"], r["x1"], r["y1"]))
            name = safe_name(rv.field_id)
            if (sp, r["kind"], name) in used:                 # 바꾼 글자 때문에 겹치면 field_id 의 해시를 붙인다
                name += "-" + hashlib.sha256(rv.field_id.encode()).hexdigest()[:8]
            used.add((sp, r["kind"], name))
            rel = Path(sp) / r["kind"] / f"{name}.png"
            imwrite(out / rel, img)
            if sp not in handles:
                (out / sp).mkdir(parents=True, exist_ok=True)
                handles[sp] = open(out / sp / "labels.jsonl", "w", encoding="utf-8")   # noqa: SIM115
            handles[sp].write(json.dumps({
                "field_id": rv.field_id, "file": rel.as_posix(), "text": rv.value if rv.verdict == "value" else "",
                "verdict": rv.verdict, "template": r["template_name"], "region": r["region"], "field_name": r["field_name"],
                "row_key": r["row_key"], "kind": r["kind"], "format": r["format"], "work_date": d, "split": sp,
                "spec": spec.to_dict(),
                # 잉크 판정이 값 있음 → 인식기에 가는 칸. 읽지 않는 형식(소수·시각)의 칸은 기계의 값 유무 그대로 (tasks/0005)
                "inked": ((r["backend"] or "") != "ink") if r["format"] in (None, "integer") else r["has_value_raw"] == 1,
                "resolution": src, "out_scale": out_scale, "pad": pd, "bbox": [r["x0"], r["y0"], r["x1"], r["y1"]],
                "reviewer": rv.reviewer, "reviewed_at": rv.reviewed_at}, ensure_ascii=False) + "\n")
            written += 1
            by_split[sp] = by_split.get(sp, 0) + 1
            by_source[src] = by_source.get(src, 0) + 1
    finally:
        for h in handles.values():
            h.close()
    return {"out": str(out), "written": written, "by_split": by_split, "by_source": by_source, "skipped_illegible": skipped}


def export_meta_crops(con: sqlite3.Connection, site, settings, out: str | Path, split: str = "all",
                      meta_key: str | None = None, res: str = "auto", out_scale: float = 1.5, pad: int | None = META_PAD,
                      allow_in_repo: bool = False, include_illegible: bool = False) -> dict:
    """메타 필드의 크롭 + 정답(사람·파일명 값). 돌려주는 값: {"written", "by_split", "by_key", "by_label_source",
    "by_source", "skipped_illegible"}. 줄 수 = PNG 수 = 사람·파일명 값이 있는 (쪽, 키) 수 (illegible 로 검수된 것 빼고)."""
    from .store import effective

    out = Path(out)
    pad = META_PAD if pad is None else pad
    check_spec_args(out_scale, pad)
    if inside_git_tree(out) and not allow_in_repo:
        raise ExportError(f"{out} 은 git 작업 트리 안입니다. 크롭에는 현장의 글씨(이름·차량번호)가 들어 있으므로 저장소 밖에 "
                          "내보내세요 (정말 필요하면 --allow-in-repo)")
    if why := inside_intake_folders(out, settings, "크롭"):
        raise ExportError(why)
    if split not in ("all", "test", "train"):
        raise ValueError(f"split 은 all | test | train: {split}")
    rows = con.execute(
        "SELECT m.page_id, m.meta_key, m.value, m.source, m.field_id, p.work_date FROM doc_page_meta m "
        "JOIN doc_page p ON m.page_id = p.page_id WHERE m.field_id IS NOT NULL AND m.value IS NOT NULL "
        f"AND m.source IN ({','.join('?' * len(HUMAN_SOURCES))})" + (" AND m.meta_key = ?" if meta_key else "")
        + " ORDER BY p.work_date, m.page_id, m.meta_key", (*HUMAN_SOURCES, *([meta_key] if meta_key else []))).fetchall()
    reviews = effective(con, field_ids=[r["field_id"] for r in rows])
    written = skipped = 0
    by_split: dict[str, int] = {}
    by_key: dict[str, int] = {}
    by_label: dict[str, int] = {}
    by_source: dict[str, int] = {}
    handles: dict[str, object] = {}
    try:
        for r in rows:
            sp = site.split_of(r["work_date"])
            if split != "all" and sp != split:
                continue
            rv = reviews.get(r["field_id"])
            verdict = "value"
            if rv is not None and rv.verdict == "illegible":
                if not include_illegible:
                    skipped += 1
                    continue
                verdict = "illegible"
            f = field_info(con, r["field_id"])
            if f is None:
                continue
            img, spec = spec_crop(settings, f, res, out_scale, pad)
            rel = Path(sp) / "meta" / safe_name(r["meta_key"]) / f"{safe_name(r['field_id'])}.png"
            imwrite(out / rel, img)
            if sp not in handles:
                (out / sp / "meta").mkdir(parents=True, exist_ok=True)
                handles[sp] = open(out / sp / "meta" / "labels.jsonl", "w", encoding="utf-8")   # noqa: SIM115
            handles[sp].write(json.dumps({
                "field_id": r["field_id"], "file": rel.as_posix(), "meta_key": r["meta_key"],
                "text": r["value"] if verdict == "value" else "", "verdict": verdict, "label_source": r["source"],
                "template": f["template_name"], "region": f["region"], "field_name": f["field_name"], "kind": f["kind"],
                "format": f["format"], "work_date": r["work_date"], "split": sp, "spec": spec.to_dict(),
                "inked": (f["ink"] or 0) > 0,
                "resolution": spec.res, "out_scale": out_scale, "pad": pad, "bbox": [f["x0"], f["y0"], f["x1"], f["y1"]],
                "reviewer": rv.reviewer if rv is not None else None,
                "reviewed_at": rv.reviewed_at if rv is not None else None}, ensure_ascii=False) + "\n")
            written += 1
            by_split[sp] = by_split.get(sp, 0) + 1
            by_key[r["meta_key"]] = by_key.get(r["meta_key"], 0) + 1
            by_label[r["source"]] = by_label.get(r["source"], 0) + 1
            by_source[spec.res] = by_source.get(spec.res, 0) + 1
    finally:
        for h in handles.values():
            h.close()
    return {"out": str(out), "written": written, "by_split": by_split, "by_key": by_key, "by_label_source": by_label,
            "by_source": by_source, "skipped_illegible": skipped}

"""검수 화면용 크롭. 요청이 올 때 잘라 낸다. 따로 저장하지 않는다.

좌표는 전부 템플릿 좌표계(doc_field 의 bbox)다. 원본에 닿으면(아카이브가 연결된 컴퓨터) 쪽의 호모그래피로 원본을
높은 해상도로 렌더링해 그 셀만 정합하고(source), 아니면 200 dpi 정합 이미지에서 자른다(aligned).
어느 쪽을 썼는지 같이 돌려준다 (tasks/0002 4.5). 잘라 내는 구현은 파이프라인·내보내기와 같은 것이다
(imaging/cropspec.py — tasks/0003 4.1). 읽기는 imaging/io.py 를 쓴다 (한글 경로).
"""
from __future__ import annotations

import json
import sqlite3
from functools import lru_cache
from pathlib import Path

import cv2
import numpy as np

from ..imaging.cropspec import CropSpec, CropUnavailable, PageImages, auto_pad, crop_box, crop_cell
from ..imaging.io import imread_gray, resolve_source


class CropError(RuntimeError):
    pass


def field_info(con: sqlite3.Connection, field_id: str) -> sqlite3.Row | None:
    return con.execute(
        "SELECT f.*, p.aligned_image, p.template_name, p.page_no, p.work_date, p.homography, p.render_dpi, "
        "d.source_path, d.source_rel FROM doc_field f JOIN doc_page p ON f.page_id = p.page_id "
        "JOIN doc_document d ON p.document_id = d.document_id WHERE f.field_id = ?", (field_id,)).fetchone()


@lru_cache(maxsize=8)
def _load(path: str, mtime: float) -> np.ndarray:        # mtime 은 파일이 바뀌면 캐시가 무효가 되게 하는 키
    return imread_gray(path)


def aligned_image(settings, row: sqlite3.Row) -> np.ndarray:
    rel = row["aligned_image"]
    if not rel:
        raise CropError(f"정합 이미지가 저장되지 않은 쪽입니다: {row['page_id']} — 설정 [pipeline] save_aligned = true 로 "
                        "파이프라인을 다시 돌리세요")
    path = Path(settings.work_root) / rel
    if not path.exists():
        raise CropError(f"정합 이미지가 없습니다: {path} — WORK_ROOT 가 다르거나 지워졌습니다. "
                        "`minedocscan run` 으로 다시 만드세요 (검수 기록은 파일에 있으므로 잃지 않습니다)")
    return _load(str(path), path.stat().st_mtime)


def page_images(settings, r: sqlite3.Row) -> PageImages:
    """DB 의 쪽 행(field_info)에서 그 쪽의 그림. 파이프라인이 만든 PageImages 와 같은 그림이 된다:
    정합 이미지는 그때 저장한 PNG(무손실), 원본은 같은 파일·같은 쪽·같은 호모그래피(JSON 은 float 를 그대로 돌려준다)."""
    src = resolve_source(r["source_path"], r["source_rel"], settings.archive_root)
    return PageImages(aligned_loader=lambda: aligned_image(settings, r), source=src, page_no=r["page_no"],
                      homography=json.loads(r["homography"]) if r["homography"] else None,
                      render_dpi=r["render_dpi"] or settings.dpi, source_dpi=settings.source_dpi,
                      damaged=settings.damaged_pdf,
                      source_missing=f"원본에 닿을 수 없습니다: {r['source_path']} (archive_root 와 source_rel 을 확인하세요)")


def _with_fallback(settings, r: sqlite3.Row, res: str, make) -> tuple[np.ndarray, str]:
    """res: auto(원본이 닿으면 원본, 아니면 정합 이미지) | source | aligned. make(images, res) → 배열.
    돌려주는 값: (회색조 이미지, 실제로 쓴 res)."""
    if res not in ("auto", "source", "aligned"):
        raise ValueError(f"res 는 auto | source | aligned: {res}")
    images = page_images(settings, r)
    if res in ("auto", "source") and images.has_source:
        try:
            return make(images, "source"), "source"
        except (OSError, ValueError, KeyError, RuntimeError) as e:
            if res == "source":
                raise CropError(f"원본에서 뜰 수 없습니다: {type(e).__name__}: {e}") from e
    elif res == "source":
        raise CropError(f"원본에 닿을 수 없습니다: {r['source_path']} (archive_root 와 source_rel 을 확인하세요)")
    try:
        return make(images, "aligned"), "aligned"
    except CropUnavailable as e:
        raise CropError(str(e)) from e


def crop_region(settings, r: sqlite3.Row, bbox: tuple[int, int, int, int], out_scale: float = 1.0,
                res: str = "auto") -> tuple[np.ndarray, str]:
    """템플릿 좌표의 상자 bbox 를 out_scale 배 크기로. 돌려주는 값: (회색조 이미지, "source" 또는 "aligned")."""
    return _with_fallback(settings, r, res, lambda im, rs: crop_box(im, bbox, out_scale, rs))


def spec_crop(settings, r: sqlite3.Row, spec_res: str, scale: float, pad: int | None) -> tuple[np.ndarray, CropSpec]:
    """셀 하나를 규격대로 (spec_res 는 auto | source | aligned). 돌려주는 값: (배열, 실제로 쓴 규격).
    파이프라인이 인식기에 넘기는 배열과 화소까지 같다 (imaging/cropspec.crop_cell)."""
    bbox = (r["x0"], r["y0"], r["x1"], r["y1"])
    img, used = _with_fallback(settings, r, spec_res,
                               lambda im, rs: crop_cell(im, bbox, CropSpec(rs, scale, pad)))
    return img, CropSpec(used, scale, pad)


def _png(img: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".png", img)
    if not ok:
        raise CropError("PNG 인코딩 실패")
    return buf.tobytes()


def _pad(r: sqlite3.Row, pad: int | None) -> int:
    """여유 폭. 실제 양식은 행 높이가 27 px 쯤이라 고정 6 px 로는 칸 선을 넘은 획이 잘린다 → 행 높이의 절반, 최소 8 px."""
    return pad if pad is not None else auto_pad((r["x0"], r["y0"], r["x1"], r["y1"]))


def cell_crop(con: sqlite3.Connection, settings, field_id: str, pad: int | None = None, scale: float = 3,
              res: str = "auto") -> tuple[bytes, str]:
    """셀 하나(PNG). pad 만큼 여유를 두고 scale 배로 키운다 (한두 자리 숫자를 크게 보기 위해). 둘째 값은 출처."""
    r = field_info(con, field_id)
    if r is None:
        raise KeyError(field_id)
    img, spec = spec_crop(settings, r, res, scale, pad)
    return _png(img), spec.res


def cell_png(con: sqlite3.Connection, settings, field_id: str, pad: int | None = None, scale: int = 3) -> bytes:
    return cell_crop(con, settings, field_id, pad, scale)[0]


def row_crop(con: sqlite3.Connection, settings, field_id: str, pad: int | None = None,
             res: str = "auto", box: bool = True) -> tuple[bytes, str]:
    """그 행 전체(같은 표·같은 행의 모든 셀)를 자르고 대상 셀에 테두리를 친다 — 인쇄된 광종·편이 같이 보여야 한다.
    표 밖 자유 필드(row_no = -1)는 그 필드 주변을 넓게 자른다. box=False 면 테두리 없이 (✓ 행: 두 칸을 같이 본다)."""
    r = field_info(con, field_id)
    if r is None:
        raise KeyError(field_id)
    pad = _pad(r, pad)
    if r["row_no"] >= 0:
        ext = con.execute("SELECT MIN(x0), MIN(y0), MAX(x1), MAX(y1) FROM doc_field WHERE page_id = ? AND region = ? "
                          "AND row_no = ?", (r["page_id"], r["region"], r["row_no"])).fetchone()
        x0, y0, x1, y1 = ext
    else:
        x0, y0, x1, y1 = r["x0"] - 120, r["y0"] - 40, r["x1"] + 120, r["y1"] + 40
    x0, y0 = max(0, x0 - pad), max(0, y0 - pad)
    gray, src = crop_region(settings, r, (x0, y0, x1 + pad, y1 + pad), 1.0, res)
    crop = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
    if box:
        cv2.rectangle(crop, (r["x0"] - x0 - 2, r["y0"] - y0 - 2), (r["x1"] - x0 + 2, r["y1"] - y0 + 2), (0, 0, 220), 3)
    return _png(crop), src


def pair_crop(con: sqlite3.Connection, settings, first_id: str, second_id: str, scale: float = 3,
              res: str = "auto") -> tuple[bytes, str]:
    """나란한 두 칸(✓ 의 유·무)을 한 띠로, 행 높이의 절반만큼 여유를 두고 scale 배로. ✓ 는 경계선을 넘어 그려지므로 두 칸을 같이 본다.
    표시를 하지 않는다 — 기계의 판정이 드러나지 않게."""
    a, b = field_info(con, first_id), field_info(con, second_id)
    if a is None or b is None:
        raise KeyError(first_id if a is None else second_id)
    x0, y0 = min(a["x0"], b["x0"]), min(a["y0"], b["y0"])
    x1, y1 = max(a["x1"], b["x1"]), max(a["y1"], b["y1"])
    pad = auto_pad((x0, y0, x1, y1))
    gray, src = crop_region(settings, a, (max(0, x0 - pad), max(0, y0 - pad), x1 + pad, y1 + pad), scale, res)
    return _png(gray), src


def row_png(con: sqlite3.Connection, settings, field_id: str, pad: int | None = None) -> bytes:
    return row_crop(con, settings, field_id, pad)[0]

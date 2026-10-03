"""검수 화면용 크롭. 요청이 올 때 잘라 낸다. 따로 저장하지 않는다.

좌표는 전부 템플릿 좌표계(doc_field 의 bbox)다. 원본에 닿으면(아카이브가 연결된 컴퓨터) 쪽의 호모그래피로 원본을
높은 해상도로 렌더링해 그 셀만 정합하고(imaging/hires.py, source), 아니면 200 dpi 정합 이미지에서 자른다(aligned).
어느 쪽을 썼는지 같이 돌려준다 (tasks/0002 4.5). 읽기는 imaging/io.py 를 쓴다 (한글 경로).
"""
from __future__ import annotations

import json
import sqlite3
from functools import lru_cache
from pathlib import Path

import cv2
import numpy as np

from ..imaging.hires import cell_from_source
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


def _clip(img: np.ndarray, x0: int, y0: int, x1: int, y1: int) -> np.ndarray:
    h, w = img.shape[:2]
    return img[max(0, y0):min(h, y1), max(0, x0):min(w, x1)]


def _png(img: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".png", img)
    if not ok:
        raise CropError("PNG 인코딩 실패")
    return buf.tobytes()


def _pad(r: sqlite3.Row, pad: int | None) -> int:
    """여유 폭. 실제 양식은 행 높이가 27 px 쯤이라 고정 6 px 로는 칸 선을 넘은 획이 잘린다 → 행 높이의 절반, 최소 8 px."""
    return pad if pad is not None else max(8, (r["y1"] - r["y0"]) // 2)


def crop_region(settings, r: sqlite3.Row, bbox: tuple[int, int, int, int], out_scale: float = 1.0,
                res: str = "auto") -> tuple[np.ndarray, str]:
    """템플릿 좌표의 bbox 를 out_scale 배 크기로. res: auto(원본이 닿으면 원본) | source | aligned.
    돌려주는 값: (회색조 이미지, "source" 또는 "aligned")."""
    if res not in ("auto", "source", "aligned"):
        raise ValueError(f"res 는 auto | source | aligned: {res}")
    src = resolve_source(r["source_path"], r["source_rel"], settings.archive_root) if res != "aligned" else None
    if src is not None and r["homography"]:
        try:
            img = cell_from_source(src, r["page_no"], np.array(json.loads(r["homography"])), r["render_dpi"] or settings.dpi,
                                   bbox, 0, out_scale, settings.source_dpi, settings.damaged_pdf)
            return img, "source"
        except (OSError, ValueError, KeyError, RuntimeError):
            if res == "source":
                raise
    elif res == "source":
        raise CropError(f"원본에 닿을 수 없습니다: {r['source_path']} (archive_root 와 source_rel 을 확인하세요)")
    img = aligned_image(settings, r)
    x0, y0, x1, y1 = bbox
    crop = _clip(img, x0, y0, x1, y1)
    if out_scale != 1 and crop.size:
        crop = cv2.resize(crop, None, fx=out_scale, fy=out_scale, interpolation=cv2.INTER_CUBIC)
    return crop, "aligned"


def cell_crop(con: sqlite3.Connection, settings, field_id: str, pad: int | None = None, scale: float = 3,
              res: str = "auto") -> tuple[bytes, str]:
    """셀 하나(PNG). pad 만큼 여유를 두고 scale 배로 키운다 (한두 자리 숫자를 크게 보기 위해). 둘째 값은 출처."""
    r = field_info(con, field_id)
    if r is None:
        raise KeyError(field_id)
    pad = _pad(r, pad)
    img, src = crop_region(settings, r, (r["x0"] - pad, r["y0"] - pad, r["x1"] + pad, r["y1"] + pad), scale, res)
    return _png(img), src


def cell_png(con: sqlite3.Connection, settings, field_id: str, pad: int | None = None, scale: int = 3) -> bytes:
    return cell_crop(con, settings, field_id, pad, scale)[0]


def row_crop(con: sqlite3.Connection, settings, field_id: str, pad: int | None = None,
             res: str = "auto") -> tuple[bytes, str]:
    """그 행 전체(같은 표·같은 행의 모든 셀)를 자르고 대상 셀에 테두리를 친다 — 인쇄된 광종·편이 같이 보여야 한다.
    표 밖 자유 필드(row_no = -1)는 그 필드 주변을 넓게 자른다."""
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
    cv2.rectangle(crop, (r["x0"] - x0 - 2, r["y0"] - y0 - 2), (r["x1"] - x0 + 2, r["y1"] - y0 + 2), (0, 0, 220), 3)
    return _png(crop), src


def row_png(con: sqlite3.Connection, settings, field_id: str, pad: int | None = None) -> bytes:
    return row_crop(con, settings, field_id, pad)[0]

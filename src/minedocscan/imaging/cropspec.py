"""인식기에 넘기는 셀 크롭의 규격과, 셀 하나를 규격대로 뜨는 **유일한** 구현 (tasks/0003 4.1).

검수 화면(review/crops.py), 크롭 내보내기(review/export.py), 파이프라인(handlers/base.recognize)이 전부 여기를 부른다.
그래서 같은 셀이면 내보낸 PNG 와 파이프라인이 인식기에 넘긴 배열이 화소까지 같다 — 학습 때 본 그림과 운용 때 보는
그림이 다르면 평가셋의 수치가 운용에서 재현되지 않는다.

좌표계는 템플릿 좌표(기준 이미지 픽셀, 200 dpi) 하나다. 규격(CropSpec):
  res    aligned = 200 dpi 정합 이미지에서 자른다 | source = 원본을 source_dpi 로 렌더링해 그 셀만 호모그래피로 정합한다
  scale  템플릿 좌표 대비 배율 (0 초과 6 이하). 결과 크기 = round(상자 크기 × scale)
  pad    칸 둘레 여유(템플릿 px). None 이면 행 높이의 절반(최소 8) — 실제 숫자 칸은 글씨가 칸보다 커서 괘선을 넘는다

PageImages 는 한 쪽의 그림 두 가지(정합 이미지, 원본)를 필요할 때 한 번만 만들어 들고 있는다.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from .hires import render_source

RESOLUTIONS = ("aligned", "source")
MAX_SCALE = 6.0
AUTO_PAD = "auto"            # JSON 에서 "행 높이의 절반, 최소 8" 규칙을 적는 이름


class CropUnavailable(RuntimeError):
    """그 규격의 그림을 만들 수 없다 (정합 이미지가 없거나 원본에 닿을 수 없음)."""


@dataclass(frozen=True)
class CropSpec:
    res: str = "aligned"
    scale: float = 1.0
    pad: int | None = 0                  # None = 행 높이의 절반, 최소 8 px

    def __post_init__(self) -> None:
        if self.res not in RESOLUTIONS:
            raise ValueError(f"res 는 {RESOLUTIONS} 중 하나: {self.res!r}")
        if not (0 < float(self.scale) <= MAX_SCALE):
            raise ValueError(f"scale 은 0 초과 {MAX_SCALE:g} 이하: {self.scale}")
        if self.pad is not None and int(self.pad) < 0:
            raise ValueError(f"pad 는 0 이상: {self.pad}")

    def pad_for(self, bbox: tuple[int, int, int, int]) -> int:
        """이 칸에 실제로 쓸 여유(px). 규칙은 검수 화면과 같다."""
        return int(self.pad) if self.pad is not None else auto_pad(bbox)

    def box_for(self, bbox: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
        p = self.pad_for(bbox)
        x0, y0, x1, y1 = bbox
        return x0 - p, y0 - p, x1 + p, y1 + p

    def to_dict(self) -> dict:
        return {"res": self.res, "scale": float(self.scale), "pad": AUTO_PAD if self.pad is None else int(self.pad)}

    @classmethod
    def from_dict(cls, d: dict) -> CropSpec:
        pad = d.get("pad", 0)
        return cls(str(d.get("res", "aligned")), float(d.get("scale", 1.0)), None if pad in (None, AUTO_PAD) else int(pad))

    def describe(self) -> str:
        pad = "행 높이의 절반" if self.pad is None else f"{self.pad} px"
        return f"{self.res} ×{self.scale:g}, 여유 {pad}"


# 규격을 선언하지 않은 백엔드(null, oracle)가 받는 크롭: 칸 그대로 (cells.observe_cells 의 crop 과 같다)
DEFAULT_SPEC = CropSpec("aligned", 1.0, 0)


def auto_pad(bbox: tuple[int, int, int, int]) -> int:
    """실제 양식은 행 높이가 27 px 쯤이라 고정 6 px 로는 칸 선을 넘은 획이 잘린다 → 행 높이의 절반, 최소 8 px."""
    return max(8, (bbox[3] - bbox[1]) // 2)


class PageImages:
    """한 쪽의 그림. 정합 이미지와 원본(source_dpi 로 렌더링한 것)을 필요할 때 한 번씩만 만든다.

    aligned        정합 이미지(템플릿 좌표) 배열, 또는 None (aligned_loader 로 읽는다)
    source         원본 파일 경로 (PDF 또는 이미지). 닿을 수 없으면 None
    homography     렌더링한 쪽 픽셀 → 템플릿 픽셀 (3×3). 없으면 원본 크롭을 만들 수 없다
    render_dpi     그 호모그래피를 구할 때 쪽을 렌더링한 해상도
    source_image   이미 읽어 둔 원본 이미지(이미지 파일로 들어온 문서) — 파이프라인이 다시 읽지 않게
    """

    def __init__(self, *, aligned: np.ndarray | None = None, aligned_loader: Callable[[], np.ndarray] | None = None,
                 source: str | Path | None = None, page_no: int = 1, homography=None, render_dpi: int = 200,
                 source_dpi: int = 300, damaged: str = "fail", source_image: np.ndarray | None = None,
                 aligned_missing: str = "정합 이미지가 없습니다", source_missing: str = "원본에 닿을 수 없습니다"):
        self._aligned = aligned
        self._aligned_loader = aligned_loader
        self.source = Path(source) if source is not None else None
        self.page_no = page_no
        self.homography = None if homography is None else np.asarray(homography, dtype=np.float64)
        self.render_dpi = int(render_dpi)
        self.source_dpi = int(source_dpi)
        self.damaged = damaged
        self._source_image = source_image
        self._source: tuple[np.ndarray, float] | None = None
        self._aligned_missing, self._source_missing = aligned_missing, source_missing

    def aligned(self) -> np.ndarray:
        if self._aligned is None:
            if self._aligned_loader is None:
                raise CropUnavailable(self._aligned_missing)
            self._aligned = self._aligned_loader()
        return self._aligned

    @property
    def has_source(self) -> bool:
        return self.homography is not None and (self._source_image is not None or self.source is not None)

    def source_page(self) -> tuple[np.ndarray, float]:
        """(원본 쪽 그림, k). k = 원본 픽셀 / 렌더링 픽셀. 쪽마다 한 번만 렌더링한다."""
        if self._source is None:
            if not self.has_source:
                raise CropUnavailable(self._source_missing)
            if self._source_image is not None:
                self._source = (self._source_image, 1.0)
            else:
                self._source = render_source(self.source, self.page_no, self.render_dpi, self.source_dpi, self.damaged)
        return self._source


def crop_box(images: PageImages, box: tuple[int, int, int, int], scale: float, res: str) -> np.ndarray:
    """템플릿 좌표의 상자 box 를 scale 배 크기(round(폭×scale) × round(높이×scale))의 회색조 배열로.
    상자가 그림 밖으로 나가면 흰색으로 채운다. 셀 크롭과 행 띠가 같이 쓴다."""
    x0, y0, x1, y1 = (int(v) for v in box)
    w, h = max(1, round((x1 - x0) * scale)), max(1, round((y1 - y0) * scale))
    t = np.array([[scale, 0, -x0 * scale], [0, scale, -y0 * scale], [0, 0, 1]], dtype=np.float64)
    if res == "source":
        img, k = images.source_page()
        s_inv = np.array([[1 / k, 0, 0], [0, 1 / k, 0], [0, 0, 1]], dtype=np.float64)       # 원본 → 렌더링 좌표
        return cv2.warpPerspective(img, t @ images.homography @ s_inv, (w, h), flags=cv2.INTER_CUBIC, borderValue=255)
    if res != "aligned":
        raise ValueError(f"res 는 {RESOLUTIONS} 중 하나: {res!r}")
    img = images.aligned()
    sub = _cut(img, x0, y0, x1, y1)
    if (w, h) == (sub.shape[1], sub.shape[0]):
        return sub
    return cv2.resize(sub, (w, h), interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_CUBIC)


def crop_cell(images: PageImages, bbox: tuple[int, int, int, int], spec: CropSpec) -> np.ndarray:
    """셀 하나를 규격대로. 내보내기와 파이프라인이 둘 다 이것을 부른다."""
    return crop_box(images, spec.box_for(bbox), float(spec.scale), spec.res)


def _cut(img: np.ndarray, x0: int, y0: int, x1: int, y1: int) -> np.ndarray:
    """img[y0:y1, x0:x1] — 밖으로 나간 부분은 흰색(255). 안쪽이면 복사 없이 같은 화소."""
    h, w = img.shape[:2]
    if x0 >= 0 and y0 >= 0 and x1 <= w and y1 <= h:
        return img[y0:y1, x0:x1]
    out = np.full((max(0, y1 - y0), max(0, x1 - x0)), 255, dtype=img.dtype)
    sx0, sy0, sx1, sy1 = max(0, x0), max(0, y0), min(w, x1), min(h, y1)
    if sx1 > sx0 and sy1 > sy0:
        out[sy0 - y0:sy1 - y0, sx0 - x0:sx1 - x0] = img[sy0:sy1, sx0:sx1]
    return out

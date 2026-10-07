"""글씨 덩어리 → 셀 배정 (표 서식을 벗어난 수기 텍스트 영역 인식).

표 괘선을 지운 뒤 RLSA(Run Length Smoothing)로 글씨 덩어리를 묶고, 덩어리가 셀 하나에 속하는지
여러 셀에 걸치는지(= 셀 값이 아니라 표 위에 쓴 메모)를 판정해 소속 셀을 정한다.

템플릿 정합 뒤에 쓰므로 표 검출 모델은 필요 없다. 셀 좌표를 이미 알기 때문에
'어느 셀에 속하는가'와 '셀을 넘어갔는가'만 판정하면 된다.
"""
from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from ..forms.template import Cell
from .grid import binarize, detect_grid_roi


@dataclass
class Blob:
    bbox: tuple[int, int, int, int]
    area: int
    cells: list[int]          # 겹치는 셀 인덱스
    is_note: bool             # 여러 셀에 걸친 메모


def rlsa_h(binary: np.ndarray, gap: int) -> np.ndarray:
    """수평 RLSA: 같은 행에서 gap 이하로 떨어진 잉크 픽셀 사이를 채운다."""
    k = cv2.getStructuringElement(cv2.MORPH_RECT, (gap, 1))
    return cv2.morphologyEx(binary, cv2.MORPH_CLOSE, k)


def erase_rules(binary: np.ndarray, ys: list[int], xs: list[int], band: int = 3) -> np.ndarray:
    """괘선 위치를 알고 있으므로 그 띠를 지운다. 기울어 남은 선 조각은 형태학 연산으로 한 번 더 지운다."""
    out = binary.copy()
    for y in ys:
        out[max(0, y - band):y + band + 1, :] = 0
    for x in xs:
        out[:, max(0, x - band):x + band + 1] = 0
    hk = cv2.getStructuringElement(cv2.MORPH_RECT, (70, 1))
    vk = cv2.getStructuringElement(cv2.MORPH_RECT, (1, 50))
    leftovers = cv2.morphologyEx(out, cv2.MORPH_OPEN, hk) | cv2.morphologyEx(out, cv2.MORPH_OPEN, vk)
    return cv2.bitwise_and(out, cv2.bitwise_not(cv2.dilate(leftovers, np.ones((3, 3), np.uint8))))


def assign_blobs(aligned_gray: np.ndarray, cells: list[Cell], grid_ys: list[int], grid_xs: list[int],
                 min_area: int = 40, note_span: float = 1.6,
                 print_mask: np.ndarray | None = None) -> tuple[dict[int, int], list[Blob]]:
    """표 하나 안의 글씨 덩어리를 셀에 배정한다.

    반환: ({셀 인덱스: 배정된 잉크 면적}, [덩어리 목록]).
    메모로 판정된 덩어리는 어느 셀에도 면적을 더하지 않는다.
    print_mask (쪽 전체의 인쇄 마스크, bool): 표 영역을 이진화한 직후 그 화소를 0 으로 한다 — 인쇄를 뺀 그림으로 덩어리를 묶는다
    (tasks/0006 4.3). 괘선 재검출은 회색 그림 그대로. None 이면 예전과 같다.
    """
    x0, y0, x1, y1 = min(grid_xs), min(grid_ys), max(grid_xs), max(grid_ys)
    raw = binarize(aligned_gray[y0:y1, x0:x1])
    if raw.size == 0 or not cells:
        return {}, []
    if print_mask is not None:
        raw[print_mask[y0:y1, x0:x1]] = 0
    # 정합 뒤에도 괘선은 몇 px 어긋날 수 있으므로 템플릿 위치와 실제 재검출 위치를 모두 지운다
    fys, fxs = detect_grid_roi(aligned_gray, (max(0, x0 - 8), max(0, y0 - 8), x1 + 8, y1 + 8), 0.3, 0.25)
    b = erase_rules(raw, [y - y0 for y in list(grid_ys) + fys], [x - x0 for x in list(grid_xs) + fxs], band=4)
    b = cv2.morphologyEx(b, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
    cell_w = int(np.median([c.bbox[2] - c.bbox[0] for c in cells]))
    merged = rlsa_h(b, max(6, cell_w // 2))     # 단어 사이 공백을 넘어 한 줄 메모가 하나로 묶이도록
    n, labels, stats, _ = cv2.connectedComponentsWithStats(merged, connectivity=8)
    ink_by_cell: dict[int, int] = {}
    blobs: list[Blob] = []
    for i in range(1, n):
        bx, by, bw, bh, _ = stats[i]
        if bw * bh < min_area:
            continue
        area = int((b[labels == i] > 0).sum())      # 실제 잉크 면적 (RLSA 로 채운 부분 제외)
        if area < min_area:
            continue
        gx0, gy0, gx1, gy1 = bx + x0, by + y0, bx + bw + x0, by + bh + y0
        hits = []
        for ci, c in enumerate(cells):
            cx0, cy0, cx1, cy1 = c.bbox
            if _overlap(c.bbox, (gx0, gy0, gx1, gy1)) >= 0.3 * min(bw * bh, (cx1 - cx0) * (cy1 - cy0)):
                hits.append(ci)
        is_note = bw > note_span * cell_w or len(hits) >= 3 or (not hits and bw > cell_w)
        blobs.append(Blob((gx0, gy0, gx1, gy1), area, hits, is_note))
        if not is_note and hits:
            best = max(hits, key=lambda ci: _overlap(cells[ci].bbox, (gx0, gy0, gx1, gy1)))
            ink_by_cell[best] = ink_by_cell.get(best, 0) + area
    return ink_by_cell, blobs


def _overlap(a, b) -> int:
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    return ix * iy


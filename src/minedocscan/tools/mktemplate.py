"""새 양식의 템플릿 뼈대 만들기.

빈 양식(또는 깨끗한 스캔 한 장)에서 표 괘선을 검출해 template.yaml 초안과 reference.png 를 쓴다.
검출된 것은 기하(괘선 좌표)뿐이다. 열 이름·kind·행 키·핸들러는 사람이 채운다 (docs/SITE_PACK.md).
표가 여러 개인 양식은 표마다 --roi 를 주어 여러 번 실행한 뒤 regions 를 합친다.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from ..imaging.grid import detect_grid, detect_grid_roi
from ..imaging.io import imwrite, load_pages


def init_template(image: str | Path, name: str, templates_dir: str | Path, roi: tuple[int, int, int, int] | None = None,
                  header_rows: int = 1, page: int = 1, dpi: int = 200, handler: str = "generic",
                  title: str | None = None, overwrite: bool = False) -> Path:
    gray = None
    for no, g in load_pages(image, dpi):
        if no == page:
            gray = g
            break
    if gray is None:
        raise ValueError(f"{image} 에 {page} 페이지가 없습니다")
    if roi:
        ys, xs = detect_grid_roi(gray, roi)
    else:
        ys, xs, _, _ = detect_grid(gray)

    out_dir = Path(templates_dir) / name
    target = out_dir / "template.yaml"
    if target.exists() and not overwrite:
        raise FileExistsError(f"이미 있습니다: {target} (덮어쓰려면 --overwrite)")
    h, w = gray.shape
    spec: dict = {"name": name, "title": title or name, "reference_image": "reference.png", "dpi": dpi,
                  "page_size": [w, h], "handler": handler, "handler_options": {}, "regions": [], "fields": []}
    n_rows = len(ys) - 1 - header_rows
    if len(xs) >= 2 and n_rows >= 1:
        spec["regions"].append({
            "name": "main", "grid": {"ys": ys, "xs": xs}, "header_rows": header_rows,
            # kind 를 고친다: printed | handwritten_text | handwritten_number | checkmark | signature
            "columns": [{"idx": i, "name": f"col_{i}", "kind": "handwritten_text"} for i in range(len(xs) - 1)],
            "rows": [{"row": i, "key": f"row_{i}"} for i in range(n_rows)],
        })
    out_dir.mkdir(parents=True, exist_ok=True)
    imwrite(out_dir / "reference.png", gray)
    target.write_text(yaml.safe_dump(spec, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return target

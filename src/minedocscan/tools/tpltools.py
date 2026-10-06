"""템플릿을 만드는 사람의 도구 (tasks/0005 단계 6): preview(칸·필드를 기준 이미지 위에 그린 그림), check(오류를 전부 목록으로).

  minedocscan template preview <템플릿 폴더> [--scan FILE --page N]   → WORK_ROOT/template-preview/<이름>.png
  minedocscan template preview <템플릿 폴더> --print                  → 인쇄 층 위에 (인쇄 화소에 색) — tasks/0006 단계 2
  minedocscan template check   <템플릿 폴더>                          → 오류 목록 (없으면 0줄, 종료 코드 0)

preview 의 그림에는 실제 양식(이름이 인쇄·기재된 머리글, --scan 이면 손글씨)이 들어 있다 — 저장소 안에는 쓰지 않는다 (거절).
그림에 쓰는 글자는 칸·필드 이름·종류·형식·역할과 행 번호(행 키가 ASCII 면 키도)뿐이다. OpenCV 내장 글꼴은 한글을 못 그린다.
"""
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import yaml

from ..forms.template import CELL_KINDS, Template, TemplateError, cell_lines, region_cells

# 칸 종류마다의 색 (BGR)
COLORS = {"handwritten_number": (200, 80, 0), "handwritten_text": (40, 150, 40), "checkmark": (0, 140, 255),
          "signature": (160, 40, 160), "printed": (150, 150, 150)}
SPLIT_COLOR = (0, 200, 255)
REGION_COLOR = (0, 0, 220)
PRINT_COLOR = (210, 210, 60)           # preview --print: 인쇄 층의 인쇄 화소 (청록 — 칸 종류의 색과 겹치지 않게)
COVERED_LIST = 10                      # check: 인쇄에 덮인 칸을 이만큼까지 이름으로, 나머지는 수로


# ── check ──────────────────────────────────────────────────────────────────
def check_template(tdir: str | Path) -> list[str]:
    """템플릿 폴더의 오류 전부: 읽기 오류, 필수 항목, 핸들러, 표·열·행·필드(이름 겹침, 괘선 범위, 형식과 종류, 역할에 필요한 칸,
    소계), 겹치는 칸, 쪽 밖의 칸, 너무 좁은 칸, 기준 이미지. 값(머리글의 이름·차량번호, 행 키)은 찍지 않는다."""
    tdir = Path(tdir)
    path = tdir / "template.yaml" if tdir.is_dir() else tdir
    if not path.exists():
        return [f"template.yaml 이 없습니다: {path}"]
    try:
        spec = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        return [f"YAML 을 읽을 수 없습니다: {getattr(e, 'problem_mark', '') or e}"]
    if not isinstance(spec, dict):
        return ["template.yaml 의 맨 위는 항목들(키: 값)이어야 합니다"]
    out = [f"필수 항목이 없습니다: {k}" for k in ("name", "reference_image") if not spec.get(k)]
    if out:
        return out
    try:
        tpl = Template(path, validate=False)
        out += tpl.problems()
    except (TemplateError, KeyError, TypeError, ValueError) as e:
        return out + [f"템플릿을 읽을 수 없습니다: {type(e).__name__}: {e}"]
    from ..handlers import REGISTRY

    if tpl.handler not in REGISTRY:
        out.append(f"알 수 없는 handler {tpl.handler!r} (가능: {', '.join(sorted(REGISTRY))})")
    names = [r.get("name") for r in tpl.regions]
    out += [f"표 이름 {n!r} 이 겹칩니다" for n in sorted({n for n in names if names.count(n) > 1}, key=str)]
    if "fields" in names:
        out.append("표 이름 'fields' 는 쓸 수 없습니다 (표 밖 필드의 자리)")
    for reg in tpl.regions:
        cols = [c.get("name") for c in reg.get("columns") or []]
        out += [f"{tpl.name}/{reg.get('name')}: 열 이름 {n!r} 이 겹칩니다" for n in sorted({n for n in cols if cols.count(n) > 1}, key=str)]
        rows = [r.get("row") for r in reg.get("rows") or []]
        out += [f"{tpl.name}/{reg.get('name')}: 행 번호 {n} 가 겹칩니다" for n in sorted({n for n in rows if rows.count(n) > 1}, key=str)]
    for f in tpl.fields:
        b = f.get("bbox")
        if not (isinstance(b, list | tuple) and len(b) == 4 and all(isinstance(v, int) for v in b)):
            out.append(f"{tpl.name}/fields/{f.get('name')}: bbox 는 정수 네 개 [x0, y0, x1, y1]")
    cells, skipped = _boxes(tpl)
    if skipped:                                              # 칸을 만들 수 없는 표(괘선 범위 밖의 행·열 …)는 기하 검사에서 빠진다
        out.append(f"{', '.join(f'{tpl.name}/{n}' for n in skipped)}: 칸을 만들 수 없어(행·열이 괘선 범위 밖이거나 "
                   f"표 정의가 빠짐 — 위의 오류) 기하 검사(겹침·쪽 밖·좁은 칸)를 건너뛰었습니다 (표 {len(skipped)}개)")
    size = _page_size(tpl, out)
    for name, (x0, y0, x1, y1) in cells:
        if x1 - x0 < 4 or y1 - y0 < 4:
            out.append(f"{name}: 칸이 너무 좁습니다 ({x1 - x0}×{y1 - y0} px — 괘선 안쪽 여백 4 px 를 뺀 크기)")
        if size and (x0 < 0 or y0 < 0 or x1 > size[0] or y1 > size[1]):
            out.append(f"{name}: 쪽 밖으로 나갑니다 (쪽 {size[0]}×{size[1]} px)")
    for i, (a, ba) in enumerate(cells):
        for b, bb in cells[i + 1:]:
            if min(ba[2], bb[2]) > max(ba[0], bb[0]) and min(ba[3], bb[3]) > max(ba[1], bb[1]):
                out.append(f"칸이 겹칩니다: {a} ↔ {b}")
    out += _print_covered(tpl)
    return out


def _print_covered(tpl: Template) -> list[str]:
    """인쇄 층(print_image)이 있으면: 표의 손으로 쓰는 칸 중 인쇄 마스크(4.3 — 2 px 넓힌 것)가 칸의 절반 넘게 덮은 칸을 한 줄로.
    값이 들어갈 자리가 없다 — 칸 안의 인쇄, 손글씨의 잔상, 다른 양식·어긋난 층. 파일·크기의 오류는 problems() 가 이미 알렸다.
    표 밖 필드는 오류로 세지 않는다: 날마다 같은 자리에 같은 글씨로 쓰는 필드(작성자·서명·점검란)에는 잔상이 남는 것이 정상이고
    필드는 인쇄 층을 쓰지 않는다 (4.3, 8절 1) — 필드의 덮인 비율은 print-layer 의 요약과 preview --print 로 본다."""
    from ..imaging.printlayer import coverage

    if tpl.print_path is None or tpl.print_problems():
        return []
    try:
        m = tpl.print_mask
    except (OSError, ValueError):                            # 머리(IHDR)는 맞는데 그림이 깨졌다 — 파이프라인이 쓸 때 멈춘다
        return [f"{tpl.name}: 인쇄 층을 읽을 수 없습니다 (그림이 깨졌습니다): {tpl.spec.get('print_image')}"]
    boxes = handwritten_boxes(tpl, fields=False)
    bad = [n for (n, _b), c in zip(boxes, coverage(m, [b for _n, b in boxes]), strict=True) if c > 0.5]
    if not bad:
        return []
    names = ", ".join(bad[:COVERED_LIST]) + (f" … 외 {len(bad) - COVERED_LIST}개" if len(bad) > COVERED_LIST else "")
    return [f"{tpl.name}: 인쇄 층이 칸의 절반 넘게 덮은 표의 손으로 쓰는 칸 {len(bad)}개 — 값이 들어갈 자리가 없습니다 "
            f"(칸 안의 인쇄, 손글씨의 잔상이거나 맞지 않는 층 — template preview --print 로 봅니다): {names}"]


def _page_size(tpl: Template, out: list[str]) -> tuple[int, int] | None:
    ref = tpl.dir / tpl.spec["reference_image"]
    if not ref.exists():
        out.append(f"기준 이미지가 없습니다: {tpl.spec['reference_image']}")
        ps = tpl.spec.get("page_size")
        return tuple(ps) if isinstance(ps, list) and len(ps) == 2 else None
    h, w = tpl.reference.shape[:2]
    ps = tpl.spec.get("page_size")
    if isinstance(ps, list) and len(ps) == 2 and tuple(ps) != (w, h):
        out.append(f"page_size {ps} 가 기준 이미지 크기 [{w}, {h}] 와 다릅니다")
    return w, h


def _boxes(tpl: Template, hand_only: bool = False,
           fields: bool = True) -> tuple[list[tuple[str, tuple[int, int, int, int]]], list[str]]:
    """((이름, bbox) 목록, 칸을 만들 수 없는 표의 이름) — 표의 칸은 "<표>/<열>/행 <번호>", 필드는 "fields/<이름>".
    칸을 만들 수 없는 표는 통째로 빠지고 나머지 표·필드는 검사한다. bbox 가 정수 네 개가 아닌 필드는 이미 오류로 알렸으므로 뺀다.
    hand_only: 손으로 쓰는 칸(handwritten_*)만 — 인쇄 층에 덮인 비율을 잴 칸 (print-layer 의 요약, check). fields: 표 밖 필드도."""
    out, skipped = [], []
    hand = lambda kind: str(kind or "").startswith("handwritten")      # noqa: E731
    for reg in tpl.regions:
        try:
            out += [(f"{c.region}/{c.name}/행 {c.row}", c.bbox) for c in region_cells(reg) if not hand_only or hand(c.kind)]
        except (KeyError, IndexError, TypeError, ValueError):
            skipped.append(str(reg.get("name")))
    for f in tpl.fields if fields else []:
        b = f.get("bbox")
        if isinstance(b, list | tuple) and len(b) == 4 and all(isinstance(v, int) for v in b):
            if not hand_only or hand(f.get("kind")):
                out.append((f"fields/{f.get('name')}", tuple(b)))
    return out, skipped


def handwritten_boxes(tpl: Template, fields: bool = True) -> list[tuple[str, tuple[int, int, int, int]]]:
    """손으로 쓰는 칸의 (이름, bbox) — 표의 칸과 (fields 이면) 표 밖 필드, template check 와 같은 이름 (행 키는 찍지 않는다)."""
    return _boxes(tpl, hand_only=True, fields=fields)[0]


# ── preview ────────────────────────────────────────────────────────────────
def preview(tdir: str | Path, out_dir: str | Path, scan: str | Path | None = None, page: int = 1, dpi: int = 200,
            print_layer: bool = False) -> dict:
    """칸·필드의 테두리와 이름·종류·형식·역할·행 번호를 기준 이미지(또는 --scan 의 쪽을 정합한 것) 위에 그린 PNG.
    print_layer=True(--print): 인쇄 층 위에, 인쇄 화소(넓히지 않은 것)를 한 색으로 — 값 자리가 인쇄에 덮이지 않았나 (tasks/0006 4.2).
    돌려주는 값: {"out", "boxes"(그린 테두리 수 = 칸 + 필드), "aligned"(--scan 이면 정합 결과)}."""
    from ..imaging.io import imwrite
    from ..review.export import inside_git_tree

    tdir = Path(tdir)
    path = tdir / "template.yaml" if tdir.is_dir() else tdir
    out_dir = Path(out_dir)
    if inside_git_tree(out_dir):
        raise ValueError(f"{out_dir} 은 git 작업 트리 안입니다. 미리보기에는 실제 양식(이름·차량번호)이 들어 있습니다 — "
                         "저장소 밖(WORK_ROOT)에 씁니다")
    if print_layer and scan is not None:
        raise ValueError("--print 와 --scan 은 같이 쓰지 않습니다 (인쇄 층은 템플릿 좌표의 그림이다)")
    tpl = Template(path)
    if print_layer and tpl.print_path is None:
        raise ValueError(f"{tpl.name}: 인쇄 층이 없습니다 — template print-layer 로 만든 뒤 template.yaml 에 "
                         "print_image: print.png 를 적습니다")
    try:                                                     # 기준 이미지가 없거나 깨졌으면 템플릿 폴더의 오류다 — template check 가 알린다
        base, aligned = tpl.reference, None
    except (OSError, ValueError) as e:
        raise TemplateError(f"기준 이미지를 읽을 수 없습니다: {tpl.spec.get('reference_image')}") from e
    tint = None
    if print_layer:
        from ..imaging.printlayer import binary

        try:
            base = tpl.print_layer
        except (OSError, ValueError) as e:
            raise TemplateError(f"인쇄 층을 읽을 수 없습니다: {tpl.spec.get('print_image')}") from e
        tint = binary(base)
    if scan is not None:
        from ..imaging.align import align_to_template
        from ..imaging.io import load_pages

        gray = next((g for no, g in load_pages(scan, dpi) if no == page), None)
        if gray is None:
            raise ValueError(f"{scan} 에 {page} 쪽이 없습니다")
        ar = align_to_template(gray, tpl.reference, tpl.regions, ref_features=tpl.features)
        base = ar.warped
        aligned = {"ok": bool(ar.ok), "inliers": int(ar.n_inliers), "grid_err": None if ar.grid_err_px == float("inf")
                   else round(float(ar.grid_err_px), 2)}
    img, boxes = draw(tpl, base, tint=tint)
    stem = tpl.name if scan is None else f"{tpl.name}__{Path(scan).stem}_p{page}"
    if print_layer:
        stem = f"{tpl.name}__print"
    out = out_dir / f"{stem}.png"
    imwrite(out, img)
    return {"out": str(out), "boxes": boxes, "aligned": aligned}


def draw(tpl: Template, gray: np.ndarray, tint: np.ndarray | None = None) -> tuple[np.ndarray, int]:
    """그림 (BGR)과 그린 테두리 수. 칸은 종류의 색, 나눔 선(split_ys·split_xs)은 노란 점선, 표의 테두리 위에 표 이름·역할.
    tint: 이 화소(bool)를 PRINT_COLOR 로 칠한 뒤 테두리를 그린다 (preview --print 의 인쇄 화소)."""
    boxes = 0
    img = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR) if gray.ndim == 2 else gray.copy()
    img = cv2.addWeighted(img, 0.55, np.full_like(img, 255), 0.45, 0)        # 바탕을 흐리게 — 테두리가 보이게
    if tint is not None:
        img[tint] = PRINT_COLOR
    for reg in tpl.regions:
        ys, xs = cell_lines(reg)
        g = reg["grid"]
        for y in g.get("split_ys") or []:
            _dashed(img, (xs[0], y), (xs[-1], y), SPLIT_COLOR)
        for x in g.get("split_xs") or []:
            _dashed(img, (x, ys[0]), (x, ys[-1]), SPLIT_COLOR)
        cv2.rectangle(img, (xs[0], ys[0]), (xs[-1], ys[-1]), REGION_COLOR, 1)
        role = f" (role {reg['role']})" if reg.get("role") else ""
        _text(img, f"{reg['name']}{role}", xs[0], ys[0] - 6, REGION_COLOR, 0.55)
    for c in tpl.cells():
        x0, y0, x1, y1 = c.bbox
        color = COLORS.get(c.kind, (0, 0, 0))
        cv2.rectangle(img, (x0, y0), (x1, y1), color, 2)
        boxes += 1
        label = c.name + (f" [{c.fmt}]" if c.fmt and c.kind.startswith("handwritten") else "")
        if c.kind == "printed":
            label = c.name
        _text(img, label, x0 + 3, y0 + 14, color, 0.4)
        if c.col == min(col["idx"] for col in tpl.region(c.region)["columns"]):
            key = c.row_key if c.row_key.isascii() else ""
            _text(img, f"r{c.row} {key}".strip(), x0 + 3, y1 - 4, (60, 60, 60), 0.35)
    for c in tpl.field_cells():
        x0, y0, x1, y1 = c.bbox
        color = COLORS.get(c.kind, (0, 0, 0))
        cv2.rectangle(img, (x0, y0), (x1, y1), color, 2)
        boxes += 1
        extra = ", ".join(x for x in (c.kind, c.col_meta.get("meta_key"), c.col_meta.get("format")) if x)
        _text(img, f"{c.name} ({extra})", x0 + 3, y0 + 14, color, 0.45)
    _legend(img, tint is not None)
    return img, boxes


def _text(img, s: str, x: int, y: int, color, scale: float) -> None:
    s = "".join(ch if ch.isascii() else "?" for ch in s)       # 내장 글꼴은 ASCII 만
    cv2.putText(img, s, (int(x), int(max(10, y))), cv2.FONT_HERSHEY_SIMPLEX, scale, (255, 255, 255), 3, cv2.LINE_AA)
    cv2.putText(img, s, (int(x), int(max(10, y))), cv2.FONT_HERSHEY_SIMPLEX, scale, color, 1, cv2.LINE_AA)


def _dashed(img, p0, p1, color, dash: int = 10) -> None:
    (x0, y0), (x1, y1) = p0, p1
    n = max(1, int(max(abs(x1 - x0), abs(y1 - y0)) // dash))
    for i in range(0, n, 2):
        a = (int(x0 + (x1 - x0) * i / n), int(y0 + (y1 - y0) * i / n))
        b = (int(x0 + (x1 - x0) * min(i + 1, n) / n), int(y0 + (y1 - y0) * min(i + 1, n) / n))
        cv2.line(img, a, b, color, 2)


def _legend(img, printed: bool = False) -> None:
    y = 24
    for kind in sorted(CELL_KINDS):
        cv2.rectangle(img, (8, y - 12), (22, y + 2), COLORS[kind], -1)
        _text(img, kind, 28, y, (40, 40, 40), 0.45)
        y += 20
    _dashed(img, (8, y - 5), (22, y - 5), SPLIT_COLOR, 4)
    _text(img, "split (no printed rule)", 28, y, (40, 40, 40), 0.45)
    if printed:
        y += 20
        cv2.rectangle(img, (8, y - 12), (22, y + 2), PRINT_COLOR, -1)
        _text(img, "print layer (binarized)", 28, y, (40, 40, 40), 0.45)

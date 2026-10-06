"""같은 날 섞여 쓰이는 다른 인쇄 판의 템플릿 만들기 (tasks/0006 4.6, 단계 4).

  minedocscan template variant <기존 템플릿 폴더> --scan FILE --page N --name NAME [--out-dir DIR]

기존 판의 열·행·필드 정의는 그대로 두고 **표마다 괘선만 새 스캔에서 다시 잡는다.**
  새 기준 그림  그 쪽을 기존 판에 맞춰 편 그림. 호모그래피는 표 영역(괘선 범위 + TABLE_MARGIN px) 밖의 특징점(머리·제목)만으로
                구한다 — 기준 이미지 쪽에서 표 영역을 가린 ORB 특징점. 쪽 전체로 구하면 RANSAC 이 표 쪽으로 타협해 표를 기존 판
                자리로 끌어오고 머리를 옮긴다 (다시 잡은 괘선이 판 B 의 실제 자리와 2–16 px 다르고, 표 밖 필드의 bbox 도 맞지 않는다).
  괘선          표마다 기존 괘선 둘레의 창에서 괘선을 잡고(imaging/grid.detect_grid_roi), 기존 괘선마다 ±min(MAX_SHIFT, 이웃 표의
                가장 가까운 괘선까지의 절반) 안에서 가장 가까운 괘선과 짝짓는다 — 창이 이웃 표의 괘선을 품지 않게 (합성 양식은 두 표
                사이가 50 px). 짝이 없는 괘선이 있는 표, 인쇄되지 않은 나눔 선(split_ys·split_xs)이 있는 표(다시 잡을 수 없다)는
                기존 괘선을 그대로 두고 사람이 고치도록 알린다.
  그 밖         열·행·필드·handler·role·format·메타 키는 그대로. 표 밖 필드의 bbox 도 그대로 (머리에 맞춰 편 그림이므로 같은 좌표계)
                — template preview 로 확인한다. family 는 기존 판의 것, 없으면 기존 판의 이름. 새 판에는 concurrent: true.
                print_image 는 복사하지 않는다 — 새 판의 인쇄 층은 그 판으로 적재된 쪽으로 따로 만든다.

기존 판의 파일은 고치지 않는다: 기존 판에 적을 두 줄(family, concurrent: true)을 안내한다.
새 기준 그림은 현장 스캔이다 — 출력 폴더가 git 작업 트리 안이면 거절한다. 이미 있는 폴더도 거절한다 (덮어쓰지 않는다).
옆 템플릿(기존 판의 templates 폴더, 출력 폴더의 부모)이 이미 쓰는 이름도 거절한다 — 이름이 둘이면 사이트 팩이 읽히지 않는다.
읽을 수 없는 기존 판(YAML, 이름 없음, 기준 이미지 없음·깨짐)은 한 줄로 거절하고 template check 를 안내한다.
요약에는 값·이름이 없다: 표 이름, 괘선의 수와 움직인 폭, 인라이어 수.
"""
from __future__ import annotations

import copy
import re
from pathlib import Path

import cv2
import numpy as np
import yaml

from ..forms.template import Template, TemplateError
from ..imaging.align import MIN_INLIERS, warp_to_template
from ..imaging.grid import detect_grid_roi
from ..imaging.io import imwrite, load_page
from ..review.export import inside_git_tree

TABLE_MARGIN = 60          # 표 영역 = 괘선 범위 + 이만큼 (px) — 그 안의 특징점은 호모그래피에 쓰지 않는다 (4.6)
MAX_SHIFT = 40             # 괘선을 짝짓는 거리의 상한 (px) — 판 B 의 표는 8–12 px 아래였다 (tasks/0006 1절)
NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")


class VariantError(ValueError):
    """거절 (한 줄): 저장소 안, 이미 있는 폴더, 특징점 모자람 …"""


def make_variant(template_dir: str | Path, scan: str | Path, page: int, name: str, out_dir: str | Path | None = None,
                 dpi: int = 200, damaged: str = "fail") -> dict:
    """새 판의 폴더(template.yaml + reference.png)를 만들고 요약을 돌려준다. out_dir 기본: 기존 템플릿 폴더 옆의 <name>."""
    tdir = Path(template_dir)
    path = tdir / "template.yaml" if tdir.is_dir() else tdir
    if not path.is_file():
        raise VariantError(f"template.yaml 이 없습니다: {path}")
    if not NAME_RE.match(name):
        raise VariantError(f"--name 은 영문·숫자·밑줄로: {name!r}")
    check_hint = f"minedocscan template check {tdir}"
    try:
        tpl = Template(path)
    except TemplateError as e:
        raise VariantError(f"기존 템플릿에 오류가 있습니다 — {check_hint}: {str(e).splitlines()[0]}") from e
    except (KeyError, TypeError, AttributeError) as e:          # 이름이 없는 YAML, 맨 위가 항목들이 아님 …
        raise VariantError(f"기존 템플릿을 읽을 수 없습니다 ({type(e).__name__}) — {check_hint}") from e
    if name == tpl.name:
        raise VariantError(f"새 판의 이름이 기존 판과 같습니다: {name}")
    if not tpl.regions:
        raise VariantError(f"{tpl.name}: 표가 없는 템플릿입니다 — 판은 표의 괘선을 다시 잡는 것이다")
    out = Path(out_dir) if out_dir is not None else tpl.dir.parent / name
    taken = _template_names(tpl.dir.parent, out.parent)
    if name in taken:                                            # 사이트 팩이 읽히지 않는다 (이름은 사이트 팩 안에서 하나)
        raise VariantError(f"이름 {name} 은 이미 다른 템플릿이 씁니다: {taken[name]}/ — 다른 --name 으로")
    if inside_git_tree(out):
        raise VariantError(f"{out} 은 git 작업 트리 안입니다. 새 판의 기준 이미지는 현장 스캔이므로 저장소 밖의 사이트 팩에 둡니다")
    if out.exists():
        raise VariantError(f"이미 있습니다: {out} (덮어쓰지 않습니다)")
    try:
        ref = tpl.reference
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise VariantError(f"기준 이미지를 읽을 수 없습니다: {tpl.spec.get('reference_image')} — {check_hint}") from e
    try:
        gray = load_page(scan, page, dpi, damaged)
    except (KeyError, OSError, ValueError, RuntimeError) as e:
        raise VariantError(f"스캔을 읽을 수 없습니다: {str(e).splitlines()[0] if str(e) else type(e).__name__}") from e

    H, inliers = header_homography(gray, ref, tpl.regions)
    if H is None or inliers < MIN_INLIERS:
        raise VariantError(f"표 영역 밖의 특징점으로 정합하지 못했습니다 (인라이어 {inliers} < {MIN_INLIERS}) — 머리·제목이 보이는 "
                           "깨끗한 쪽을 고릅니다")
    warped = warp_to_template(gray, H, ref.shape)

    spec = copy.deepcopy(tpl.spec)
    # 이웃 표까지의 간격은 기존 판의 괘선으로 잰다 — 앞에서 다시 잡은 표의 괘선으로 재면 결과가 template.yaml 의 표 순서에 달린다
    existing = copy.deepcopy(tpl.spec["regions"])
    tables = []
    for i, reg in enumerate(spec["regions"]):
        tables.append(_redetect(warped, reg, [r for j, r in enumerate(existing) if j != i]))
    family = tpl.family or tpl.name
    spec.update(name=name, reference_image="reference.png", family=family, concurrent=True)
    spec.pop("print_image", None)
    out.mkdir(parents=True)
    try:
        imwrite(out / "reference.png", warped)
        (out / "template.yaml").write_text(yaml.safe_dump(spec, allow_unicode=True, sort_keys=False), encoding="utf-8")
        Template(out / "template.yaml")                              # 다시 읽어 검증한다 (키는 기존 판 그대로다)
    except (TemplateError, OSError, ValueError) as e:
        for p in out.iterdir():
            p.unlink()
        out.rmdir()
        raise VariantError(f"새 판의 템플릿을 만들 수 없습니다: {str(e).splitlines()[0]}") from e
    add = [] if tpl.family else [f"family: {family}"]
    if not tpl.concurrent:
        add.append("concurrent: true")
    return {"template": tpl.name, "variant": name, "out": str(out), "inliers": inliers, "family": family,
            "tables": tables, "fix_by_hand": [t["region"] for t in tables if not t["redetected"]],
            "existing_needs": add, "existing": str(path)}


def _template_names(*dirs: Path) -> dict[str, str]:
    """폴더들 바로 아래의 */template.yaml 이 쓰는 이름 → 폴더 이름. 읽을 수 없는 템플릿은 건너뛴다 (그 오류는 template check 가)."""
    out: dict[str, str] = {}
    for d in dict.fromkeys(dirs):
        for p in sorted(d.glob("*/template.yaml")) if d.is_dir() else []:
            try:
                spec = yaml.safe_load(p.read_text(encoding="utf-8"))
            except (OSError, ValueError, yaml.YAMLError):
                continue
            n = spec.get("name") if isinstance(spec, dict) else None
            if isinstance(n, str):
                out.setdefault(n, p.parent.name)
    return out


def header_homography(gray: np.ndarray, ref: np.ndarray, regions: list[dict], ratio: float = 0.75):
    """쪽 → 기준 이미지의 호모그래피를 표 영역(괘선 범위 + TABLE_MARGIN) 밖의 특징점만으로. 돌려주는 값: (H | None, 인라이어 수).
    기준 이미지 쪽은 표 영역을 가린 마스크로 특징점을 뽑고, 그래도 기준 점이 표 영역에 드는 짝(가장자리)은 버린다.
    ORB·비율 검정·RANSAC 의 설정은 imaging/align.py 와 같다."""
    h, w = ref.shape[:2]
    mask = np.full((h, w), 255, np.uint8)
    for reg in regions:
        x0, y0, x1, y1 = _table_box(reg, TABLE_MARGIN, w, h)
        mask[y0:y1, x0:x1] = 0
    orb = cv2.ORB_create(nfeatures=6000, scaleFactor=1.2, nlevels=8, edgeThreshold=15, patchSize=31)
    k1, d1 = orb.detectAndCompute(gray, None)
    k2, d2 = orb.detectAndCompute(ref, mask)
    if d1 is None or d2 is None:
        return None, 0
    knn = cv2.BFMatcher(cv2.NORM_HAMMING).knnMatch(d1, d2, k=2)
    good = [m for m, n in (p for p in knn if len(p) == 2) if m.distance < ratio * n.distance]
    good = [m for m in good if mask[min(h - 1, int(k2[m.trainIdx].pt[1])), min(w - 1, int(k2[m.trainIdx].pt[0]))]]
    if len(good) < 12:
        return None, 0
    src = np.float32([k1[m.queryIdx].pt for m in good]).reshape(-1, 1, 2)
    dst = np.float32([k2[m.trainIdx].pt for m in good]).reshape(-1, 1, 2)
    H, inl = cv2.findHomography(src, dst, cv2.RANSAC, 4.0)
    return H, (int(inl.sum()) if inl is not None else 0)


def _table_box(reg: dict, margin: int, w: int, h: int) -> tuple[int, int, int, int]:
    ys, xs = reg["grid"]["ys"], reg["grid"]["xs"]
    return max(0, min(xs) - margin), max(0, min(ys) - margin), min(w, max(xs) + margin), min(h, max(ys) + margin)


def _radius(lines: list[int], span: tuple[int, int], others: list[tuple[list[int], tuple[int, int]]]) -> float:
    """짝짓는 거리: min(MAX_SHIFT, 이웃 표의 가장 가까운 괘선까지의 절반). 이웃 표 = 다른 축의 범위가 겹치는 표
    (나란히 놓인 표의 괘선은 창에 들어오지 않는다)."""
    r = float(MAX_SHIFT)
    for o_lines, o_span in others:
        if o_span[0] <= span[1] and span[0] <= o_span[1]:
            r = min(r, min(abs(a - b) for a in lines for b in o_lines) / 2)
    return r


def _redetect(warped: np.ndarray, reg: dict, others: list[dict]) -> dict:
    """표 하나의 괘선을 다시 잡아 reg["grid"] 의 ys·xs 를 바꾼다 (짝이 다 있을 때만). 요약 한 줄분을 돌려준다.
    others: 다른 표들 — 기존 판의 괘선 그대로 (다시 잡기 전의 것)."""
    g = reg["grid"]
    ys, xs = list(g["ys"]), list(g["xs"])
    out = {"region": reg.get("name"), "lines": len(ys) + len(xs), "redetected": False, "max_shift": None, "reason": None}
    if g.get("split_ys") or g.get("split_xs"):
        out["reason"] = "인쇄되지 않은 나눔 선(split_ys·split_xs)이 있어 다시 잡을 수 없습니다"
        return out
    ry = _radius(ys, (min(xs), max(xs)), [(o["grid"]["ys"], (min(o["grid"]["xs"]), max(o["grid"]["xs"]))) for o in others])
    rx = _radius(xs, (min(ys), max(ys)), [(o["grid"]["xs"], (min(o["grid"]["ys"]), max(o["grid"]["ys"]))) for o in others])
    h, w = warped.shape[:2]
    roi = (max(0, int(min(xs) - rx)), max(0, int(min(ys) - ry)), min(w, int(max(xs) + rx) + 1), min(h, int(max(ys) + ry) + 1))
    found_y, found_x = detect_grid_roi(warped, roi)
    new_y, miss_y = _pair(ys, found_y, ry)
    new_x, miss_x = _pair(xs, found_x, rx)
    if miss_y or miss_x:
        out["reason"] = f"짝이 없는 괘선 {miss_y + miss_x}개 (가로 {miss_y}, 세로 {miss_x})"
        return out
    g["ys"], g["xs"] = new_y, new_x
    out["redetected"] = True
    out["max_shift"] = max(abs(a - b) for a, b in zip([*ys, *xs], [*new_y, *new_x], strict=True))
    return out


def _pair(lines: list[int], found: list[int], radius: float) -> tuple[list[int], int]:
    """기존 괘선마다 radius 안의 가장 가까운 잡은 괘선. 짝이 없거나 두 괘선이 한 괘선과 짝지어지면(순서가 바뀌면) 그 수를 센다."""
    out, miss = [], 0
    for y in lines:
        near = [f for f in found if abs(f - y) <= radius]
        if not near:
            miss += 1
            out.append(y)
            continue
        out.append(min(near, key=lambda f: (abs(f - y), f)))
    miss += sum(b <= a for a, b in zip(out, out[1:], strict=False))        # 오름차순이 아니면 그만큼 짝이 틀렸다
    return out, miss


def format_summary(r: dict) -> str:
    lines = [f"새 판을 만들었습니다: {r['out']} (판 {r['variant']}, 기존 판 {r['template']}, 계열 {r['family']})",
             f"  기준 그림: 표 영역 밖의 특징점으로 기존 판에 맞춰 편 쪽 (인라이어 {r['inliers']})"]
    for t in r["tables"]:
        if t["redetected"]:
            lines.append(f"  {t['region']}: 괘선 {t['lines']}개를 다시 잡았습니다 (가장 많이 움직인 것 {t['max_shift']} px)")
        else:
            lines.append(f"  {t['region']}: 사람이 고칠 것 — {t['reason']}. 기존 판의 괘선을 그대로 두었습니다")
    lines.append("표 밖 필드의 bbox 는 그대로입니다 — minedocscan template preview 로 확인합니다 (새 판의 폴더로, --scan 으로 그 쪽 위에).")
    if r["existing_needs"]:
        lines.append(f"기존 판의 template.yaml ({r['existing']}) 에 적을 줄 (이 명령은 기존 판을 고치지 않습니다 — 적기 전에는 "
                     "계열에 동시 판이 하나뿐이라 사이트 팩이 읽히지 않습니다):")
        lines += [f"  {x}" for x in r["existing_needs"]]
    lines.append("새 판의 인쇄 층은 그 판으로 적재된 쪽으로 따로 만듭니다 (template print-layer). 기준 그림은 현장 스캔입니다 — "
                 "저장소에 넣지 마세요.")
    return "\n".join(lines)

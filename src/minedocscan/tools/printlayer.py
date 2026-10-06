"""인쇄 층 만들기 (tasks/0006 단계 2, 4.2): `minedocscan template print-layer <템플릿 폴더>` → <템플릿 폴더>/print.png + 요약.

  쪽 고르기  그 템플릿으로 분류된 쪽(loaded, classified_only)을 날짜별로 고르게 — 날짜순으로 돌아가며 한 장씩(날짜 안에서는
             문서·쪽 순서, 날짜를 모르는 쪽은 맨 뒤의 한 묶음), 최대 max_pages 장. 같은 DB 면 같은 쪽이다.
  쪽 펴기    loaded: WORK_ROOT 의 정합 그림, 없으면 원본을 doc_page.render_dpi 로 렌더링해 doc_page.homography 로 다시 편다
             (imaging/align.warp_to_template — 정합과 같은 함수라 저장된 정합 그림과 바이트까지 같다. imaging/hires.py 는
             300 dpi·CUBIC 이라 쓰지 않는다).
             classified_only: 칸 정의가 없는 템플릿의 쪽은 정합하지 않았다(호모그래피가 없다) — 여기서 원본을 settings.dpi 로
             렌더링해 정합한다. 표가 없어 괘선 오차가 없으므로 인라이어(MIN_INLIERS)만 본다. 못 미치는 쪽은 뺀다.
  만들기     imaging/printlayer.estimate (쪽마다 erode 3×3 → 화소마다 밝기의 백분위). 2장 미만이면 거절, 5장 미만이면 경고.

DB 에는 쓰지 않는다 (읽기 전용으로 연다) — runner·report·regress 의 수치는 그대로다. 쓰는 파일은 print.png 하나뿐이고
template.yaml 은 고치지 않는다: `print_image: print.png` 는 사람이 적는다 (파일보다 키가 먼저 있으면 템플릿 오류로 사이트 팩
전체가 읽히지 않는다). 템플릿은 검증 없이 읽는다 — 키가 가리키는 파일이 아직 없어도 만들 수 있다.

인쇄 층은 현장 데이터다: 늘 같은 자리에 같은 글씨로 쓰는 칸(이름·서명)은 쪽이 많아도 잔상이 남는다 — 저장소 안에는 쓰지 않는다 (거절).
요약에는 값·이름이 없다: 쪽·날짜의 수, 뺀 쪽의 이유별 수, 인쇄 화소의 비율, 해시, 손으로 쓰는 칸 중 인쇄에 덮인 비율이 큰 칸의 이름
(template check 와 같은 표기 — 행 키는 장비 번호일 수 있어 찍지 않는다).
"""
from __future__ import annotations

import json
import sqlite3
from collections import Counter
from pathlib import Path

import numpy as np

from ..config import Settings
from ..forms.template import Template
from ..imaging import printlayer
from ..imaging.align import MIN_INLIERS, align_to_template, warp_to_template
from ..imaging.io import imread_gray, imwrite, load_page, resolve_source
from ..review.export import inside_git_tree
from ..store.db import SchemaVersionError, open_db_readonly
from .tpltools import handwritten_boxes

STATUSES = ("loaded", "classified_only")        # 인쇄 층을 만드는 쪽: 그 템플릿으로 분류된 쪽 (정합 실패·오류는 빼고)
MAX_PAGES = 40                                  # 기본 최대 쪽 수 (tasks/0006 9절)
MIN_PAGES = 2                                   # 이보다 적으면 거절 — 한 장의 "백분위"는 그 쪽 그대로다 (손글씨까지)
WARN_PAGES = 5                                  # 이보다 적으면 경고하고 만든다 (4.2)
TOP_COVERED = 10                                # 요약에 싣는 "인쇄에 덮인 칸"의 수


class PrintLayerError(ValueError):
    """거절 (한 줄): 저장소 안, 쪽이 모자람, DB 없음 …"""


def build(template_dir: str | Path, settings: Settings, max_pages: int = MAX_PAGES, percentile: float = 75,
          out: str | Path | None = None, allow_in_repo: bool = False, con: sqlite3.Connection | None = None) -> dict:
    """인쇄 층을 만들어 out(기본 <템플릿 폴더>/print.png)에 쓰고 요약을 돌려준다. con: 읽을 DB (없으면 설정의 DB 를 읽기 전용으로)."""
    tdir = Path(template_dir)
    path = tdir / "template.yaml" if tdir.is_dir() else tdir
    if not path.is_file():
        raise PrintLayerError(f"template.yaml 이 없습니다: {path}")
    check_hint = f"minedocscan template check {tdir}"
    try:
        tpl = Template(path, validate=False)
    except (KeyError, TypeError, AttributeError) as e:          # 이름이 없는 YAML … — 오류 목록은 template check 가
        raise PrintLayerError(f"템플릿을 읽을 수 없습니다 ({type(e).__name__}) — {check_hint}") from e
    out = Path(out) if out is not None else tpl.dir / "print.png"
    if out.suffix.lower() != ".png":                          # 손실 없는 형식만 — 파일의 화소 = 요약의 print_sha (4.2)
        raise PrintLayerError(f"인쇄 층은 PNG 로 씁니다 (손실 압축이면 파일의 해시가 요약의 print_sha 와 달라진다): {out.name}")
    if inside_git_tree(out) and not allow_in_repo:            # 계산하기 전에 거절한다
        raise PrintLayerError(f"{out} 은 git 작업 트리 안입니다. 인쇄 층에는 늘 같은 자리에 쓰는 손글씨(이름·서명)의 잔상이 "
                              "남습니다 — 저장소 밖의 사이트 팩에 씁니다 (합성 팩이면 --allow-in-repo)")
    if not 0 <= percentile <= 100:
        raise PrintLayerError(f"--percentile 은 0–100: {percentile}")
    if max_pages < MIN_PAGES:
        raise PrintLayerError(f"인쇄 층은 {MIN_PAGES}장 이상으로 만듭니다 (--max-pages {max_pages})")
    try:
        ref_shape = tpl.reference.shape
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise PrintLayerError(f"기준 이미지를 읽을 수 없습니다: {tpl.spec.get('reference_image')} — {check_hint}") from e
    try:                                                      # 요약의 칸 — 쪽을 펴기 전에 (표·필드 정의가 깨졌으면 계산하지 않는다)
        boxes = handwritten_boxes(tpl)
    except (KeyError, TypeError, AttributeError, ValueError) as e:
        raise PrintLayerError(f"표·필드 정의를 읽을 수 없습니다 ({type(e).__name__}) — {check_hint}") from e

    own = con is None
    try:
        if own:
            con = open_db_readonly(settings.resolved_db_url)
        rows = candidate_pages(con, tpl.name)
    except FileNotFoundError as e:
        raise PrintLayerError(f"{e} — 먼저 run 으로 쪽을 분류합니다 (칸 정의가 없는 템플릿도 분류는 된다)") from e
    except SchemaVersionError as e:
        raise PrintLayerError(str(e).splitlines()[0] + " — run --fresh 로 다시 만든 DB 에서") from e
    except sqlite3.DatabaseError as e:                        # 빈 파일, DB 가 아닌 파일 …
        raise PrintLayerError(f"DB 를 읽을 수 없습니다 ({type(e).__name__}: {e}) — 먼저 run 으로 쪽을 분류합니다") from e
    finally:
        if own and con is not None:
            con.close()

    pages, used, how, skipped = [], [], Counter(), Counter()
    for r in pick_order(rows):
        if len(pages) >= max_pages:
            break
        img, why = page_image(r, tpl, settings, ref_shape)
        if img is None:
            skipped[why] += 1
            continue
        pages.append(img)
        used.append(r)
        how[why] += 1
    if len(pages) < MIN_PAGES:
        raise PrintLayerError(f"{tpl.name}: 인쇄 층을 만들 쪽이 {len(pages)}장뿐입니다 ({MIN_PAGES}장 이상 — 이 양식으로 분류된 쪽 "
                              f"{len(rows)}장" + (f", 뺀 쪽 {_kv(skipped)}" if skipped else "") + ")")
    layer = printlayer.estimate(pages, percentile)
    del pages
    imwrite(out, layer)

    b = printlayer.binary(layer)
    cov = printlayer.coverage(b, [bb for _n, bb in boxes])
    order = sorted(range(len(boxes)), key=lambda i: (-cov[i], i))
    covered = [{"cell": boxes[i][0], "coverage": round(cov[i], 4)} for i in order[:TOP_COVERED] if cov[i] > 0]
    warnings = []
    if len(used) < WARN_PAGES:
        warnings.append(f"쪽이 {len(used)}장뿐입니다 ({WARN_PAGES}장 미만) — 손글씨의 잔상이 남을 수 있습니다. "
                        "쪽이 쌓이면 다시 만드세요")
    return {"template": tpl.name, "out": str(out), "pages": len(used), "candidates": len(rows),
            "dates": len({r["work_date"] for r in used if r["work_date"]}),
            "undated": sum(not r["work_date"] for r in used),
            "by_source": dict(sorted(how.items())), "skipped": dict(sorted(skipped.items())),
            "percentile": percentile, "print_ratio": round(float(b.mean()), 4), "sha": printlayer.sha(layer),
            "warnings": warnings, "hint": _hint(tpl, out), "covered": covered}


def candidate_pages(con: sqlite3.Connection, template_name: str) -> list[sqlite3.Row]:
    """그 템플릿으로 분류된 쪽 (loaded, classified_only) 과 원본을 찾는 데 필요한 것."""
    return con.execute(
        "SELECT p.page_id, p.document_id, p.page_no, p.status, p.work_date, p.aligned_image, p.homography, p.render_dpi, "
        "d.source_path, d.source_rel FROM doc_page p JOIN doc_document d ON p.document_id = d.document_id "
        f"WHERE p.template_name = ? AND p.status IN ({', '.join('?' * len(STATUSES))})",
        (template_name, *STATUSES)).fetchall()


def pick_order(rows: list) -> list:
    """날짜별로 고르게: 날짜순으로 돌아가며 한 장씩. 날짜 안에서는 (문서, 쪽) 순서, 날짜를 모르는 쪽은 맨 뒤의 한 묶음."""
    groups: dict = {}
    for r in rows:
        groups.setdefault(r["work_date"] or None, []).append(r)
    keys = sorted(k for k in groups if k is not None) + ([None] if None in groups else [])
    for k in keys:
        groups[k].sort(key=lambda r: (r["document_id"], r["page_no"]))
    out, i = [], 0
    while any(i < len(groups[k]) for k in keys):
        out += [groups[k][i] for k in keys if i < len(groups[k])]
        i += 1
    return out


def page_image(r, tpl: Template, settings: Settings, ref_shape: tuple[int, ...]) -> tuple[np.ndarray | None, str]:
    """쪽 하나를 템플릿 좌표로 편 그림과 어떻게 얻었는지 (aligned | rewarped | aligned_now), 못 얻으면 (None, 이유)."""
    if r["status"] == "loaded" and r["aligned_image"]:
        p = Path(settings.work_root) / r["aligned_image"]
        if p.is_file():
            try:
                img = imread_gray(p)
            except (OSError, ValueError):
                img = None
            if img is not None and img.shape == tuple(ref_shape[:2]):
                return img, "aligned"
    if r["status"] == "loaded" and not r["homography"]:
        return None, "no_homography"
    src = resolve_source(r["source_path"], r["source_rel"], settings.archive_root)
    if src is None:
        return None, "no_source"
    dpi = (r["render_dpi"] or settings.dpi) if r["status"] == "loaded" else settings.dpi
    try:
        gray = load_page(src, r["page_no"], dpi, settings.damaged_pdf)
    except (OSError, ValueError, KeyError, RuntimeError):
        return None, "unreadable"
    if r["status"] == "loaded":
        return rewarp(gray, r["homography"], ref_shape), "rewarped"
    ar = align_to_template(gray, tpl.reference, [], ref_features=tpl.features)   # 표가 없다 — 괘선 오차 없이 인라이어만
    if ar.n_inliers < MIN_INLIERS:
        return None, "few_inliers"
    return ar.warped, "aligned_now"


def rewarp(gray: np.ndarray, homography_json: str, ref_shape: tuple[int, ...]) -> np.ndarray:
    """렌더링한 쪽을 저장된 호모그래피(doc_page.homography, 반올림하지 않은 JSON)로 다시 편다 — 파이프라인의 정합 그림과 같다."""
    return warp_to_template(gray, json.loads(homography_json), ref_shape)


def _hint(tpl: Template, out: Path) -> str | None:
    """print_image 키의 안내. 명령은 template.yaml 을 고치지 않는다."""
    in_dir = out.resolve().parent == tpl.dir.resolve()
    if "print_image" not in tpl.spec:
        if in_dir:
            return f"켜려면 template.yaml 에 `print_image: {out.name}` 를 적습니다 (이 명령은 template.yaml 을 고치지 않습니다)"
        return "켜려면 이 파일을 템플릿 폴더에 두고 template.yaml 에 `print_image: <파일 이름>` 를 적습니다"
    if tpl.print_path is not None and in_dir and tpl.print_path.resolve() == out.resolve():
        return None                                  # 이미 이 파일을 쓴다
    return f"template.yaml 의 print_image 는 다른 파일을 가리킵니다: {tpl.spec.get('print_image')!s}"


def _kv(d: dict) -> str:
    return ", ".join(f"{k} {v}" for k, v in sorted(d.items()))


def format_summary(r: dict) -> str:
    """요약의 글 (값·이름 없이)."""
    lines = [f"인쇄 층을 만들었습니다: {r['out']}",
             f"  양식 {r['template']} — 쪽 {r['pages']}장 (분류된 쪽 {r['candidates']}장 중), 날짜 {r['dates']}일"
             + (f" + 날짜 모름 {r['undated']}장" if r["undated"] else "") + f", 백분위 {r['percentile']}",
             f"  얻은 법: {_kv(r['by_source'])}" + (f" · 뺀 쪽: {_kv(r['skipped'])}" if r["skipped"] else ""),
             f"  인쇄 화소 {r['print_ratio']:.2%}, print_sha {r['sha']}"]
    if r["covered"]:
        lines.append("  인쇄에 덮인 손글씨 칸 (큰 순서 — 칸 안의 인쇄이거나 잔상, template preview --print 로 봅니다):")
        lines += [f"    {c['cell']}: {c['coverage']:.1%}" for c in r["covered"]]
    lines += [f"경고: {w}" for w in r["warnings"]]
    if r["hint"]:
        lines.append(r["hint"])
    lines.append("저장소에 넣지 마세요 — 늘 같은 자리에 쓰는 손글씨의 잔상이 남을 수 있습니다.")
    return "\n".join(lines)

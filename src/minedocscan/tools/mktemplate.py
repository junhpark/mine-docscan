"""새 양식의 템플릿 뼈대 만들기.

빈 양식(또는 깨끗한 스캔 한 장)에서 표 괘선을 검출해 template.yaml 초안과 reference.png 를 쓴다.
검출된 것은 기하(괘선 좌표)뿐이다. 열 이름·kind·행 키·핸들러는 사람이 채운다 (docs/SITE_PACK.md).
표가 여러 개인 양식은 `template add-region` 으로 표를 하나씩 더한다 (tasks/0006 단계 6):

  minedocscan template add-region <템플릿 폴더> --roi x0,y0,x1,y1 --name NAME [--role ROLE] [--header-rows N]

  괘선      그 영역에서 잡는다(imaging/grid.detect_grid_roi — init --roi 와 같은 함수, 새 임계값 없음). 템플릿에 인쇄 층
            (print_image)이 있으면 그것에서, 없으면 기준 이미지에서 — 채워진 스캔에서 잡으면 손글씨의 세로획이 괘선으로 섞여
            나온다 (4.5). 인쇄되지 않은 나눔 선(split_ys·split_xs)은 잡지 않는다 — 사람이 적는다.
  뼈대      열(idx, col_<i>, handwritten_text)과 행(row, row_<i>) — init 과 같은 뼈대. --role 이면 그 역할이 요구하는 열 이름·형식의
            자리표시 (meter: 뒤의 열 셋이 start·end·total 에 reading, shifts: 마지막 열이 time_range, tally: integer, activities:
            글자 칸). 요약에 자리표시가 붙은 열을 적는다 — template check 는 그것이 맞는 열인지 보지 않는다.
  쓰기      template.yaml 의 다른 부분과 주석은 건드리지 않는다: regions 블록의 끝(다음 최상위 키 바로 앞)에 글자로 끼워 넣는다
            — fields 가 regions 뒤에 있어 파일 끝에 붙이면 필드가 된다. `regions: []` 이면 그 줄을 블록으로, regions 가 없으면 파일
            끝에 더한다. 의존성은 PyYAML 뿐이다. 쓴 뒤 다시 읽어 표가 하나 늘고 나머지가 그대로인지 보고, 아니면 원래 파일로 되돌린다.
  거절      같은 이름의 표, 템플릿 폴더가 git 작업 트리 안(현장 양식이다 — --allow-in-repo), 쪽 밖의 영역, 괘선이 모자람,
            목록이 아닌 regions. 쓰기는 임시 파일 → os.replace 라 쓰다 실패해도 원래 파일이 남는다.
  잔상      쪽이 적은 인쇄 층(5장 미만)에는 같은 자리에 쓴 값의 잔상이 남아 세로 괘선으로 잡힐 수 있다 (백분위를 보간하던 때 합성
            운행일보 4쪽 층의 계기 표 — 보간 없이는 3·4쪽 층도 맞다, test_add_region). 판이 꼭 반씩 섞인 50 백분위 층에서는 괘선이
            빠진다. 요약의 괘선 수를 인쇄된 표와 맞춰 본다.
            템플릿은 검증 없이 읽는다 — 열을 채우기 전의 뼈대는 아직 템플릿 오류일 수 있다 (template check 로 본다).
"""
from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

import yaml

from ..forms.template import METER_SLOTS, ROLES, Template, TemplateError
from ..imaging.align import rotate_upright
from ..imaging.grid import detect_grid, detect_grid_roi
from ..imaging.io import imwrite, load_pages
from ..review.export import inside_git_tree

NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")


def init_template(image: str | Path, name: str, templates_dir: str | Path, roi: tuple[int, int, int, int] | None = None,
                  header_rows: int = 1, page: int = 1, dpi: int = 200, handler: str = "generic",
                  title: str | None = None, overwrite: bool = False, rotate: int = 0) -> Path:
    """rotate: 기준 이미지를 시계 방향으로 이만큼 돌려 세운다 (돌아간 스캔으로 템플릿을 만들 때 — tasks/0007 4.4).
    파이프라인은 쪽을 이 기준 이미지의 방향으로 세운다. roi 는 세운 그림의 좌표다."""
    gray = None
    for no, g in load_pages(image, dpi):
        if no == page:
            gray = g
            break
    if gray is None:
        raise ValueError(f"{image} 에 {page} 페이지가 없습니다")
    gray = rotate_upright(gray, rotate)
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
    if len(xs) >= 2 and len(ys) - 1 - header_rows >= 1:
        spec["regions"].append(region_skeleton("main", ys, xs, header_rows))
    out_dir.mkdir(parents=True, exist_ok=True)
    imwrite(out_dir / "reference.png", gray)
    target.write_text(yaml.safe_dump(spec, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return target


# ── 표 하나의 뼈대 ─────────────────────────────────────────────────────────────
def region_skeleton(name: str, ys: list[int], xs: list[int], header_rows: int = 1, role: str | None = None) -> dict:
    """괘선(ys, xs)으로 표 하나의 뼈대: 열(idx, col_<i>, handwritten_text)과 행(row, row_<i>). 열·행의 이름과 kind 는 사람이 고친다.
    role 이면 그 역할이 요구하는 열 이름·형식의 자리표시로 (forms/template._role_problems 가 보는 것):
      meter       뒤의 열 셋(열이 둘이면 start·end)이 start·end·total 에 format reading — 앞에 열이 더 있으면 인쇄된 이름 칸
                  (col_<i>, printed). 열이 하나고 행이 둘 이상이면 세로 표 — 행 키 start·end·total, 그 열에 reading
      shifts      마지막 열이 range(handwritten_number, format time_range). 열이 둘 이상이면 첫 열은 인쇄된 근무 구분(shift)
      tally       열 전부 handwritten_number, format integer (구분·장소의 인쇄된 열, 행 메타 item·place, 열 메타 shift·subtotal 은 사람이)
      activities  열 전부 handwritten_text
    """
    n_cols, n_rows = len(xs) - 1, len(ys) - 1 - header_rows
    # kind 를 고친다: printed | handwritten_text | handwritten_number | checkmark | signature
    cols = [{"idx": i, "name": f"col_{i}", "kind": "handwritten_text"} for i in range(n_cols)]
    rows = [{"row": i, "key": f"row_{i}"} for i in range(n_rows)]
    if role == "meter":
        vertical = n_cols == 1 and n_rows >= 2
        if vertical:
            cols[0].update(name="reading", kind="handwritten_number", format="reading")
            for r, slot in zip(rows, ("start", "end", "total"), strict=False):
                r["key"] = slot
        else:                                        # 뒤의 열부터: 앞의 열은 인쇄된 이름 칸 (합성 운행일보의 계기 표가 그렇다)
            k = min(len(METER_SLOTS), n_cols)
            for c in cols[:n_cols - k]:
                c.update(kind="printed")
            for c, slot in zip(cols[n_cols - k:], METER_SLOTS[:k], strict=True):
                c.update(name=slot, kind="handwritten_number", format="reading")
    elif role == "shifts":
        if n_cols >= 2:
            cols[0].update(name="shift", kind="printed")
        cols[-1].update(name="range", kind="handwritten_number", format="time_range")
    elif role == "tally":
        for c in cols:
            c.update(kind="handwritten_number", format="integer")
    reg: dict = {"name": name}
    if role is not None:
        reg["role"] = role
    reg.update({"grid": {"ys": list(ys), "xs": list(xs)}, "header_rows": header_rows, "columns": cols, "rows": rows})
    return reg


# ── template add-region ───────────────────────────────────────────────────────
class AddRegionError(ValueError):
    """거절 (한 줄): 같은 이름의 표, 저장소 안, 쪽 밖의 영역, 괘선이 모자람, 글자로 끼워 넣을 수 없는 regions …"""


def add_region(template_dir: str | Path, roi: tuple[int, int, int, int], name: str, role: str | None = None,
               header_rows: int = 1, allow_in_repo: bool = False) -> dict:
    """그 영역의 괘선을 잡아 표 하나의 뼈대를 template.yaml 의 regions 끝에 글자로 끼워 넣고 요약을 돌려준다.
    괘선은 인쇄 층(print_image)이 있으면 그것에서, 없으면 기준 이미지에서 잡는다. 템플릿은 검증 없이 읽는다."""
    tdir = Path(template_dir)
    path = tdir / "template.yaml" if tdir.is_dir() else tdir
    if inside_git_tree(path) and not allow_in_repo:           # 읽기 전에 거절한다
        raise AddRegionError(f"{path.parent} 은 git 작업 트리 안입니다. 현장 양식의 템플릿(머리글의 이름·차량번호)은 저장소 밖의 사이트 팩에 "
                             "둡니다 (합성 팩이면 --allow-in-repo)")
    if not path.is_file():
        raise AddRegionError(f"template.yaml 이 없습니다: {path}")
    if not NAME_RE.match(name or "") or name == "fields":
        raise AddRegionError(f"--name 은 영문·숫자·밑줄로, 'fields' 는 안 된다 (표 밖 필드의 자리): {name!r}")
    if role is not None and role not in ROLES:
        raise AddRegionError(f"알 수 없는 --role {role!r} (가능: {', '.join(ROLES)})")
    if header_rows < 0:
        raise AddRegionError(f"--header-rows 는 0 이상: {header_rows}")
    check_hint = f"minedocscan template check {path.parent}"
    original = path.read_bytes()
    try:
        tpl = Template(path, validate=False)
    except (yaml.YAMLError, TemplateError, KeyError, TypeError, AttributeError) as e:
        raise AddRegionError(f"템플릿을 읽을 수 없습니다 ({type(e).__name__}) — {check_hint}") from e
    if not isinstance(tpl.regions, list):           # `regions: 5`, 매핑 … — 블록으로 끼워 넣을 자리가 아니다
        raise AddRegionError(f"regions 의 모양을 알 수 없습니다 (목록이 아니라 {type(tpl.regions).__name__}) — {check_hint}")
    names = [r.get("name") if isinstance(r, dict) else None for r in tpl.regions]
    if name in names:
        raise AddRegionError(f"{tpl.name}: 같은 이름의 표가 이미 있습니다: {name} (다른 --name 으로, 고치려면 template.yaml 에서)")

    source = "print" if tpl.print_path is not None else "reference"
    bad_print = tpl.print_problems() if source == "print" else []
    if bad_print:                                    # 없는 파일, 기준 이미지와 다른 크기(좌표계가 다르다) …
        raise AddRegionError(f"{bad_print[0]} — {check_hint}")
    try:
        gray = tpl.print_layer if source == "print" else tpl.reference
    except (OSError, ValueError, KeyError, TypeError) as e:
        what = f"인쇄 층 {tpl.spec.get('print_image')}" if source == "print" else f"기준 이미지 {tpl.spec.get('reference_image')}"
        raise AddRegionError(f"{what} 을 읽을 수 없습니다 — {check_hint}") from e
    h, w = gray.shape[:2]
    x0, y0, x1, y1 = roi
    if not (0 <= x0 < x1 <= w and 0 <= y0 < y1 <= h):
        raise AddRegionError(f"--roi {x0},{y0},{x1},{y1} 이 쪽({w}×{h}) 밖이거나 비었습니다 (템플릿 좌표 x0,y0,x1,y1)")
    ys, xs = detect_grid_roi(gray, roi)
    if len(xs) < 2 or len(ys) - 1 - header_rows < 1:
        raise AddRegionError(f"괘선이 모자랍니다: 가로 {len(ys)}개, 세로 {len(xs)}개 (머리 행 {header_rows}) — 표 둘레를 조금 넉넉히 "
                             "--roi 로 주거나, 인쇄 층(template print-layer)에서 잡습니다")
    region = region_skeleton(name, ys, xs, header_rows, role)

    text = original.decode("utf-8")
    new_text = _insert_region(text, region)
    _replace_bytes(path, new_text.encode("utf-8"))   # 쓰다 실패해도(디스크 …) 원래 파일은 그대로
    try:                                             # 다시 읽어 표가 하나 늘고 나머지가 그대로인지 — 아니면 원래 파일로
        before = yaml.safe_load(text) or {}
        after = yaml.safe_load(new_text)
        ok = (isinstance(after, dict) and {k: v for k, v in after.items() if k != "regions"}
              == {k: v for k, v in before.items() if k != "regions"}
              and (after.get("regions") or []) == [*(before.get("regions") or []), region])
        Template(path, validate=False)
    except Exception:                                # 무엇으로 실패하든 원래 파일로 되돌린다
        ok = False
    if not ok:
        _replace_bytes(path, original)
        raise AddRegionError(f"표를 글자로 끼워 넣지 못했습니다 — 원래 파일로 되돌렸습니다 ({path}). regions 를 블록 형식"
                             "(`regions:` 다음 줄부터 `- name: …`)으로 바꾼 뒤 다시 합니다")

    notes = []
    if role is not None and tpl.handler != "usage":
        notes.append(f"role 은 usage 핸들러의 표에만 씁니다 — 이 템플릿의 handler 는 {tpl.handler!r} (template check 가 오류로 낸다)")
    if tpl.concurrent and tpl.family:
        notes.append(f"이 판은 계열 {tpl.family!r} 의 동시 판(concurrent)입니다 — 같은 계열의 다른 동시 판에도 같은 표(같은 --name·"
                     "--role·--header-rows, 같은 열·행과 그 메타)를 더해야 사이트 팩이 읽힙니다 (template check 는 판 하나만 본다)")
    if source == "reference":
        notes.append("인쇄 층이 없어 기준 이미지에서 잡았습니다 — 기준 이미지가 채워진 스캔이면 손글씨의 세로획이 괘선으로 섞일 수 "
                     "있습니다 (template print-layer 로 인쇄 층을 만들고 print_image 를 적은 뒤에 잡는 것이 낫다)")
    return {"template": tpl.name, "path": str(path), "region": name, "role": role, "source": source,
            "roi": [x0, y0, x1, y1], "lines": {"ys": len(ys), "xs": len(xs)}, "header_rows": header_rows,
            "columns": len(region["columns"]), "rows": len(region["rows"]),
            "placeholders": _placeholders(region), "notes": notes}


def _placeholders(region: dict) -> list[str]:
    """요약에 적는 자리표시: 이름을 붙인 열(`idx 이름 kind[/format]`)과 이름을 붙인 행 키 — col_<i>·row_<i> 는 빼고.
    자리표시의 이름이다 (현장 값이 아니다). template check 는 이 이름이 맞는 열에 붙었는지 보지 않는다."""
    out = [f"열 {c['idx']} {c['name']} {c['kind']}" + (f"/{c['format']}" if c.get("format") else "")
           for c in region["columns"] if c["name"] != f"col_{c['idx']}" or c["kind"] != "handwritten_text"]
    out += [f"행 {r['row']} {r['key']}" for r in region["rows"] if r["key"] != f"row_{r['row']}"]
    return out


def _replace_bytes(path: Path, data: bytes) -> None:
    """같은 폴더의 임시 파일에 쓰고 os.replace 로 바꾼다 — 쓰다 실패하면 원래 파일이 그대로 남는다 (사람이 고친 템플릿이다)."""
    tmp = path.with_name(f".{path.name}.add-region.tmp")
    try:
        tmp.write_bytes(data)
        shutil.copymode(path, tmp)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _insert_region(text: str, region: dict) -> str:
    """template.yaml 의 글자에 표 하나를 끼워 넣은 글자. 다른 줄(주석 포함)은 그대로 둔다.
      `regions:` 블록         블록의 끝 = 다음 최상위 키 바로 앞 (그 키 바로 위의 빈 줄·맨 앞 주석은 그 키의 것으로 본다)
      `regions: []`·null·빈 값  그 줄을 `regions:` 로 바꾸고 바로 아래에 (줄 끝의 주석은 둔다)
      regions 가 없다          파일 끝에 `regions:` 와 함께
    그 밖(흐름 형식의 목록 …)은 AddRegionError."""
    nl = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith(("\n", "\r")):
        lines[-1] += nl
    head = next((i for i, ln in enumerate(lines) if re.match(r"regions\s*:", ln)), None)

    def block(indent: str) -> list[str]:
        dumped = yaml.safe_dump([region], allow_unicode=True, sort_keys=False, default_flow_style=False)
        return [indent + ln + nl for ln in dumped.splitlines()]

    if head is None:
        return "".join([*lines, "regions:" + nl, *block("")])
    m = re.match(r"regions\s*:\s*(.*?)\s*$", lines[head].rstrip("\r\n"))
    rest = m.group(1) if m else ""
    value, comment = (rest.split("#", 1)[0].strip(), rest[rest.index("#"):]) if "#" in rest else (rest.strip(), "")
    if value in ("[]", "null", "~", ""):
        end = len(lines)
        for i in range(head + 1, len(lines)):
            ln = lines[i]
            if ln.strip() and not ln[0].isspace() and not ln.startswith(("#", "-")):
                end = i                                # 다음 최상위 키
                break
        items = [i for i in range(head + 1, end) if re.match(r"\s*- ", lines[i]) and not lines[i].lstrip().startswith("#")]
        if value and items:                            # `regions: []` 인데 아래에 목록이 있다 — YAML 오류다
            raise AddRegionError("regions 의 모양을 알 수 없습니다 — template check 로 봅니다")
        if value:                                      # [] · null → 블록으로
            lines[head] = "regions:" + (f"  {comment}" if comment else "") + nl
        indent = re.match(r"(\s*)- ", lines[items[0]]).group(1) if items else ""
        while end - 1 > head and (not lines[end - 1].strip() or lines[end - 1].startswith("#")):
            end -= 1                                   # 다음 키 바로 위의 빈 줄·주석은 그 키의 것
        return "".join([*lines[:end], *block(indent), *lines[end:]])
    raise AddRegionError("regions 가 흐름 형식([...])이라 글자로 끼워 넣을 수 없습니다 — 블록 형식(`regions:` 다음 줄부터 "
                         "`- name: …`)으로 바꾼 뒤 다시 합니다")


def format_summary(r: dict) -> str:
    """요약의 글 (값·이름 없이 — 표 이름과 수만)."""
    src = "인쇄 층" if r["source"] == "print" else "기준 이미지"
    lines = [f"표를 더했습니다: {r['region']}" + (f" (role {r['role']})" if r["role"] else "") + f" → {r['path']}",
             f"  {src}에서 괘선을 잡았습니다: 가로 {r['lines']['ys']}개, 세로 {r['lines']['xs']}개 (영역 {','.join(map(str, r['roi']))}) "
             f"→ 머리 행 {r['header_rows']}, 데이터 행 {r['rows']}개, 열 {r['columns']}개",
             "  열 이름·kind·format, 행 키·메타는 자리표시입니다 — 채운 뒤 minedocscan template check 와 template preview "
             "(--print) 로 확인합니다. 통과할 때까지 이 사이트 팩으로 run 하지 않습니다 (템플릿 오류 하나로 전부 멈춘다).",
             "  check 는 자리표시가 맞는 열에 붙었는지 보지 않습니다 — 아래의 자리를 preview --print 의 인쇄된 머리와 맞춰 봅니다.",
             "  인쇄되지 않은 나눔 선(칸 안의 인쇄된 줄마다 행을 나누는 grid.split_ys·split_xs)은 잡지 않습니다 — 사람이 적습니다."]
    if r["source"] == "print":
        lines.append("  인쇄 층을 쪽이 적게(print-layer 가 5장 미만이면 경고한다) 만들었으면 같은 자리에 쓴 값의 잔상이 괘선으로 섞일 수 "
                     "있습니다 — 가로·세로 수를 인쇄된 표와 맞춰 보고, 다르면 쪽이 쌓인 뒤 층을 다시 만들어 잡거나 남는 괘선을 지웁니다. "
                     "모자라면 판이 반씩 섞인 50 백분위 층일 수 있습니다 (--max-pages 를 홀수로 다시 만든다).")
    if r["placeholders"]:
        lines.append("  자리표시: " + ", ".join(r["placeholders"]))
    lines += [f"참고: {n}" for n in r["notes"]]
    return "\n".join(lines)

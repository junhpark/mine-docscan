"""설명서의 부록 "명령 목록" (tasks/0009 4.8) — argparse 의 동작(actions)에서 만든다.

    python scripts/cli_reference.py            # docs/manual/명령.md 를 쓴다
    python scripts/cli_reference.py --check    # 지금의 것과 다르면 종료 코드 1 (시험이 본다)

format_help() 를 쓰지 않는다 — 줄바꿈이 터미널 폭과 파이썬 판에 따라 달라진다. 명령·하위 명령마다 위치 인자와 선택, 기본값,
고를 수 있는 값을 표로 적는다. 도움말의 글은 cli.py 의 것 그대로다 (한 곳).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "manual" / "명령.md"
HEAD = """# 부록 — 명령 목록

`minedocscan <명령> [하위 명령] [선택]`. 이 쪽은 `scripts/cli_reference.py` 가 프로그램의 명령 정의에서 만든다 — 손으로 고치지 않는다
(시험이 지금의 명령과 같은지 본다). 쓰임새와 순서는 설명서의 각 장에, 규칙은 `docs/` 의 문서에.

여러 명령이 같이 받는 선택 (`[공통]`): `--config`(설정 파일), `--site`(사이트 팩), `--archive-root`(보관 폴더), `--work-root`(작업 폴더),
`--db-url`, `--json`(요약을 JSON 으로).
"""
COMMON = {"--config", "--site", "--archive-root", "--work-root", "--db-url", "--json"}


def _cell(text) -> str:
    return " ".join(str(text).split()).replace("|", "\\|").replace("<", "&lt;")   # <site> 같은 자리표시가 HTML 태그로 사라지지 않게


def _default(a: argparse.Action) -> str:
    if isinstance(a, (argparse._StoreTrueAction, argparse._StoreFalseAction)) or a.default in (None, argparse.SUPPRESS):
        return ""
    if isinstance(a.default, (list, tuple)):
        return ", ".join(map(str, a.default))
    return str(a.default)


def _actions(p: argparse.ArgumentParser) -> list[str]:
    rows = []
    for a in p._actions:
        if isinstance(a, (argparse._HelpAction, argparse._SubParsersAction)) or a.help == argparse.SUPPRESS:
            continue
        if a.option_strings and set(a.option_strings) & COMMON:
            continue
        name = ", ".join(f"`{o}`" for o in a.option_strings) if a.option_strings else f"`{a.dest}`"
        if a.option_strings and a.metavar:
            name += f" `{a.metavar}`"
        choices = ", ".join(f"`{c}`" for c in a.choices) if a.choices else ""
        rows.append(f"| {name} | {_cell(a.help or '')} | {_cell(_default(a))} | {choices} |")
    return rows


def _common(p: argparse.ArgumentParser) -> bool:
    return any(set(a.option_strings) & COMMON for a in p._actions)


def render() -> str:
    from minedocscan.cli import build_parser

    ap = build_parser()
    out = [HEAD]
    sub = next(a for a in ap._actions if isinstance(a, argparse._SubParsersAction))
    helps = {c.dest: c.help for c in sub._choices_actions}
    for name in sorted(sub.choices):
        p = sub.choices[name]
        out.append(f"## `{name}`\n\n{_cell(helps.get(name) or '')}" + (" `[공통]`" if _common(p) else "") + "\n")
        rows = _actions(p)
        if rows:
            out += ["| 인자·선택 | 뜻 | 기본값 | 고를 수 있는 값 |", "|---|---|---|---|", *rows, ""]
        nested = next((a for a in p._actions if isinstance(a, argparse._SubParsersAction)), None)
        if nested is not None:
            nhelps = {c.dest: c.help for c in nested._choices_actions}
            for sname in sorted(nested.choices):
                sp = nested.choices[sname]
                out.append(f"### `{name} {sname}`\n\n{_cell(nhelps.get(sname) or '')}" + (" `[공통]`" if _common(sp) else "") + "\n")
                srows = _actions(sp)
                if srows:
                    out += ["| 인자·선택 | 뜻 | 기본값 | 고를 수 있는 값 |", "|---|---|---|---|", *srows, ""]
    return "\n".join(out).rstrip() + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true", help="쓰지 않고 지금의 docs/manual/명령.md 와 같은지만 본다")
    ap.add_argument("--out", type=Path, default=OUT)
    a = ap.parse_args(argv)
    text = render()
    if a.check:
        same = a.out.is_file() and a.out.read_text(encoding="utf-8") == text
        print("명령.md 는 지금의 명령과 같습니다" if same else f"{a.out} 가 지금의 명령과 다릅니다 — python scripts/cli_reference.py 로 다시 만드십시오")
        return 0 if same else 1
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(text, encoding="utf-8")
    print(f"{a.out} 를 썼습니다")
    return 0


if __name__ == "__main__":
    sys.exit(main())

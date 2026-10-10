"""사용 설명서(docs/manual/)를 한 장짜리 manual.html 로 (tasks/0009 4.8) — 그림을 data: 로 담는다. 설치 묶음에 들어가고 CI 의 산출물이다.

    python scripts/manual_html.py --out dist/manual.html

차례는 docs/manual/README.md 의 링크 순서다 (README 자체가 첫머리). 장 사이의 링크(`2-설치.md#…`)는 이 파일 안의 자리로 바꾼다.
docs/ 의 다른 문서(SITE_PACK.md …)를 가리키는 링크는 글로만 남긴다 — 묶음에는 저장소가 없다. PDF 는 브라우저의 인쇄로.
같은 설명서면 같은 바이트다 (만든 시각을 넣지 않는다). markdown 꾸러미가 필요하다 (`pip install -e ".[docs]"`) — 런타임·묶음에는 들어가지 않는다.
"""
from __future__ import annotations

import argparse
import base64
import html
import posixpath
import re
import sys
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
MANUAL = ROOT / "docs" / "manual"
MEDIA = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".svg": "image/svg+xml"}
CSS = """
:root { color-scheme: light; }
body { font-family: "Malgun Gothic", "Apple SD Gothic Neo", "Noto Sans KR", sans-serif; max-width: 980px; margin: 0 auto; padding: 16px;
       line-height: 1.6; color: #1d1d1f; background: #fff; }
h1 { border-bottom: 2px solid #333; padding-bottom: 4px; margin-top: 2.4em; }
h2 { border-bottom: 1px solid #ccc; margin-top: 1.8em; }
code { background: #f3f3f3; padding: 1px 4px; border-radius: 3px; font-size: 92%; }
pre { background: #f6f6f6; padding: 10px; overflow-x: auto; border: 1px solid #e3e3e3; }
pre code { background: none; padding: 0; }
table { border-collapse: collapse; margin: 1em 0; display: block; overflow-x: auto; }
th, td { border: 1px solid #ccc; padding: 4px 8px; vertical-align: top; }
th { background: #f0f0f0; }
img { max-width: 100%; border: 1px solid #ddd; }
nav.toc { border: 1px solid #ddd; padding: 8px 16px; background: #fafafa; }
span.doc { border-bottom: 1px dotted #888; }
section.chapter { break-before: page; }
section.chapter:first-of-type { break-before: auto; }
@media print { body { max-width: none; } pre, table, img { break-inside: avoid; } a { color: inherit; text-decoration: none; } }
"""


def chapters(manual: Path = MANUAL) -> list[Path]:
    """README.md 의 차례 — README 다음에 README 가 가리키는 .md 를 나온 순서대로 (한 번씩)."""
    readme = manual / "README.md"
    out = [readme]
    for target in re.findall(r"\]\(([^)#\s]+\.md)(?:#[^)]*)?\)", readme.read_text(encoding="utf-8")):
        p = (manual / target).resolve()
        if p.parent == manual.resolve() and p.is_file() and p not in out:
            out.append(p)
    return out


def anchor(path: Path) -> str:
    return "ch-" + re.sub(r"[^0-9A-Za-z가-힣]+", "-", path.stem).strip("-")


def gh_slug(text: str) -> str:
    """GitHub 이 제목에 붙이는 자리 이름 (작은 글자, 낱말 글자·붙임표·빈칸만, 빈칸 → 붙임표) — 설명서의 `#…` 링크가 GitHub 에서도 이 파일에서도 맞게."""
    return re.sub(r"[^\w\- ]", "", text.strip().lower()).replace(" ", "-")


def _image(manual: Path, src: str) -> str | None:
    p = (manual / src).resolve()
    if not p.is_file() or manual.resolve() not in p.parents or p.suffix.lower() not in MEDIA:
        return None
    return f"data:{MEDIA[p.suffix.lower()]};base64," + base64.b64encode(p.read_bytes()).decode("ascii")


def _chapter(p: Path, ids: dict[str, str], manual: Path) -> tuple[str, str]:
    """장 하나 → (제목, HTML). 제목의 자리는 '<장>-<GitHub 의 자리 이름>', 링크·그림을 이 파일 안으로."""
    import markdown

    prefix = ids[p.name]
    md = markdown.Markdown(extensions=["tables", "fenced_code", "toc", "sane_lists"],
                           extension_configs={"toc": {"slugify": lambda value, sep: f"{prefix}-{gh_slug(value)}"}})
    text = md.convert(p.read_text(encoding="utf-8"))
    title = re.search(r"<h1[^>]*>(.*?)</h1>", text, re.S)

    def link(m: re.Match) -> str:
        href = html.unescape(m.group(1))
        target, _, frag = href.partition("#")
        frag = unquote(frag)
        if not target and (not frag or frag.startswith(f"{prefix}-")):  # toc 가 만든 자리
            return m.group(0)
        if not target:                                                 # 같은 장 안
            return f'href="#{prefix}-{html.escape(frag)}"'
        if re.match(r"(https?|mailto):", target):
            return m.group(0)
        q = (p.parent / unquote(target)).resolve()                    # 이름만 견주면 다른 README.md 가 설명서의 차례로 간다
        name = q.name
        if q.parent == manual.resolve() and name in ids:
            return f'href="#{ids[name]}-{html.escape(frag)}"' if frag else f'href="#{ids[name]}"'
        repo = posixpath.normpath(posixpath.join("docs/manual", unquote(target)))
        return 'href="#" data-doc="' + html.escape(repo) + '"'            # 저장소의 다른 문서 — 묶음에는 없다

    def img(m: re.Match) -> str:
        data = _image(manual, unquote(html.unescape(m.group(1))))
        if data is None:
            raise SystemExit(f"그림이 없거나 설명서 폴더 밖이다: {m.group(1)} ({p.name})")
        return f'src="{data}"'

    text = re.sub(r'src="([^"]*)"', img, re.sub(r'href="([^"]*)"', link, text))
    # 저장소의 다른 문서(묶음에는 없다)는 링크가 아니라 글로 — 어디에 있는지는 마우스를 올리면
    text = re.sub(r'<a href="#" data-doc="([^"]*)">(.*?)</a>', r'<span class="doc" title="저장소의 \1">\2</span>', text, flags=re.S)
    return (title.group(1) if title else html.escape(p.stem)), text


def render(manual: Path = MANUAL, version: str | None = None) -> str:
    if version is None:
        sys.path.insert(0, str(ROOT / "src"))
        from minedocscan import __version__ as version
    parts = chapters(manual)
    ids = {p.name: anchor(p) for p in parts}
    body, toc = [], []
    for p in parts:
        title, text = _chapter(p, ids, manual)
        toc.append(f'<li><a href="#{ids[p.name]}">{title}</a></li>')
        body.append(f'<section class="chapter" id="{ids[p.name]}">\n{text}\n</section>')
    return ("<!doctype html>\n<html lang=\"ko\">\n<head>\n<meta charset=\"utf-8\">\n"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
            f"<title>minedocscan 사용 설명서 {html.escape(version)}</title>\n<style>{CSS}</style>\n</head>\n<body>\n"
            f"<p>minedocscan {html.escape(version)} — 사용 설명서. PDF 로는 브라우저의 인쇄(Ctrl+P → PDF 로 저장)로 만든다.</p>\n"
            f"<nav class=\"toc\"><strong>차례</strong><ol>{''.join(toc)}</ol></nav>\n" + "\n".join(body) + "\n</body>\n</html>\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, required=True, help="쓸 manual.html")
    ap.add_argument("--manual", type=Path, default=MANUAL, help="설명서 폴더 (기본 docs/manual)")
    a = ap.parse_args(argv)
    try:
        import markdown  # noqa: F401
    except ImportError:
        print("markdown 꾸러미가 없습니다 — pip install -e \".[docs]\" (또는 pip install markdown)", file=sys.stderr)
        return 2
    text = render(a.manual)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(text, encoding="utf-8", newline="\n")
    print(f"{a.out} ({len(text.encode('utf-8')):,} 바이트, 장 {len(chapters(a.manual))})")
    return 0


if __name__ == "__main__":
    sys.exit(main())

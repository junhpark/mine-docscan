"""제3자 라이선스 목록 (tasks/0009 4.3 나) — 설치되는 패키지에 들어가지 않는다 (scripts/).

  python scripts/licenses.py --wheels DIR [--out THIRD_PARTY_NOTICES.txt] [--check]
      설치 묶음의 wheels/ (win_amd64) 에 든 바퀴 그대로가 목록이다. 앱 전용 파이썬(embeddable)과 pip 을 더한다.
  python scripts/licenses.py --installed [EXTRA ...] [--out …] [--check]
      지금 환경에 설치된 minedocscan(과 EXTRA — 예: postgres)의 의존성 닫힘 (우분투 CI — 윈도우와 닫힘이 조금 다르다: tzdata …).

- 배포판마다 이름·판·라이선스·누리집과 라이선스 파일의 본문 → THIRD_PARTY_NOTICES.txt. 본문은 바이트로 읽는다 (UTF-8 이 아니면 latin-1 —
  pypdfium2 의 freetype.txt 가 그렇다).
- **검사는 배포판 단위의 SPDX 표현으로** (메타데이터의 License-Expression, 없으면 저장소의 대응표 MAPPED). 허용 목록(ALLOWED) 밖이거나
  알 수 없는 배포판이 있으면 --check 는 종료 코드 1.
- 바퀴 안에 같이 든 것(numpy 의 OpenBLAS·GCC 런타임, OpenCV 의 LICENSE-3RD-PARTY, PDFium 의 의존성들)은 목록과 본문에 넣되 막지 않는다.
  바퀴에 본문이 없는 것(psycopg-binary 의 libpq·OpenSSL, 앱 전용 파이썬, pip)은 저장소에 둔 본문(scripts/licenses/)으로.
- [train]·[dev]·[docs] 는 넣지 않는다 (묶음에 들어가지 않는다). 이 프로그램 자신의 문구는 사람이 정한다 — 저장소의 NOTICE (첫 줄에 자리만).
- 종료 코드: 0 (--check 면 막는 것이 없다), 1 (--check 에서 막는 것이 있다), 2 (바퀴가 없다 — 빈 목록이 통과하지 않게).
"""
from __future__ import annotations

import argparse
import re
import sys
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
TEXTS = HERE / "licenses"

ALLOWED = {"MIT", "BSD-2-Clause", "BSD-3-Clause", "Apache-2.0", "PSF-2.0", "Python-2.0", "ISC", "Zlib", "0BSD", "CC0-1.0",
           "MPL-2.0", "PostgreSQL", "LGPL-2.1-only", "LGPL-2.1-or-later", "LGPL-3.0-only", "LGPL-3.0-or-later"}

# 메타데이터에 SPDX 표현(License-Expression)이 없고 License 글도 SPDX 가 아닌 배포판 — 바퀴의 License 글·분류자와 라이선스 파일을
# 사람이 읽고 정했다. (License 글이 SPDX 표현이면 — "MIT" — 그것을 쓴다: openpyxl, et-xmlfile, pyyaml)
MAPPED = {
    "opencv-python-headless": "Apache-2.0",              # "Apache 2.0" (LICENSE.txt). 같이 든 것은 LICENSE-3RD-PARTY.txt (ffmpeg 등)
    "pypdfium2": "Apache-2.0 OR BSD-3-Clause",           # "BSD-3-Clause, Apache-2.0, dependency licenses" (LICENSES/)
    "numpy": "BSD-3-Clause",                             # 2.0 전 판(하한 1.26)은 License-Expression 이 없고 License 글이 본문 전체다.
                                                         # 2.0 부터는 메타데이터의 표현(BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0)
}

# 바퀴에 본문이 없는 것 — 저장소의 본문 (scripts/licenses/). 그 바퀴에 같이 든 라이브러리 파일이 이름에 맞을 때만 넣는다
# (윈도우의 psycopg-binary 는 libpq·OpenSSL, 리눅스는 krb5 … 도)
EXTRA_TEXTS = {
    "psycopg-binary": [("libpq", "PostgreSQL", "libpq-postgresql.txt", r"libpq"),
                       ("OpenSSL 3", "Apache-2.0", "openssl.txt", r"libssl|libcrypto"),
                       ("MIT Kerberos", "MIT", "krb5.txt", r"krb5")],
}
APP_EXTRAS = [  # 묶음에 같이 들어가는 것 (바퀴가 아니다)
    ("python (embeddable)", "PSF-2.0", "https://www.python.org/", "python.txt"),
    ("pip", "MIT", "https://pip.pypa.io/", "pip.txt"),
]


# ── SPDX 표현 ─────────────────────────────────────────────────────────────
_TOKEN = re.compile(r"\s*(\(|\)|[A-Za-z0-9.\-+:]+)")


def parse_spdx(expr: str):
    """SPDX 표현 → 나무 ('or'|'and', [자식…]) | ('with', 라이선스, 예외) | 라이선스. 꼴이 틀리면 ValueError."""
    toks = []
    pos = 0
    expr = expr.strip()
    while pos < len(expr):
        m = _TOKEN.match(expr, pos)
        if not m:
            raise ValueError(f"SPDX 표현이 아닙니다: {expr!r}")
        toks.append(m.group(1))
        pos = m.end()
    i = 0

    def peek():
        return toks[i] if i < len(toks) else None

    def take():
        nonlocal i
        if i >= len(toks):
            raise ValueError(f"SPDX 표현이 아닙니다: {expr!r}")
        i += 1
        return toks[i - 1]

    def atom():
        t = take() if peek() is not None else None
        if t is None or t.upper() in ("AND", "OR", "WITH") or t == ")":
            raise ValueError(f"SPDX 표현이 아닙니다: {expr!r}")
        if t == "(":
            node = disj()
            if take() != ")":
                raise ValueError(f"SPDX 표현이 아닙니다: {expr!r}")
            return node
        if peek() is not None and peek().upper() == "WITH":
            take()
            return ("with", t, take())
        return t

    def conj():
        kids = [atom()]
        while peek() is not None and peek().upper() == "AND":
            take()
            kids.append(atom())
        return kids[0] if len(kids) == 1 else ("and", kids)

    def disj():
        kids = [conj()]
        while peek() is not None and peek().upper() == "OR":
            take()
            kids.append(conj())
        return kids[0] if len(kids) == 1 else ("or", kids)

    if not toks:
        raise ValueError("빈 SPDX 표현")
    tree = disj()
    if i != len(toks):
        raise ValueError(f"SPDX 표현이 아닙니다: {expr!r}")
    return tree


def allowed(expr: str) -> bool:
    """허용 목록으로 쓸 수 있나: AND 는 모두, OR 는 하나. WITH 예외는 바탕 라이선스로 본다."""
    def ok(node) -> bool:
        if isinstance(node, str):
            return node in ALLOWED
        if node[0] == "with":
            return node[1] in ALLOWED
        kids = node[1]
        return all(ok(k) for k in kids) if node[0] == "and" else any(ok(k) for k in kids)

    return ok(parse_spdx(expr))


# ── 배포판 ────────────────────────────────────────────────────────────────
def norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


@dataclass
class Dist:
    name: str
    version: str
    expression: str | None                     # SPDX (메타데이터 또는 MAPPED) — None 이면 알 수 없다
    homepage: str = ""
    texts: list[tuple[str, bytes]] = field(default_factory=list)     # (파일 이름, 본문 바이트)
    bundled: list[tuple[str, str, bytes]] = field(default_factory=list)   # 바퀴에 본문이 없어 저장소에서 (이름, 라이선스, 본문)
    binaries: list[str] = field(default_factory=list)          # 바퀴 안에 같이 든 라이브러리 파일 (.dll·.so — 이름만, 해시 꼬리 없이)
    system: bool = False                                       # --installed: OS 의 꾸러미가 설치했다 (데비안은 본문을 /usr/share/doc 로 옮긴다)
    missing: list[str] = field(default_factory=list)           # 메타데이터의 License-File 가운데 찾지 못한 것 (막는다)

    @property
    def ok(self) -> bool:
        try:
            return (self.expression is not None and allowed(self.expression)
                    and bool(self.texts or self.bundled or self.system) and not self.missing)
        except ValueError:
            return False


def _meta_fields(text: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for line in text.split("\n\n", 1)[0].splitlines():
        if ": " in line and not line.startswith(" "):
            k, v = line.split(": ", 1)
            out.setdefault(k, []).append(v.strip())
    return out


def _homepage(f: dict[str, list[str]]) -> str:
    if f.get("Home-page"):
        return f["Home-page"][0]
    for v in f.get("Project-URL", []):
        label, _, url = v.partition(", ")
        if label.strip().lower() in ("homepage", "home", "source", "repository", "source code"):
            return url.strip()
    return (f.get("Project-URL") or [", "])[0].partition(", ")[2].strip()


_LICENSE_FILE = re.compile(r"(^|/)(LICEN[CS]E|COPYING|NOTICE|AUTHORS)[^/]*$", re.I)
_BINARY = re.compile(r"\.(dll|so(\.[\d.]+)?|dylib)$", re.I)
_HASH_TAIL = re.compile(r"-[0-9a-f]{8,}(?=\.)", re.I)


def _binaries(paths) -> list[str]:
    """바퀴 안의 같이 든 라이브러리 (파이썬 확장 모듈 .pyd·.cpython-*.so 는 빼고). auditwheel·delvewheel 의 해시 꼬리는 지운다."""
    out = set()
    for n in paths:
        base = str(n).replace("\\", "/").rsplit("/", 1)[-1]
        if _BINARY.search(base) and ".cpython-" not in base and not base.startswith("_"):
            out.add(_HASH_TAIL.sub("", base))
    return sorted(out)


def _expression(name: str, f: dict[str, list[str]]) -> str | None:
    """License-Expression > 저장소의 대응표 > License 글이 SPDX 표현이면 그것. 그 밖에는 None (알 수 없다 — 막는다)."""
    if f.get("License-Expression"):
        return f["License-Expression"][0]
    if norm(name) in MAPPED:
        return MAPPED[norm(name)]
    lic = (f.get("License") or [""])[0].strip()
    try:
        parse_spdx(lic)
        return lic
    except ValueError:
        return None


def _unique(texts: list[tuple[str, bytes]]) -> list[tuple[str, bytes]]:
    """같은 본문은 한 번 (OpenCV 는 같은 파일을 cv2/ 와 dist-info/ 에 둘 다 둔다)."""
    seen, out = set(), []
    for name, b in texts:
        if b not in seen:
            seen.add(b)
            out.append((name, b))
    return out


def _bundled(name: str, binaries: list[str]) -> list[tuple[str, str, bytes]]:
    return [(label, spdx, (TEXTS / fn).read_bytes()) for label, spdx, fn, pat in EXTRA_TEXTS.get(norm(name), [])
            if any(re.search(pat, b, re.I) for b in binaries)]


def from_wheel(path: Path) -> Dist:
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        meta = next(n for n in names if n.endswith(".dist-info/METADATA") and n.count("/") == 1)
        f = _meta_fields(z.read(meta).decode("utf-8", "replace"))
        info = meta.rsplit("/", 1)[0] + "/"
        have = set(names)
        listed, missing = set(), []
        for v in f.get("License-File", []):             # 2.4: dist-info/licenses/<경로>, 2.1(setuptools): dist-info/<이름> 에 납작하게
            got = [c for c in (info + "licenses/" + v, info + v, info + v.rsplit("/", 1)[-1]) if c in have]
            listed |= set(got[:1])
            if not got:
                missing.append(v)
        files = sorted(n for n in names if not n.endswith("/") and
                       (n in listed or n.startswith(info + "licenses/") or (n.startswith(info) and _LICENSE_FILE.search(n))
                        or (_LICENSE_FILE.search(n) and not n.startswith(info))))
        texts = _unique([(n, z.read(n)) for n in files])
    name = f["Name"][0]
    bins = _binaries(names)
    return Dist(name, f["Version"][0], _expression(name, f), _homepage(f), texts, _bundled(name, bins), bins, missing=missing)


def from_installed(dist) -> Dist:
    f = _meta_fields(dist.read_text("METADATA") or "")
    name = f["Name"][0]
    texts = []
    seen = set()
    listed = {v for e in f.get("License-File", []) for v in (e, "licenses/" + e, e.rsplit("/", 1)[-1])}   # 2.1 은 납작하게 (pypdfium2 4.x)
    for p in dist.files or []:
        s = str(p).replace("\\", "/")
        if _LICENSE_FILE.search(s) or "/licenses/" in s or (".dist-info/" in s and s.split(".dist-info/", 1)[1] in listed):
            q = p.locate()
            if q.is_file() and s not in seen:
                seen.add(s)
                texts.append((s, q.read_bytes()))
    bins = _binaries(dist.files or [])
    installer = (dist.read_text("INSTALLER") or "").strip().lower()
    return Dist(name, f["Version"][0], _expression(name, f), _homepage(f), _unique(sorted(texts)), _bundled(name, bins), bins,
                system=installer not in ("", "pip", "uv"))


def wheels_closure(wheels: Path) -> list[Dist]:
    """묶음의 wheels/ 에 든 바퀴 전부 (minedocscan 자신은 빼고). 폴더가 없거나 바퀴가 하나도 없으면 ValueError — 빈 목록이 검사를
    통과하지 않게."""
    found = sorted(wheels.glob("*.whl")) if wheels.is_dir() else []
    if not found:
        raise ValueError(f"바퀴가 없습니다: {wheels}")
    return [d for d in (from_wheel(w) for w in found) if norm(d.name) != "minedocscan"]


def installed_closure(extras: list[str]) -> list[Dist]:
    """설치된 minedocscan(과 extras)의 런타임 의존성 닫힘 — 표식은 지금 환경으로 평가한다."""
    from importlib import metadata

    from packaging.requirements import Requirement

    seen: dict[str, object] = {}
    missing: set[str] = set()
    done: set[tuple[str, frozenset]] = set()             # (배포판, extras) — 같은 것을 두 번 펼치지 않는다 (고리가 있어도 끝난다)
    todo = [("minedocscan", frozenset(extras))]
    while todo:
        name, ex = todo.pop()
        if (norm(name), ex) in done:
            continue
        done.add((norm(name), ex))
        try:
            d = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            missing.add(name)                          # 요구하는데 설치되지 않았다 — 알 수 없는 것으로 (막는다)
            continue
        seen[norm(d.metadata["Name"])] = d
        for r in d.requires or []:
            req = Requirement(r)
            envs = [{"extra": e} for e in (ex or {""})]
            if req.marker is not None and not any(req.marker.evaluate(e) for e in envs):
                continue
            todo.append((req.name, frozenset(req.extras)))
    return ([from_installed(d) for k, d in sorted(seen.items()) if k != "minedocscan"]
            + [Dist(n, "(설치되지 않음)", None) for n in sorted(missing)])


def app_extras(python_embed: Path | None = None, have: set[str] = frozenset()) -> list[Dist]:
    """묶음에 같이 들어가는 것 (바퀴가 아니다). python_embed: 앱 전용 파이썬의 zip — 그 안의 LICENSE.txt(파이썬과 같이 든 DLL 들 —
    OpenSSL·libffi·SQLite·zlib … 의 본문이 들어 있다)를 쓴다. 없으면 저장소의 python.txt (CPython 의 LICENSE). have: 이미 바퀴로 든
    배포판 (pip 이 wheels/ 에 있으면 바퀴의 본문 — 같이 든 것까지 — 을 쓴다)."""
    out = []
    for n, spdx, url, fn in APP_EXTRAS:
        if norm(n) in have:
            continue
        texts = [(fn, (TEXTS / fn).read_bytes())]
        version = ""
        if n.startswith("python") and python_embed is not None:
            with zipfile.ZipFile(python_embed) as z:
                lic = next((x for x in z.namelist() if x.upper() == "LICENSE.TXT"), None)
                if lic is None:
                    raise ValueError(f"앱 전용 파이썬의 zip 에 LICENSE.txt 가 없습니다: {python_embed.name}")
                texts = [(f"{python_embed.name}/{lic}", z.read(lic))]
            m = re.search(r"python-(\d+\.\d+\.\d+)", python_embed.name)
            version = m.group(1) if m else ""
        out.append(Dist(n, version, spdx, url, texts))
    return out


# ── 글 ─────────────────────────────────────────────────────────────────────
def decode(b: bytes) -> str:
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        return b.decode("latin-1")


def notices(dists: list[Dist]) -> str:
    rule = "=" * 78
    out = ["THIRD-PARTY NOTICES — 제3자 소프트웨어", "",
           "이 묶음에는 아래의 제3자 소프트웨어가 들어 있다. 배포판마다 이름·판·라이선스(SPDX)·누리집과 라이선스 파일의 본문.",
           "이 프로그램 자신의 저작권·라이선스 문구는 NOTICE 에 있다.", ""]
    for d in dists:
        out.append(f"- {d.name} {d.version}".rstrip() + f" — {d.expression or '(알 수 없음)'}")
        if d.binaries:
            out.append(f"    같이 든 라이브러리: {', '.join(d.binaries)}")
    for d in dists:
        out += ["", rule, f"{d.name} {d.version}".rstrip(), f"License: {d.expression or '(알 수 없음)'}"]
        if d.homepage:
            out.append(f"Homepage: {d.homepage}")
        if d.binaries:
            out.append(f"Bundled: {', '.join(d.binaries)}")
        out.append(rule)
        for fn, b in d.texts:
            out += ["", f"--- {fn} ---", decode(b).rstrip()]
        for label, spdx, b in d.bundled:
            out += ["", f"--- {label} ({spdx}) — 바퀴에 같이 든 라이브러리, 본문은 scripts/licenses/ ---", decode(b).rstrip()]
        if d.system and not (d.texts or d.bundled):
            out += ["", "(OS 의 꾸러미로 설치되어 본문이 메타데이터에 없다 — 묶음은 바퀴의 본문을 쓴다)"]
    return "\n".join(out) + "\n"


def problems(dists: list[Dist]) -> list[str]:
    """막는 것: 허용 목록 밖, 알 수 없는 라이선스, 라이선스 본문이 하나도 없는 배포판 (목록이 본문 없이 나가지 않게)."""
    out = []
    for d in dists:
        if not d.ok:
            why = d.expression or "라이선스를 알 수 없다"
            if d.expression and not (d.texts or d.bundled or d.system):
                why += " — 라이선스 본문이 없다"
            if d.missing:
                why += f" — 메타데이터가 적은 라이선스 파일을 찾지 못했다: {', '.join(d.missing)}"
            out.append(f"{d.name} {d.version}: {why}")
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="licenses", description=__doc__.split("\n")[0])
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--wheels", type=Path, help="설치 묶음의 wheels/ 폴더")
    src.add_argument("--installed", nargs="*", metavar="EXTRA", help="지금 환경의 minedocscan[EXTRA…] 닫힘")
    ap.add_argument("--out", type=Path, help="THIRD_PARTY_NOTICES.txt 를 쓸 곳")
    ap.add_argument("--python-embed", type=Path, help="앱 전용 파이썬의 zip (그 안의 LICENSE.txt 를 쓴다)")
    ap.add_argument("--check", action="store_true", help="허용 목록 밖·알 수 없는 배포판이 있으면 종료 코드 1")
    a = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):              # 윈도우 콘솔(cp1252·cp949)에서도 한글을 찍다 죽지 않게
        if stream is not None and (stream.encoding or "").lower().replace("-", "") != "utf8":
            stream.reconfigure(encoding="utf-8", errors="replace")
    try:
        dists = wheels_closure(a.wheels) if a.wheels else installed_closure(a.installed or [])
    except ValueError as e:
        print(e, file=sys.stderr)
        return 2
    dists += app_extras(a.python_embed, {norm(d.name) for d in dists})
    if a.out:
        a.out.write_text(notices(dists), encoding="utf-8")
    bad = problems(dists)
    for d in dists:
        print(f"{'ok ' if d.ok else 'NO '} {d.name} {d.version} — {d.expression or '?'}")
    if bad:
        print(f"허용 목록 밖이거나 알 수 없는 라이선스 {len(bad)}개:", *bad, sep="\n  ", file=sys.stderr)
    return 1 if (a.check and bad) else 0


if __name__ == "__main__":
    sys.exit(main())

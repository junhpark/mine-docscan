"""윈도우 오프라인 설치 묶음 `minedocscan-<판>-win64.zip` 을 만든다 (tasks/0009 4.5 가, 0010 4.4·4.5). 인터넷이 되는 곳에서 — CI 의 윈도우 작업.

    python scripts/bundle.py --out dist          묶음 (잠금 bundle.lock 의 바퀴만 — 같은 커밋 + 같은 잠금이면 zip 의 바이트가 같다)
    python scripts/bundle.py lock                판을 올린다: 풀어서(pip download) bundle.lock 과 bundle.toml 의 [build]·[sources] 를 새로 쓴다

들어가는 것 (zip 안의 이름은 ASCII, 맨 위 폴더 하나):
  python-<판>-embed-amd64.zip, pip-<판>-py3-none-any.whl   bundle.toml 의 판·SHA-256 과 같아야 한다 — 다르면 멈춘다
  wheels/                    minedocscan 의 바퀴(이 커밋에서 만든다)와 bundle.lock 의 바퀴 전부 (cp312-win_amd64, 해시 확인).
                             OpenCV 바퀴는 FFmpeg 플러그인(cv2/opencv_videoio_ffmpeg*.dll — LGPL-2.1, 쓰지 않는다)을 빼고 다시 묶는다
  sources/                   소스를 같이 줘야 하는 것(psycopg·psycopg-binary — LGPL-3.0)의 sdist 와 README.txt — bundle.toml 의 [sources]
  install.ps1, uninstall.ps1 scripts/windows/ 의 것을 UTF-8 BOM·CRLF 로 (Windows PowerShell 5.1 은 BOM 이 없으면 한글을 깨뜨린다)
  minedocscan.example.toml   config/ 의 보기
  THIRD_PARTY_NOTICES.txt    scripts/licenses.py --wheels … --sources … --python-embed … --check (허용 목록 밖·소스 조건을 못 채우면 멈춘다)
  manual.html                docs/manual/ 을 한 장으로 (scripts/manual_html.py)
  NOTICE, INSTALL.txt, VERSION
  SHA256SUMS.txt             묶음 안 파일 전부의 해시 (install.ps1 이 먼저 확인한다)
묶음 옆에 bundle-report.json: 크기·sha256, 바퀴 수, 바퀴가 담은 DLL 과 msvcp140.dll 이 필요한 바이너리 (embeddable 파이썬에는 vcruntime 만
있다), 뺀 것, 소스 — 그리고 licenses.txt (licenses.py 가 찍은 줄 — 시험 성적서의 라이선스 절).

같은 바이트 (tasks/0010 4.4): 묶음을 만드는 도구(minedocscan 을 묶는 hatchling, 설명서의 markdown)는 bundle.toml [build] 의 판·해시를
따로 만든 가상 환경에 깔아 쓰고, minedocscan 의 바퀴는 SOURCE_DATE_EPOCH = 커밋 시각으로 (hatchling 이 따른다). zip 은 이름 순서, 시각은
커밋 시각(UTC, 1980 이후), 권한·만든 시스템·압축 수준을 고정해 쓴다 (다시 묶는 OpenCV 바퀴도). 저장소의 글은 .gitattributes 로 LF.
이 도구는 설치되는 패키지에 들어가지 않는다 (scripts/).
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import tomllib
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG = Path(__file__).with_name("bundle.toml")
LOCK = Path(__file__).with_name("bundle.lock")
WINDOWS = Path(__file__).with_name("windows")
SUMS = "SHA256SUMS.txt"
TEXTS = ("install.ps1", "uninstall.ps1", "INSTALL.txt")   # BOM·CRLF 로 (메모장·PowerShell 5.1)
VC_DLLS = ("msvcp140.dll", "msvcp140_1.dll", "msvcp140_2.dll", "vcruntime140.dll", "vcruntime140_1.dll", "concrt140.dll")
GENERATED = "# ── 아래는 python scripts/bundle.py lock 이 쓴다 (손으로 고치지 않는다) ──"
# OpenCV 의 FFmpeg 플러그인 (tasks/0010 4.5): cv2.pyd 는 동영상을 읽을 때만 이 이름으로 찾아 연다 — 이 프로그램은 cv2.VideoCapture 를 쓰지 않는다
FFMPEG = re.compile(r"cv2/opencv_videoio_ffmpeg[^/]*\.dll", re.I)
OPENCV = "opencv-python-headless"
# 소스를 같이 주는 것: 배포판 → sdist 의 프로젝트 (psycopg-binary 는 psycopg_c 로 만든다 — 그 바퀴에 생성된 C 와 libpq·libssl·libcrypto 가 든다)
SOURCES = {"psycopg": "psycopg", "psycopg-binary": "psycopg-c"}
BUILD_TOOLS = ["hatchling", "markdown>=3.5"]          # 묶음을 만드는 도구 — [build] 에 판·해시로 (lock 이 푼다)


class BundleError(RuntimeError):
    """묶음을 만들 수 없다 — 한 줄로."""


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _hex64(v) -> bool:
    return isinstance(v, str) and len(v) == 64 and all(c in "0123456789abcdef" for c in v)


def load_config(path: Path = CONFIG) -> dict:
    cfg = tomllib.loads(path.read_text(encoding="utf-8"))
    for key in ("python", "pip"):
        if not _hex64(cfg[key].get("sha256", "")):
            raise BundleError(f"{path.name} 의 [{key}] sha256 이 SHA-256(소문자 16진수 64자)이 아니다 — 판을 올릴 때 해시를 같이 적는다")
    for name, entry in (cfg.get("sources") or {}).items():
        if not (_hex64(entry.get("sha256")) and entry.get("url") and entry.get("version")):
            raise BundleError(f"{path.name} 의 [sources] {name} 에 version·url·sha256 이 다 있어야 한다 — python scripts/bundle.py lock")
    return cfg


# ── 잠금 ───────────────────────────────────────────────────────────────────
_LOCK_LINE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;]+)\s+--hash=sha256:([0-9a-f]{64})$")


def read_lock(path: Path = LOCK) -> dict[str, tuple[str, str, str]]:
    """bundle.lock → {정규화한 이름: (이름, 판, sha256)}. 줄마다 `이름==판 --hash=sha256:…` (pip 의 requirements 꼴, 줄 잇기 `\\`)."""
    if not path.is_file():
        raise BundleError(f"잠금이 없다: {path.name} — python scripts/bundle.py lock (인터넷이 되는 곳에서)")
    out = {}
    text = path.read_text(encoding="utf-8").replace("\\\n", " ")
    for raw in text.splitlines():
        line = " ".join(raw.split("#", 1)[0].split())
        if not line:
            continue
        m = _LOCK_LINE.match(line)
        if not m:
            raise BundleError(f"{path.name} 의 줄을 읽을 수 없다 (이름==판 --hash=sha256:… 하나씩): {line[:60]}")
        out[norm(m.group(1))] = (m.group(1), m.group(2), m.group(3))
    if not out:
        raise BundleError(f"{path.name} 에 바퀴가 없다")
    return out


def write_lock(entries: list[tuple[str, str, str]], path: Path = LOCK) -> None:
    lines = ["# 설치 묶음의 바퀴 (cp312-win_amd64) — tasks/0010 4.4. python scripts/bundle.py lock 이 쓴다 (손으로 고치지 않는다).",
             "# 묶음 만들기는 이 바퀴만 받는다 (pip download --no-deps --require-hashes). minedocscan 자신은 커밋에서 만든다 (잠금 밖).",
             "# 판을 올리는 순서: bundle.py lock → CI 의 windows-install(묶음·설치·자가 시험) → 커밋. scripts/README.md, 설명서 2장"]
    for name, version, digest in sorted(entries, key=lambda e: norm(e[0])):
        lines += [f"{name}=={version} \\", f"    --hash=sha256:{digest}"]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def wheel_name_version(path: Path) -> tuple[str, str]:
    """바퀴 파일 이름 → (이름, 판)."""
    parts = path.name[:-len(".whl")].split("-")
    return parts[0], parts[1]


def verify_downloaded(wheels: Path, lock: dict) -> None:
    """받은 바퀴가 잠금과 하나하나 같다 — 이름·판·해시. 모자라거나 남거나 다르면 멈춘다 (pip 의 --require-hashes 에 더한 확인)."""
    got = {}
    for w in sorted(wheels.glob("*.whl")):
        name, version = wheel_name_version(w)
        got[norm(name)] = (version, sha256(w), w.name)
    for key, (name, version, digest) in lock.items():
        if key not in got:
            raise BundleError(f"잠금의 바퀴를 받지 못했다: {name}=={version}")
        if got[key][0] != version or got[key][1] != digest:
            raise BundleError(f"받은 바퀴가 잠금과 다르다: {got[key][2]} (판 또는 SHA-256) — 멈춘다")
    extra = sorted(set(got) - set(lock))
    if extra:
        raise BundleError(f"잠금에 없는 바퀴를 받았다: {', '.join(got[k][2] for k in extra)}")


# ── 닫힘 (잠금이 런타임 의존성을 다 담나) ─────────────────────────────────────────
def _packaging():
    try:
        from packaging.markers import Marker  # noqa: F401
        from packaging.requirements import Requirement
        from packaging.version import Version
    except ImportError:                                  # 묶음을 만드는 파이썬에 packaging 이 없으면 pip 이 가진 것
        from pip._vendor.packaging.requirements import Requirement
        from pip._vendor.packaging.version import Version
    return Requirement, Version


def win_env(python_version: str) -> dict:
    """앱 전용 파이썬(cp312-win_amd64)의 표식 환경."""
    short = ".".join(python_version.split(".")[:2])
    return {"os_name": "nt", "sys_platform": "win32", "platform_system": "Windows", "platform_machine": "AMD64",
            "platform_release": "", "platform_version": "", "python_version": short, "python_full_version": python_version,
            "implementation_name": "cpython", "implementation_version": python_version,
            "platform_python_implementation": "CPython", "extra": ""}


def wheel_requires(path: Path) -> tuple[str, str, list[str]]:
    """바퀴의 METADATA → (이름, 판, Requires-Dist 들)."""
    with zipfile.ZipFile(path) as z:
        meta = next(n for n in z.namelist() if n.endswith(".dist-info/METADATA") and n.count("/") == 1)
        head = z.read(meta).decode("utf-8", "replace").split("\n\n", 1)[0]
    fields: dict[str, list[str]] = {}
    for line in head.splitlines():
        if ": " in line and not line.startswith((" ", "\t")):
            k, v = line.split(": ", 1)
            fields.setdefault(k, []).append(v.strip())
    return fields["Name"][0], fields["Version"][0], fields.get("Requires-Dist", [])


def closure(app_whl: Path, wheels: list[Path], extras: list[str], env: dict) -> dict[str, list[str]]:
    """minedocscan[extras] 에서 시작해 Requires-Dist 를 env 의 표식으로 따라간다. 돌려주는 값: {정규화한 이름: 요구한 쪽들}.
    바퀴가 없거나 판이 범위 밖이면 BundleError — 잠금이 닫힘을 다 담지 못했다."""
    Requirement, Version = _packaging()
    have = {}
    for w in wheels:
        name, version, reqs = wheel_requires(w)
        have[norm(name)] = (name, version, reqs)
    app_name, app_version, app_reqs = wheel_requires(app_whl)
    seen: dict[str, list[str]] = {}
    todo = [(app_name, app_reqs, frozenset(extras))]
    done: set[tuple[str, frozenset]] = set()
    while todo:
        who, reqs, ex = todo.pop()
        for r in reqs:
            req = Requirement(r)
            envs = [dict(env, extra=e) for e in (ex or {""})]
            if req.marker is not None and not any(req.marker.evaluate(e) for e in envs):
                continue
            key = norm(req.name)
            if key not in have:
                raise BundleError(f"잠금에 없는 런타임 의존성: {req.name} ({who} 가 요구한다) — bundle.toml 의 windows_only 를 보고 "
                                  "python scripts/bundle.py lock")
            name, version, sub = have[key]
            if not req.specifier.contains(Version(version), prereleases=True):
                raise BundleError(f"잠금의 판이 요구 범위 밖이다: {name}=={version} ({who} 가 {req.specifier} 를 요구한다)")
            seen.setdefault(key, []).append(who)
            mark = (key, frozenset(req.extras))
            if mark not in done:
                done.add(mark)
                todo.append((name, sub, frozenset(req.extras)))
    return seen


def check_closure(app_whl: Path, wheels: Path, cfg: dict) -> None:
    """잠금의 바퀴들이 minedocscan[extras] 의 닫힘과 같다 — 모자라면(위) 멈추고, 닫힘 밖의 바퀴가 있어도 멈춘다."""
    found = sorted(wheels.glob("*.whl"))
    need = closure(app_whl, found, cfg["wheels"].get("extras", []), win_env(cfg["python"]["version"]))
    extra = sorted(w.name for w in found if norm(wheel_name_version(w)[0]) not in need)
    if extra:
        raise BundleError(f"잠금에 닫힘 밖의 바퀴가 있다: {', '.join(extra)} — python scripts/bundle.py lock")


# ── 같은 바이트의 zip ─────────────────────────────────────────────────────────
def source_date_epoch(root: Path = ROOT) -> int:
    """묶음의 시각: 환경변수 SOURCE_DATE_EPOCH, 없으면 커밋 시각 (git). 둘 다 없으면 BundleError."""
    if os.environ.get("SOURCE_DATE_EPOCH", "").strip():
        return int(os.environ["SOURCE_DATE_EPOCH"])
    try:
        out = subprocess.run(["git", "-C", str(root), "log", "-1", "--format=%ct"], capture_output=True, text=True, check=True)
        return int(out.stdout.strip())
    except (OSError, subprocess.CalledProcessError, ValueError):
        raise BundleError("커밋 시각을 알 수 없다 — git 저장소에서 돌리거나 SOURCE_DATE_EPOCH 를 준다") from None


def zip_time(epoch: int) -> tuple[int, int, int, int, int, int]:
    t = time.gmtime(max(epoch, 315532800))                 # zip 의 시각은 1980 부터
    return (t.tm_year, t.tm_mon, t.tm_mday, t.tm_hour, t.tm_min, t.tm_sec - t.tm_sec % 2)


def write_zip(path: Path, entries: list[tuple[str, bytes]], epoch: int) -> Path:
    """(이름, 바이트) 들을 이 순서대로 — 시각·권한·만든 시스템·압축을 고정해 (같은 입력이면 같은 바이트, 같은 zlib 에서)."""
    when = zip_time(epoch)
    tmp = path.with_name(path.name + ".part")
    with zipfile.ZipFile(tmp, "w") as z:
        for name, data in entries:
            info = zipfile.ZipInfo(name, date_time=when)
            info.create_system = 3                            # 유닉스 (권한을 external_attr 로)
            info.external_attr = 0o100644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=6)
    os.replace(tmp, path)
    return path


# ── OpenCV 의 FFmpeg (tasks/0010 4.5) ────────────────────────────────────────
def _record_name(names: list[str]) -> str:
    rec = [n for n in names if n.endswith(".dist-info/RECORD") and n.count("/") == 1]
    if len(rec) != 1:
        raise BundleError("바퀴의 RECORD 를 찾을 수 없다")
    return rec[0]


def strip_ffmpeg(whl: Path, out: Path, epoch: int) -> list[str]:
    """OpenCV 바퀴에서 FFmpeg 플러그인을 빼고 RECORD 의 그 줄을 지워 다시 묶는다 (이름·판 그대로). 돌려주는 값: 뺀 항목.
    그 DLL 이 없으면 BundleError — OpenCV 의 판이 바뀌어 이름이 달라졌을 수 있다 (말없이 FFmpeg 가 남지 않게)."""
    with zipfile.ZipFile(whl) as z:
        names = z.namelist()
        gone = [n for n in names if FFMPEG.fullmatch(n)]
        if not gone:
            raise BundleError(f"{whl.name} 에 FFmpeg 플러그인(cv2/opencv_videoio_ffmpeg*.dll)이 없다 — OpenCV 의 판이 바뀌었으면 "
                              "scripts/bundle.py 의 FFMPEG 를 다시 본다")
        record = _record_name(names)
        rows = list(csv.reader(io.StringIO(z.read(record).decode("utf-8"))))
        kept = [r for r in rows if r and r[0] not in gone]
        buf = io.StringIO()
        csv.writer(buf, lineterminator="\n").writerows(kept)
        entries = [(n, buf.getvalue().encode("utf-8") if n == record else z.read(n)) for n in names if n not in gone]
    write_zip(out, entries, epoch)
    return gone


def record_problems(whl: Path) -> list[str]:
    """바퀴의 RECORD 가 항목과 하나하나 맞는가 (pip 은 설치할 때 RECORD 의 해시를 확인하지 않는다 — 시험이 본다). 돌려주는 값: 어긋난 것."""
    import base64

    out = []
    with zipfile.ZipFile(whl) as z:
        names = [n for n in z.namelist() if not n.endswith("/")]
        record = _record_name(names)
        rows = {r[0]: r for r in csv.reader(io.StringIO(z.read(record).decode("utf-8"))) if r}
        for n in names:
            r = rows.get(n)
            if r is None:
                out.append(f"RECORD 에 없다: {n}")
            elif n != record:
                data = z.read(n)
                digest = "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
                if r[1] != digest or r[2] != str(len(data)):
                    out.append(f"해시·크기가 다르다: {n}")
        out += [f"항목이 없다: {n}" for n in rows if n not in names]
    return out


# ── 소스 (tasks/0010 4.5) ───────────────────────────────────────────────────
SOURCES_README = """소스 — 이 묶음에 든 LGPL 구성요소의 대응 소스 (tasks/0010 4.5)

{lines}

- psycopg-binary 의 바퀴는 psycopg_c 의 sdist 로 만든다 (그 바퀴에는 생성된 C 와 libpq·libssl·libcrypto 가 든다 — 그것들의 라이선스는
  THIRD_PARTY_NOTICES.txt). 바이너리를 만드는 방법은 psycopg 저장소의 그 판 태그에 있다: https://github.com/psycopg/psycopg (tools/, .github/)
- OpenCV 바퀴의 FFmpeg 플러그인(opencv_videoio_ffmpeg*.dll, LGPL-2.1)은 묶음에서 뺐다 — 이 프로그램은 동영상을 읽지 않는다.
- 각 파일의 SHA-256 은 묶음의 SHA256SUMS.txt 에, 받은 곳은 scripts/bundle.toml 의 [sources] 에 있다.
"""


def place_sources(cfg: dict, lock: dict, cache: Path, dest: Path) -> list[dict]:
    """[sources] 의 sdist 를 받아(해시 확인) dest 에. 판이 잠금의 바퀴와 다르면 BundleError. README.txt 도."""
    entries = cfg.get("sources") or {}
    out = []
    for dist, project in SOURCES.items():
        if norm(dist) not in lock:
            continue
        version = lock[norm(dist)][1]
        entry = entries.get(project) or entries.get(project.replace("-", "_"))
        if entry is None:
            raise BundleError(f"bundle.toml 의 [sources] 에 {project} 가 없다 — {dist} 의 소스 (python scripts/bundle.py lock)")
        if entry["version"] != version:
            raise BundleError(f"[sources] {project} 의 판 {entry['version']} 이 바퀴 {dist}=={version} 와 다르다 — "
                              "python scripts/bundle.py lock")
        path = fetch(entry, cache)
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, dest / path.name)
        out.append({"dist": dist, "file": path.name, "bytes": path.stat().st_size})
    lines = "\n".join(f"- {o['file']} — 바퀴 {o['dist']}=={lock[norm(o['dist'])][1]} 의 소스" for o in out)
    (dest / "README.txt").write_text(SOURCES_README.format(lines=lines), encoding="utf-8", newline="\n")
    return out


def fetch(entry: dict, cache: Path, given: Path | None = None) -> Path:
    """bundle.toml 의 항목 하나: given(이미 받은 파일) 또는 캐시 또는 내려받기 → SHA-256 확인. 다르면 BundleError."""
    name = entry["url"].rsplit("/", 1)[-1]
    want = entry["sha256"]
    path = Path(given) if given else cache / name
    if not path.is_file():
        cache.mkdir(parents=True, exist_ok=True)
        part = path.with_name(path.name + ".part")
        try:
            with urllib.request.urlopen(entry["url"], timeout=120) as r, open(part, "wb") as f:
                shutil.copyfileobj(r, f)
        except OSError as e:                                  # 망·프록시 — 한 줄로 (받은 곳의 호스트만)
            host = entry["url"].split("/")[2] if "://" in entry["url"] else "?"
            raise BundleError(f"{name} 을 받지 못했다 ({host}, {type(e).__name__}) — 인터넷이 되는 곳에서, 또는 받아 둔 파일을 준다") from None
        os.replace(part, path)
    got = sha256(path)
    if got != want:
        raise BundleError(f"{name} 의 SHA-256 이 bundle.toml 과 다르다 (받은 것 {got[:16]}…, 적힌 것 {want[:16]}…) — 멈춘다")
    return path


def run(cmd: list[str], capture: bool = False, env: dict | None = None) -> str:
    print("  $", " ".join(cmd), flush=True)
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", **(env or {})}     # licenses.py 의 한글 (윈도우 러너는 cp1252)
    if not capture:
        subprocess.run(cmd, check=True, env=env)
        return ""
    r = subprocess.run(cmd, env=env, stdout=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
    print(r.stdout, end="", flush=True)
    if r.returncode:
        raise subprocess.CalledProcessError(r.returncode, cmd, r.stdout)
    return r.stdout


# ── 만드는 도구 (bundle.toml [build]) ─────────────────────────────────────────
def build_env(cfg: dict, work: Path) -> Path:
    """[build] 의 판·해시로 만든 가상 환경 (hatchling·markdown 과 그 의존성) — 돌려주는 값: 그 파이썬."""
    reqs = (cfg.get("build") or {}).get("requirements") or []
    if not reqs:
        raise BundleError("bundle.toml 에 [build] requirements 가 없다 — python scripts/bundle.py lock")
    venv = work / "buildenv"
    run([sys.executable, "-m", "venv", str(venv)])
    py = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    req = work / "build-requirements.txt"
    req.write_text("\n".join(reqs) + "\n", encoding="utf-8")
    run([str(py), "-m", "pip", "install", "--quiet", "--disable-pip-version-check", "--no-deps", "--require-hashes",
         "--only-binary=:all:", "-r", str(req)])
    return py


def build_app_wheel(py: Path, dest: Path, epoch: int) -> Path:
    """이 커밋의 minedocscan 바퀴 — [build] 의 hatchling 으로, 격리 없이, SOURCE_DATE_EPOCH = 커밋 시각."""
    run([str(py), "-m", "pip", "wheel", str(ROOT), "--no-deps", "--no-build-isolation", "--disable-pip-version-check",
         "-w", str(dest)], env={"SOURCE_DATE_EPOCH": str(epoch)})
    return next(dest.glob("minedocscan-*.whl"))


def build_manual(dest: Path, py: Path | None = None) -> Path:
    """docs/manual/ → 한 장짜리 manual.html (scripts/manual_html.py). py: [build] 의 markdown 이 깔린 파이썬 (없으면 이 파이썬 —
    markdown 꾸러미가 없으면 BundleError)."""
    if py is not None:
        run([str(py), str(Path(__file__).with_name("manual_html.py")), "--out", str(dest)])
        return dest
    try:
        import markdown  # noqa: F401
    except ImportError:
        raise BundleError('설명서(manual.html)를 만들려면 markdown 꾸러미가 있어야 한다 — pip install -e ".[docs]"') from None
    sys.path.insert(0, str(Path(__file__).parent))
    import manual_html

    dest.write_text(manual_html.render(), encoding="utf-8", newline="\n")
    return dest


def app_version() -> str:
    text = (ROOT / "src" / "minedocscan" / "__init__.py").read_text(encoding="utf-8")
    return next(line.split("=", 1)[1].strip().strip('"\'') for line in text.splitlines() if line.startswith("__version__"))


def download_locked(cfg: dict, dest: Path) -> None:
    """잠금의 바퀴만 받는다 (의존성을 풀지 않는다 — 잠금에 없는 것은 받지 않는다, 해시가 다르면 pip 이 멈춘다)."""
    w = cfg["wheels"]
    run([sys.executable, "-m", "pip", "download", "--no-deps", "--require-hashes", "--only-binary=:all:", "--disable-pip-version-check",
         "--platform", w["platform"], "--python-version", w["python_version"], "--implementation", "cp",
         "-r", str(LOCK), "-d", str(dest)])


# ── PE 의 가져오기 표 (msvcp140.dll 이 필요한 바이너리) ─────────────────────────────
def pe_imports(data: bytes) -> list[str]:
    """PE(.dll·.pyd·.exe)가 가져오는 DLL 이름 (가져오기 표와 지연 가져오기 표). PE 가 아니면 빈 목록."""
    try:
        if data[:2] != b"MZ":
            return []
        pe = struct.unpack_from("<I", data, 0x3C)[0]
        if data[pe:pe + 4] != b"PE\0\0":
            return []
        n_sections, opt_size = struct.unpack_from("<H", data, pe + 6)[0], struct.unpack_from("<H", data, pe + 20)[0]
        opt = pe + 24
        magic = struct.unpack_from("<H", data, opt)[0]
        dirs = opt + (112 if magic == 0x20B else 96)
        sections = []
        for i in range(n_sections):
            s = opt + opt_size + 40 * i
            vsize, va, raw_size, raw = struct.unpack_from("<IIII", data, s + 8)
            sections.append((va, max(vsize, raw_size), raw))

        def off(rva: int) -> int | None:
            for va, size, raw in sections:
                if va <= rva < va + size:
                    return rva - va + raw
            return None

        def cstr(rva: int) -> str:
            o = off(rva)
            return "" if o is None else data[o:data.index(b"\0", o)].decode("ascii", "replace")

        names = []
        imp_rva = struct.unpack_from("<I", data, dirs + 8)[0]               # 1: 가져오기
        o = off(imp_rva) if imp_rva else None
        while o is not None and o + 20 <= len(data):
            name_rva = struct.unpack_from("<I", data, o + 12)[0]
            if not any(struct.unpack_from("<5I", data, o)):
                break
            names.append(cstr(name_rva))
            o += 20
        delay_rva = struct.unpack_from("<I", data, dirs + 13 * 8)[0]        # 13: 지연 가져오기
        o = off(delay_rva) if delay_rva else None
        while o is not None and o + 32 <= len(data):
            fields = struct.unpack_from("<8I", data, o)
            if not any(fields):
                break
            names.append(cstr(fields[1]))
            o += 32
        return [n for n in names if n]
    except (struct.error, ValueError, IndexError):
        return []


def dll_report(wheels: Path, embed: Path | None) -> dict:
    """VC 런타임: 바퀴의 바이너리(.pyd·.dll)가 이름 그대로 가져오는 VC 런타임 DLL 중 묶음(embeddable 파이썬)이 주지 않는 것 — 그 PC 에
    Visual C++ 재배포 패키지가 있어야 한다. 바퀴가 자기 것을 이름을 바꿔 담은 것(numpy.libs/msvcp140-<해시>.dll)은 그 바퀴만 쓴다."""
    bundled, imported = {}, {}
    for whl in sorted(wheels.glob("*.whl")):
        with zipfile.ZipFile(whl) as z:
            for name in z.namelist():
                base = name.rsplit("/", 1)[-1].lower()
                if not base.endswith((".pyd", ".dll")):
                    continue
                if base.split("-")[0].split(".")[0] + ".dll" in VC_DLLS:
                    bundled.setdefault(whl.name, []).append(name)
                for dll in pe_imports(z.read(name)):
                    if dll.lower() in VC_DLLS:
                        imported.setdefault(dll.lower(), set()).add(whl.name)
    embed_dlls = []
    if embed is not None:
        with zipfile.ZipFile(embed) as z:
            embed_dlls = sorted(n.lower() for n in z.namelist() if n.lower() in VC_DLLS)
    missing = sorted(set(imported) - set(embed_dlls))
    return {"wheels_with_own_vc_runtime": bundled, "vc_runtime_imports": {k: sorted(v) for k, v in sorted(imported.items())},
            "embed_vc_runtime": embed_dlls, "needed_from_system": {k: sorted(imported[k]) for k in missing},
            "msvcp140_needed_from_system": any(k.startswith("msvcp140") for k in missing)}


# ── 묶음 ───────────────────────────────────────────────────────────────────
def with_bom(src: Path, dst: Path) -> None:
    """UTF-8 BOM + CRLF (Windows PowerShell 5.1 은 BOM 이 없는 스크립트를 시스템 코드 페이지로 읽어 한글을 깨뜨린다)."""
    text = src.read_text(encoding="utf-8-sig").replace("\r\n", "\n").replace("\n", "\r\n")
    dst.write_bytes(b"\xef\xbb\xbf" + text.encode("utf-8"))


def write_sums(folder: Path) -> Path:
    lines = [f"{sha256(p)}  {p.relative_to(folder).as_posix()}" for p in sorted(folder.rglob("*"))
             if p.is_file() and p.name != SUMS]
    out = folder / SUMS
    out.write_text("\n".join(lines) + "\n", encoding="ascii", newline="\n")
    return out


def make_zip(folder: Path, zip_path: Path, epoch: int) -> Path:
    """폴더 → zip (맨 위 폴더 하나, 이름 순서, 시각·권한 고정 — write_zip). 이름이 ASCII 가 아니면 멈춘다 (zip 의 이름 인코딩이 PC 마다
    다르게 읽힌다)."""
    files = sorted((p for p in folder.rglob("*") if p.is_file()), key=lambda p: p.relative_to(folder.parent).as_posix())
    bad = [p.name for p in files if not p.relative_to(folder.parent).as_posix().isascii()]
    if bad:
        raise BundleError(f"zip 안의 이름이 ASCII 가 아니다: {bad[:3]}")
    return write_zip(zip_path, [(p.relative_to(folder.parent).as_posix(), p.read_bytes()) for p in files], epoch)


def copy_text(src: Path, dst: Path) -> None:
    """글 파일을 LF 로 (윈도우에서 받은 저장소가 CRLF 여도 같은 바이트 — .gitattributes 와 같이)."""
    dst.write_bytes(src.read_bytes().replace(b"\r\n", b"\n"))


def assemble(stage: Path, *, version: str, embed: Path, pip_whl: Path, wheels: Path, notices: Path, manual: Path | None,
             sources: Path | None = None) -> None:
    """묶음 폴더를 채운다 (SHA256SUMS 까지)."""
    if stage.exists():
        shutil.rmtree(stage)
    (stage / "wheels").mkdir(parents=True)
    shutil.copyfile(embed, stage / embed.name)
    shutil.copyfile(pip_whl, stage / pip_whl.name)
    for w in sorted(wheels.glob("*.whl")):
        shutil.copyfile(w, stage / "wheels" / w.name)
    if sources is not None and sources.is_dir():
        shutil.copytree(sources, stage / "sources")
    for name in TEXTS:
        with_bom(WINDOWS / name, stage / name)
    copy_text(ROOT / "config" / "minedocscan.example.toml", stage / "minedocscan.example.toml")
    copy_text(ROOT / "NOTICE", stage / "NOTICE")
    shutil.copyfile(notices, stage / "THIRD_PARTY_NOTICES.txt")
    if manual is not None:
        shutil.copyfile(manual, stage / "manual.html")
    (stage / "VERSION").write_text(version + "\n", encoding="ascii", newline="\n")
    write_sums(stage)


def build(a, cfg: dict) -> int:
    version = app_version()
    out = a.out.resolve()
    cache = (a.cache or out / "cache").resolve()
    work = out / "work"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    lock = read_lock()
    epoch = source_date_epoch()
    print(f"묶음 minedocscan {version} — 파이썬 {cfg['python']['version']}, pip {cfg['pip']['version']}, 잠금의 바퀴 {len(lock)}개, "
          f"시각 {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(epoch))}", flush=True)
    embed = fetch(cfg["python"], cache, a.python_zip)
    pip_whl = fetch(cfg["pip"], cache, a.pip_wheel)
    py = build_env(cfg, work)
    app = build_app_wheel(py, work / "own", epoch)
    got = work / "downloaded"
    download_locked(cfg, got)
    verify_downloaded(got, lock)
    check_closure(app, got, cfg)
    wheels = work / "wheels"
    wheels.mkdir()
    removed = []
    for w in sorted(got.glob("*.whl")):
        if norm(wheel_name_version(w)[0]) == norm(OPENCV):
            removed += strip_ffmpeg(w, wheels / w.name, epoch)
            bad = record_problems(wheels / w.name)
            if bad:
                raise BundleError(f"다시 묶은 {w.name} 의 RECORD 가 맞지 않는다: {bad[:3]}")
        else:
            shutil.copyfile(w, wheels / w.name)
    shutil.copyfile(app, wheels / app.name)
    sources = work / "sources"
    placed = place_sources(cfg, lock, cache, sources)
    notices = work / "THIRD_PARTY_NOTICES.txt"
    shutil.copyfile(pip_whl, wheels / pip_whl.name)                     # 라이선스 목록에 pip 도 (바퀴의 본문으로)
    try:
        lic = run([sys.executable, str(Path(__file__).with_name("licenses.py")), "--wheels", str(wheels), "--sources", str(sources),
                   "--python-embed", str(embed), "--check", "--out", str(notices)], capture=True)
    except subprocess.CalledProcessError as e:
        (out / "licenses.txt").write_text(e.output or "", encoding="utf-8")   # 막힌 목록(NO 줄)도 시험 성적서로
        raise
    (out / "licenses.txt").write_text(lic, encoding="utf-8")
    (wheels / pip_whl.name).unlink()
    stage = work / f"minedocscan-{version}-win64"
    manual = a.manual if a.manual else build_manual(work / "manual.html", py)
    if not manual.is_file():
        raise BundleError(f"설명서가 없다: {manual}")
    assemble(stage, version=version, embed=embed, pip_whl=pip_whl, wheels=wheels, notices=notices, manual=manual, sources=sources)
    zip_path = make_zip(stage, out / f"{stage.name}.zip", epoch)
    report = {"version": version, "zip": zip_path.name, "zip_bytes": zip_path.stat().st_size, "zip_sha256": sha256(zip_path),
              "source_date_epoch": epoch, "files": sum(1 for p in stage.rglob("*") if p.is_file()),
              "wheels": len(list((stage / "wheels").glob("*.whl"))), "locked": len(lock),
              "python": cfg["python"]["version"], "pip": cfg["pip"]["version"], "removed": removed,
              "sources": placed, "sources_bytes": sum(p["bytes"] for p in placed), "dll": dll_report(stage / "wheels", embed)}
    (out / "bundle-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if a.notice:
        sys.path.insert(0, str(Path(__file__).parent))
        from ci_notice import notice

        print(notice(a.notice, {"bytes": report["zip_bytes"], "sha256": report["zip_sha256"], "wheels": report["wheels"],
                                "removed": len(removed), "sources_bytes": report["sources_bytes"]}), flush=True)
    need = report["dll"]["needed_from_system"]
    print(f"묶음: {zip_path.name} {report['zip_bytes'] / 2**20:.1f} MB (sha256 {report['zip_sha256'][:16]}…), 바퀴 {report['wheels']}개, "
          f"뺀 것 {len(removed)}, 소스 {len(placed)}개 — PC 의 VC++ 재배포 패키지가 "
          + (f"필요하다: {', '.join(need)}" if need else "필요 없다 (바퀴가 가져오는 VC 런타임은 embeddable 파이썬이 준다)"), flush=True)
    return 0


# ── 잠금을 새로 쓴다 (사람이 판을 올릴 때) ─────────────────────────────────────────
def _pypi_sdist(project: str, version: str) -> dict:
    """PyPI 의 그 판의 sdist (url·sha256)."""
    with urllib.request.urlopen(f"https://pypi.org/pypi/{project}/{version}/json", timeout=60) as r:
        data = json.load(r)
    sd = [u for u in data["urls"] if u.get("packagetype") == "sdist"]
    if not sd:
        raise BundleError(f"PyPI 에 {project}=={version} 의 sdist 가 없다")
    return {"version": version, "url": sd[0]["url"], "sha256": sd[0]["digests"]["sha256"]}


def _toml_str(v: str) -> str:
    return json.dumps(v, ensure_ascii=False)


def lock_cmd(a, cfg: dict) -> int:
    """풀어서 잠금을 새로 쓴다: ① minedocscan[extras] + windows_only 를 cp312-win_amd64 로 pip download (의존성을 푼다) → 닫힘의 바퀴만
    bundle.lock ② [build]: hatchling·markdown 과 그 의존성 (순수 파이썬 바퀴) ③ [sources]: 잠금의 psycopg·psycopg-binary 와 같은 판의
    sdist (PyPI). bundle.toml 의 GENERATED 줄 아래를 다시 쓴다."""
    with tempfile.TemporaryDirectory(prefix="minedocscan-lock-") as t:
        tmp = Path(t)
        run([sys.executable, "-m", "pip", "wheel", str(ROOT), "--no-deps", "--disable-pip-version-check", "-w", str(tmp / "own")])
        app = next((tmp / "own").glob("minedocscan-*.whl"))
        w = cfg["wheels"]
        extras = ",".join(w.get("extras", []))
        req = f"minedocscan[{extras}] @ {app.resolve().as_uri()}" if extras else f"minedocscan @ {app.resolve().as_uri()}"
        plat = ["--only-binary=:all:", "--platform", w["platform"], "--python-version", w["python_version"], "--implementation", "cp",
                "--disable-pip-version-check"]
        run([sys.executable, "-m", "pip", "download", req, *w.get("windows_only", []), *plat, "-d", str(tmp / "rt")])
        found = [p for p in sorted((tmp / "rt").glob("*.whl")) if norm(wheel_name_version(p)[0]) != "minedocscan"]
        need = closure(app, found, w.get("extras", []), win_env(cfg["python"]["version"]))
        locked = [(wheel_requires(p)[0], wheel_name_version(p)[1], sha256(p)) for p in found if norm(wheel_name_version(p)[0]) in need]
        run([sys.executable, "-m", "pip", "download", *BUILD_TOOLS, *plat, "-d", str(tmp / "bt")])
        build_reqs = [f"{wheel_requires(p)[0]}=={wheel_name_version(p)[1]} --hash=sha256:{sha256(p)}"
                      for p in sorted((tmp / "bt").glob("*.whl"), key=lambda p: norm(p.name))]
    lock = {norm(n): (n, v, d) for n, v, d in locked}
    sources = {SOURCES[d]: _pypi_sdist(SOURCES[d], lock[norm(d)][1]) for d in SOURCES if norm(d) in lock}
    write_lock(locked)
    text = a.config.read_text(encoding="utf-8")
    head = text.split(GENERATED, 1)[0].rstrip() + "\n\n"
    gen = [GENERATED, "", "[build]",
           "# 묶음을 만드는 도구 — minedocscan 의 바퀴(hatchling)와 manual.html(markdown). 따로 만든 가상 환경에 이 판·해시로만 깔린다",
           "requirements = ["] + [f"  {_toml_str(r)}," for r in build_reqs] + ["]", "", "[sources]",
           "# 소스를 같이 줘야 하는 것 (LGPL — tasks/0010 4.5): 묶음의 sources/. 판은 잠금의 바퀴와 같아야 한다"]
    for project, s in sources.items():
        gen += [f"[sources.{project}]", f"version = {_toml_str(s['version'])}", f"url = {_toml_str(s['url'])}",
                f"sha256 = {_toml_str(s['sha256'])}"]
    a.config.write_text(head + "\n".join(gen) + "\n", encoding="utf-8", newline="\n")
    print(f"잠금: 바퀴 {len(locked)}개 → {LOCK.name}, 만드는 도구 {len(build_reqs)}개·소스 {len(sources)}개 → {a.config.name}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("what", nargs="?", choices=["build", "lock"], default="build",
                    help="build(기본): 묶음 / lock: 잠금과 [build]·[sources] 를 새로 쓴다 (판을 올릴 때)")
    ap.add_argument("--out", type=Path, default=ROOT / "dist", help="묶음 zip 과 보고를 쓸 폴더 (저장소 밖이 좋다 — dist/ 는 무시된다)")
    ap.add_argument("--cache", type=Path, help="내려받은 파이썬·pip·sdist 를 두는 곳 (기본 OUT/cache)")
    ap.add_argument("--config", type=Path, default=CONFIG)
    ap.add_argument("--python-zip", type=Path, help="이미 받은 embeddable zip (해시는 그대로 확인한다)")
    ap.add_argument("--pip-wheel", type=Path, help="이미 받은 pip 바퀴 (해시는 그대로 확인한다)")
    ap.add_argument("--manual", type=Path, help="이미 만든 manual.html (없으면 docs/manual/ 에서 만든다)")
    ap.add_argument("--notice", metavar="TITLE", help="끝에 CI 의 알림 한 줄 (scripts/ci_notice.py — 크기·sha256·바퀴·뺀 것·소스)")
    a = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):                             # 윈도우 러너의 표준 출력은 cp1252 — 한글을 찍다 죽지 않게
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    try:
        cfg = load_config(a.config)
        return lock_cmd(a, cfg) if a.what == "lock" else build(a, cfg)
    except BundleError as e:
        print(f"묶음을 만들지 않았습니다: {e}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as e:
        print(f"묶음을 만들지 않았습니다: 명령이 실패했다 (종료 코드 {e.returncode})", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

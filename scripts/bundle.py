"""윈도우 오프라인 설치 묶음 `minedocscan-<판>-win64.zip` 을 만든다 (tasks/0009 4.5 가). 인터넷이 되는 곳에서 — CI 의 윈도우 작업.

    python scripts/bundle.py --out dist

들어가는 것 (zip 안의 이름은 ASCII, 맨 위 폴더 하나):
  python-<판>-embed-amd64.zip, pip-<판>-py3-none-any.whl   bundle.toml 의 판·SHA-256 과 같아야 한다 — 다르면 멈춘다
  wheels/                    minedocscan 의 바퀴와 런타임 의존성 전부([postgres] 포함, cp312-win_amd64)
  install.ps1, uninstall.ps1 scripts/windows/ 의 것을 UTF-8 BOM·CRLF 로 (Windows PowerShell 5.1 은 BOM 이 없으면 한글을 깨뜨린다)
  minedocscan.example.toml   config/ 의 보기
  THIRD_PARTY_NOTICES.txt    scripts/licenses.py --wheels … --python-embed … --check (허용 목록 밖이면 멈춘다)
  NOTICE, manual.html, INSTALL.txt, VERSION
  SHA256SUMS.txt             묶음 안 파일 전부의 해시 (install.ps1 이 먼저 확인한다)
묶음 옆에 bundle-report.json: 크기, 바퀴 수, 바퀴가 담은 DLL 과 msvcp140.dll 이 필요한 바이너리 (embeddable 파이썬에는 vcruntime 만 있다).
이 도구는 설치되는 패키지에 들어가지 않는다 (scripts/).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import tomllib
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG = Path(__file__).with_name("bundle.toml")
WINDOWS = Path(__file__).with_name("windows")
SUMS = "SHA256SUMS.txt"
TEXTS = ("install.ps1", "uninstall.ps1", "INSTALL.txt")   # BOM·CRLF 로 (메모장·PowerShell 5.1)
VC_DLLS = ("msvcp140.dll", "msvcp140_1.dll", "msvcp140_2.dll", "vcruntime140.dll", "vcruntime140_1.dll", "concrt140.dll")


class BundleError(RuntimeError):
    """묶음을 만들 수 없다 — 한 줄로."""


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_config(path: Path = CONFIG) -> dict:
    cfg = tomllib.loads(path.read_text(encoding="utf-8"))
    for key in ("python", "pip"):
        sha = str(cfg[key].get("sha256", ""))
        if len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
            raise BundleError(f"{path.name} 의 [{key}] sha256 이 SHA-256(소문자 16진수 64자)이 아니다 — 판을 올릴 때 해시를 같이 적는다")
    return cfg


def fetch(entry: dict, cache: Path, given: Path | None = None) -> Path:
    """bundle.toml 의 항목 하나: given(이미 받은 파일) 또는 캐시 또는 내려받기 → SHA-256 확인. 다르면 BundleError."""
    name = entry["url"].rsplit("/", 1)[-1]
    want = entry["sha256"]
    path = Path(given) if given else cache / name
    if not path.is_file():
        cache.mkdir(parents=True, exist_ok=True)
        part = path.with_name(path.name + ".part")
        with urllib.request.urlopen(entry["url"], timeout=120) as r, open(part, "wb") as f:
            shutil.copyfileobj(r, f)
        os.replace(part, path)
    got = sha256(path)
    if got != want:
        raise BundleError(f"{name} 의 SHA-256 이 bundle.toml 과 다르다 (받은 것 {got[:16]}…, 적힌 것 {want[:16]}…) — 멈춘다")
    return path


def run(cmd: list[str]) -> None:
    print("  $", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True, env={**os.environ, "PYTHONIOENCODING": "utf-8"})   # licenses.py 의 한글 (윈도우 러너는 cp1252)


def app_version() -> str:
    text = (ROOT / "src" / "minedocscan" / "__init__.py").read_text(encoding="utf-8")
    return next(line.split("=", 1)[1].strip().strip('"\'') for line in text.splitlines() if line.startswith("__version__"))


def build_wheels(cfg: dict, dest: Path, work: Path) -> Path:
    """minedocscan 의 바퀴(여기서 만든다)와 런타임 의존성(cp312-win_amd64 의 바퀴만)을 dest 에. 돌려주는 값: minedocscan 의 바퀴."""
    own = work / "own"
    run([sys.executable, "-m", "pip", "wheel", str(ROOT), "--no-deps", "-w", str(own)])
    app = next(own.glob("minedocscan-*.whl"))
    w = cfg["wheels"]
    extras = ",".join(w.get("extras", []))
    req = f"minedocscan[{extras}] @ {app.resolve().as_uri()}" if extras else f"minedocscan @ {app.resolve().as_uri()}"
    run([sys.executable, "-m", "pip", "download", req, *w.get("windows_only", []), "--only-binary=:all:",
         "--platform", w["platform"], "--python-version", w["python_version"], "--implementation", "cp",
         "-d", str(dest)])
    return app


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
    out.write_text("\n".join(lines) + "\n", encoding="ascii")
    return out


def make_zip(folder: Path, zip_path: Path) -> Path:
    """폴더 → zip (맨 위 폴더 하나). 이름이 ASCII 가 아니면 멈춘다 (zip 의 이름 인코딩이 PC 마다 다르게 읽힌다)."""
    names = [p for p in sorted(folder.rglob("*")) if p.is_file()]
    bad = [p.name for p in names if not p.relative_to(folder.parent).as_posix().isascii()]
    if bad:
        raise BundleError(f"zip 안의 이름이 ASCII 가 아니다: {bad[:3]}")
    tmp = zip_path.with_name(zip_path.name + ".part")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for p in names:
            z.write(p, p.relative_to(folder.parent).as_posix())
    os.replace(tmp, zip_path)
    return zip_path


def assemble(stage: Path, *, version: str, embed: Path, pip_whl: Path, wheels: Path, notices: Path, manual: Path | None) -> None:
    """묶음 폴더를 채운다 (SHA256SUMS 까지)."""
    if stage.exists():
        shutil.rmtree(stage)
    (stage / "wheels").mkdir(parents=True)
    shutil.copy2(embed, stage / embed.name)
    shutil.copy2(pip_whl, stage / pip_whl.name)
    for w in sorted(wheels.glob("*.whl")):
        shutil.copy2(w, stage / "wheels" / w.name)
    for name in TEXTS:
        with_bom(WINDOWS / name, stage / name)
    shutil.copy2(ROOT / "config" / "minedocscan.example.toml", stage / "minedocscan.example.toml")
    shutil.copy2(ROOT / "NOTICE", stage / "NOTICE")
    shutil.copy2(notices, stage / "THIRD_PARTY_NOTICES.txt")
    if manual is not None and manual.is_file():
        shutil.copy2(manual, stage / "manual.html")
    else:                                                               # 설명서는 단계 7 — 그 전에는 자리만
        (stage / "manual.html").write_text("<!doctype html><meta charset='utf-8'><title>minedocscan</title>"
                                           "<p>사용 설명서는 docs/manual/ 에 있습니다.</p>\n", encoding="utf-8")
    (stage / "VERSION").write_text(version + "\n", encoding="ascii")
    write_sums(stage)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, default=ROOT / "dist", help="묶음 zip 과 보고를 쓸 폴더 (저장소 밖이 좋다 — dist/ 는 무시된다)")
    ap.add_argument("--cache", type=Path, help="내려받은 파이썬·pip 을 두는 곳 (기본 OUT/cache)")
    ap.add_argument("--config", type=Path, default=CONFIG)
    ap.add_argument("--python-zip", type=Path, help="이미 받은 embeddable zip (해시는 그대로 확인한다)")
    ap.add_argument("--pip-wheel", type=Path, help="이미 받은 pip 바퀴 (해시는 그대로 확인한다)")
    ap.add_argument("--manual", type=Path, default=ROOT / "docs" / "manual" / "manual.html")
    a = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):                             # 윈도우 러너의 표준 출력은 cp1252 — 한글을 찍다 죽지 않게
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    try:
        cfg = load_config(a.config)
        version = app_version()
        out = a.out.resolve()
        cache = (a.cache or out / "cache").resolve()
        work = out / "work"
        if work.exists():
            shutil.rmtree(work)
        print(f"묶음 minedocscan {version} — 파이썬 {cfg['python']['version']}, pip {cfg['pip']['version']}", flush=True)
        embed = fetch(cfg["python"], cache, a.python_zip)
        pip_whl = fetch(cfg["pip"], cache, a.pip_wheel)
        wheels = work / "wheels"
        build_wheels(cfg, wheels, work)
        notices = work / "THIRD_PARTY_NOTICES.txt"
        shutil.copy2(pip_whl, wheels / pip_whl.name)                    # 라이선스 목록에 pip 도 (바퀴의 본문으로)
        run([sys.executable, str(Path(__file__).with_name("licenses.py")), "--wheels", str(wheels), "--python-embed", str(embed),
             "--check", "--out", str(notices)])
        (wheels / pip_whl.name).unlink()
        stage = work / f"minedocscan-{version}-win64"
        assemble(stage, version=version, embed=embed, pip_whl=pip_whl, wheels=wheels, notices=notices, manual=a.manual)
        zip_path = make_zip(stage, out / f"{stage.name}.zip")
        report = {"version": version, "zip": zip_path.name, "zip_bytes": zip_path.stat().st_size,
                  "files": sum(1 for p in stage.rglob("*") if p.is_file()), "wheels": len(list((stage / "wheels").glob("*.whl"))),
                  "python": cfg["python"]["version"], "pip": cfg["pip"]["version"], "dll": dll_report(stage / "wheels", embed)}
        (out / "bundle-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        need = report["dll"]["needed_from_system"]
        print(f"묶음: {zip_path.name} {report['zip_bytes'] / 2**20:.1f} MB, 바퀴 {report['wheels']}개 — PC 의 VC++ 재배포 패키지가 "
              + (f"필요하다: {', '.join(need)}" if need else "필요 없다 (바퀴가 가져오는 VC 런타임은 embeddable 파이썬이 준다)"), flush=True)
        return 0
    except BundleError as e:
        print(f"묶음을 만들지 않았습니다: {e}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as e:
        print(f"묶음을 만들지 않았습니다: 명령이 실패했다 (종료 코드 {e.returncode})", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

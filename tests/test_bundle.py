"""윈도우 설치 묶음을 만드는 도구 scripts/bundle.py (tasks/0009 4.5 가) — 망 없이 되는 부분: 고정 해시, BOM, SHA256SUMS, zip 의 이름,
PE 의 가져오기 표, 묶음 폴더. 실제 묶음 만들기·설치는 윈도우 CI 의 windows-install 작업에서.
"""
from __future__ import annotations

import hashlib
import importlib.util
import struct
import sys
import tomllib
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location("bundle_tool", ROOT / "scripts" / "bundle.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["bundle_tool"] = mod
    spec.loader.exec_module(mod)
    return mod


bundle = _load()


def test_the_pinned_versions_carry_sha256():
    cfg = bundle.load_config()
    assert cfg["python"]["version"].startswith("3.12.") and cfg["python"]["url"].endswith("-embed-amd64.zip")
    assert cfg["pip"]["url"].endswith(f"pip-{cfg['pip']['version']}-py3-none-any.whl")
    assert "tzdata" in cfg["wheels"]["windows_only"] and cfg["wheels"]["extras"] == ["postgres"]


def test_a_download_that_differs_from_the_pin_stops(tmp_path):
    f = tmp_path / "python-3.12.10-embed-amd64.zip"
    f.write_bytes(b"not the real one")
    good = {"url": "https://example.invalid/python-3.12.10-embed-amd64.zip", "sha256": hashlib.sha256(f.read_bytes()).hexdigest()}
    assert bundle.fetch(good, tmp_path / "cache", f) == f
    with pytest.raises(bundle.BundleError, match="SHA-256 이 bundle.toml 과 다르다"):
        bundle.fetch(dict(good, sha256="0" * 64), tmp_path / "cache", f)
    bad = tmp_path / "bundle.toml"
    text = (ROOT / "scripts" / "bundle.toml").read_text(encoding="utf-8")
    pinned = tomllib.loads(text)["python"]["sha256"]
    bad.write_text(text.replace(pinned, "PENDING"), encoding="utf-8")
    with pytest.raises(bundle.BundleError, match=r"\[python\] sha256"):
        bundle.load_config(bad)


def test_powershell_scripts_get_a_bom_and_crlf(tmp_path):
    src = tmp_path / "a.ps1"
    src.write_text("Write-Host '설치'\nexit 0\n", encoding="utf-8")
    out = tmp_path / "b.ps1"
    bundle.with_bom(src, out)
    data = out.read_bytes()
    assert data.startswith(b"\xef\xbb\xbf") and b"\r\n" in data and b"\n" not in data.replace(b"\r\n", b"")
    assert data[3:].decode("utf-8") == "Write-Host '설치'\r\nexit 0\r\n"
    bundle.with_bom(out, out)                                           # 이미 BOM 이 있어도 하나만
    assert out.read_bytes().count(b"\xef\xbb\xbf") == 1
    for name in bundle.TEXTS:                                           # 저장소의 원본은 UTF-8 (BOM 은 묶음에서)
        assert (bundle.WINDOWS / name).read_text(encoding="utf-8")


def test_sums_cover_every_file_but_themselves_and_zip_names_are_ascii(tmp_path):
    stage = tmp_path / "minedocscan-1.2.3-win64"
    (stage / "wheels").mkdir(parents=True)
    (stage / "VERSION").write_text("1.2.3\n", encoding="ascii")
    (stage / "wheels" / "x-1-py3-none-any.whl").write_bytes(b"x")
    sums = bundle.write_sums(stage)
    lines = sums.read_text(encoding="ascii").splitlines()
    assert [x.split('  ')[1] for x in lines] == ["VERSION", "wheels/x-1-py3-none-any.whl"]     # 경로 순서
    for line in lines:
        digest, rel = line.split("  ")
        assert hashlib.sha256((stage / rel).read_bytes()).hexdigest() == digest
    z = bundle.make_zip(stage, tmp_path / "out.zip")
    assert sorted(zipfile.ZipFile(z).namelist()) == sorted(f"{stage.name}/{n}" for n in ("SHA256SUMS.txt", "VERSION",
                                                                                          "wheels/x-1-py3-none-any.whl"))
    (stage / "설명.txt").write_text("x", encoding="utf-8")
    with pytest.raises(bundle.BundleError, match="ASCII"):
        bundle.make_zip(stage, tmp_path / "out2.zip")


def tiny_pe(imports: list[str], delay: list[str] = ()) -> bytes:
    """가져오기 표(와 지연 가져오기 표)만 있는 아주 작은 PE32+ — 파서를 시험하려고."""
    sect_va, sect_raw = 0x1000, 0x200
    body = bytearray(0x400)
    names_at = 0x300
    name_rvas = []
    for n in [*imports, *delay]:
        name_rvas.append(sect_va + names_at)
        body[names_at:names_at + len(n) + 1] = n.encode() + b"\0"
        names_at += len(n) + 1
    imp = 0
    for i, _n in enumerate(imports):                                    # 가져오기 서술자 (20바이트), 끝은 0
        struct.pack_into("<5I", body, imp + 20 * i, 1, 0, 0, name_rvas[i], 1)
    dly = 0x100
    for j, _n in enumerate(delay):                                      # 지연 가져오기 서술자 (32바이트), 끝은 0
        struct.pack_into("<8I", body, dly + 32 * j, 1, name_rvas[len(imports) + j], 0, 0, 0, 0, 0, 0)
    pe_off, opt_size = 0x40, 240
    head = bytearray(sect_raw)
    head[:2] = b"MZ"
    struct.pack_into("<I", head, 0x3C, pe_off)
    head[pe_off:pe_off + 4] = b"PE\0\0"
    struct.pack_into("<HH", head, pe_off + 4, 0x8664, 1)                 # 기계, 구획 수
    struct.pack_into("<H", head, pe_off + 20, opt_size)
    opt = pe_off + 24
    struct.pack_into("<H", head, opt, 0x20B)                             # PE32+
    struct.pack_into("<II", head, opt + 112 + 8, sect_va + 0, 20 * (len(imports) + 1))        # 1: 가져오기
    struct.pack_into("<II", head, opt + 112 + 13 * 8, sect_va + dly if delay else 0, 64)      # 13: 지연 가져오기
    sec = opt + opt_size
    head[sec:sec + 8] = b".rdata\0\0"
    struct.pack_into("<IIII", head, sec + 8, len(body), sect_va, len(body), sect_raw)
    return bytes(head) + bytes(body)


def test_pe_imports_reads_the_import_and_delay_tables():
    data = tiny_pe(["python3.dll", "VCRUNTIME140.dll", "KERNEL32.dll"], delay=["MSVCP140.dll"])
    assert bundle.pe_imports(data) == ["python3.dll", "VCRUNTIME140.dll", "KERNEL32.dll", "MSVCP140.dll"]
    assert bundle.pe_imports(b"not a pe") == [] and bundle.pe_imports(data[:100]) == []


def test_dll_report_says_what_the_pc_must_supply(tmp_path):
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    with zipfile.ZipFile(wheels / "a-1-cp312-cp312-win_amd64.whl", "w") as z:
        z.writestr("a/_x.pyd", tiny_pe(["VCRUNTIME140.dll", "VCRUNTIME140_1.dll"]))
        z.writestr("a.libs/msvcp140-abcdef.dll", tiny_pe(["KERNEL32.dll"]))
    with zipfile.ZipFile(wheels / "b-1-cp312-cp312-win_amd64.whl", "w") as z:
        z.writestr("b/_y.pyd", tiny_pe(["MSVCP140.dll"]))
    embed = tmp_path / "python-3.12.10-embed-amd64.zip"
    with zipfile.ZipFile(embed, "w") as z:
        z.writestr("vcruntime140.dll", b"")
        z.writestr("vcruntime140_1.dll", b"")
    r = bundle.dll_report(wheels, embed)
    assert r["wheels_with_own_vc_runtime"] == {"a-1-cp312-cp312-win_amd64.whl": ["a.libs/msvcp140-abcdef.dll"]}
    assert r["needed_from_system"] == {"msvcp140.dll": ["b-1-cp312-cp312-win_amd64.whl"]} and r["msvcp140_needed_from_system"]
    (wheels / "b-1-cp312-cp312-win_amd64.whl").unlink()
    r = bundle.dll_report(wheels, embed)
    assert r["needed_from_system"] == {} and not r["msvcp140_needed_from_system"]


def test_assemble_lays_out_the_bundle(tmp_path):
    embed = tmp_path / "python-3.12.10-embed-amd64.zip"
    with zipfile.ZipFile(embed, "w") as z:
        z.writestr("python.exe", b"MZ")
    pip_whl = tmp_path / "pip-26.2.1-py3-none-any.whl"
    pip_whl.write_bytes(b"pip")
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    (wheels / "minedocscan-0.0.1-py3-none-any.whl").write_bytes(b"app")
    notices = tmp_path / "THIRD_PARTY_NOTICES.txt"
    notices.write_text("notices\n", encoding="utf-8")
    stage = tmp_path / "out" / "minedocscan-0.0.1-win64"
    bundle.assemble(stage, version="0.0.1", embed=embed, pip_whl=pip_whl, wheels=wheels, notices=notices, manual=None)
    names = sorted(p.relative_to(stage).as_posix() for p in stage.rglob("*") if p.is_file())
    assert names == sorted(["INSTALL.txt", "NOTICE", "SHA256SUMS.txt", "THIRD_PARTY_NOTICES.txt", "VERSION", "install.ps1",
                            "manual.html", "minedocscan.example.toml", "pip-26.2.1-py3-none-any.whl",
                            "python-3.12.10-embed-amd64.zip", "uninstall.ps1", "wheels/minedocscan-0.0.1-py3-none-any.whl"])
    for name in bundle.TEXTS:
        assert (stage / name).read_bytes().startswith(b"\xef\xbb\xbf")
    listed = {line.split("  ")[1] for line in (stage / "SHA256SUMS.txt").read_text(encoding="ascii").splitlines()}
    assert listed == set(names) - {"SHA256SUMS.txt"}
    assert (stage / "VERSION").read_text(encoding="ascii") == "0.0.1\n"

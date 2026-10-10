"""묶음을 다시 만들 수 있게 (tasks/0010 4.4·4.5 — scripts/bundle.py): 잠금(bundle.lock), 받은 바퀴의 확인, 닫힘·범위 검사, 같은 바이트의
zip, OpenCV 의 FFmpeg 빼기와 RECORD, 소스(sdist)의 판. 망 없이 — 가짜 바퀴로. 실제 묶음 두 번 만들기는 CI 의 windows-install 작업에서."""
from __future__ import annotations

import base64
import csv
import hashlib
import io
import os
import zipfile
from pathlib import Path

import pytest

from test_bundle import ROOT, bundle


def fake_wheel(folder: Path, name: str, version: str, requires: list[str] = (), files: dict[str, bytes] | None = None,
               tag: str = "py3-none-any") -> Path:
    """METADATA·RECORD 가 맞는 작은 바퀴 (시험용)."""
    dist = name.replace("-", "_")
    info = f"{dist}-{version}.dist-info"
    entries = dict(files or {})
    entries[f"{info}/METADATA"] = ("\n".join(["Metadata-Version: 2.4", f"Name: {name}", f"Version: {version}",
                                              *(f"Requires-Dist: {r}" for r in requires)]) + "\n\n").encode()
    entries[f"{info}/WHEEL"] = b"Wheel-Version: 1.0\n"
    rows = []
    for n, b in entries.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(b).digest()).rstrip(b"=").decode()
        rows.append([n, f"sha256={digest}", str(len(b))])
    rows.append([f"{info}/RECORD", "", ""])
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerows(rows)
    entries[f"{info}/RECORD"] = buf.getvalue().encode()
    path = folder / f"{dist}-{version}-{tag}.whl"
    folder.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        for n, b in entries.items():
            z.writestr(n, b)
    return path


def test_the_lock_pins_every_runtime_wheel_with_one_hash():
    lock = bundle.read_lock()
    assert {"numpy", "opencv-python-headless", "pypdfium2", "psycopg", "psycopg-binary", "tzdata"} <= set(lock)
    assert "minedocscan" not in lock                                    # 커밋에서 만든다
    for name, version, digest in lock.values():
        assert version and len(digest) == 64, name
    cfg = bundle.load_config()
    assert {r.split("==")[0].lower() for r in cfg["build"]["requirements"]} >= {"hatchling", "markdown"}
    assert all("--hash=sha256:" in r for r in cfg["build"]["requirements"])
    for dist, project in bundle.SOURCES.items():                        # 소스의 판 = 바퀴의 판
        assert cfg["sources"][project]["version"] == lock[dist][1], dist
    text = (ROOT / "scripts" / "bundle.lock").read_text(encoding="utf-8")
    assert "\r" not in text and text.count("--hash=sha256:") == len(lock)     # 줄 끝은 LF, 바퀴마다 해시 하나


def test_a_downloaded_wheel_that_differs_from_the_lock_stops(tmp_path):
    w = tmp_path / "w"
    a, b = fake_wheel(w, "alpha", "1.0"), fake_wheel(w, "bravo", "2.0")
    lock = {"alpha": ("alpha", "1.0", bundle.sha256(a)), "bravo": ("bravo", "2.0", bundle.sha256(b))}
    bundle.verify_downloaded(w, lock)
    with pytest.raises(bundle.BundleError, match="잠금과 다르다"):
        bundle.verify_downloaded(w, dict(lock, bravo=("bravo", "2.0", "0" * 64)))       # 해시 하나를 거짓으로
    with pytest.raises(bundle.BundleError, match="받지 못했다"):
        bundle.verify_downloaded(w, dict(lock, charlie=("charlie", "1.0", "0" * 64)))
    fake_wheel(w, "delta", "1.0")
    with pytest.raises(bundle.BundleError, match="잠금에 없는 바퀴"):
        bundle.verify_downloaded(w, lock)
    lines = tmp_path / "bundle.lock"
    bundle.write_lock([("alpha", "1.0", "a" * 64), ("Bravo", "2.0", "b" * 64)], lines)
    assert bundle.read_lock(lines) == {"alpha": ("alpha", "1.0", "a" * 64), "bravo": ("Bravo", "2.0", "b" * 64)}
    lines.write_text("alpha>=1.0\n", encoding="utf-8")
    with pytest.raises(bundle.BundleError, match="읽을 수 없다"):
        bundle.read_lock(lines)


def test_the_closure_check_stops_on_a_short_or_out_of_range_lock(tmp_path):
    """잠금의 바퀴들이 minedocscan[postgres] 의 닫힘(win32·cp312 의 표식으로)을 다 담는지, 판이 범위 안인지 — 가짜 바퀴로."""
    app = fake_wheel(tmp_path / "own", "minedocscan", "9.9", ["alpha>=1.0", 'bravo; sys_platform == "win32"',
                                                             'charlie; sys_platform == "linux"', 'delta[x]; extra == "postgres"'])
    cfg = {"wheels": {"extras": ["postgres"]}, "python": {"version": "3.12.10"}}
    w = tmp_path / "w"
    fake_wheel(w, "alpha", "1.2", ["echo>=2"])
    fake_wheel(w, "bravo", "1.0")
    fake_wheel(w, "delta", "1.0", ['foxtrot; extra == "x"', 'golf; python_version < "3.11"'])
    fake_wheel(w, "echo", "2.1")
    fake_wheel(w, "foxtrot", "1.0")
    bundle.check_closure(app, w, cfg)                                   # charlie(리눅스)·golf(3.11 미만)는 닫힘 밖
    (w / "echo-2.1-py3-none-any.whl").unlink()
    with pytest.raises(bundle.BundleError, match="잠금에 없는 런타임 의존성: echo"):
        bundle.check_closure(app, w, cfg)
    fake_wheel(w, "echo", "1.0")
    with pytest.raises(bundle.BundleError, match="요구 범위 밖"):
        bundle.check_closure(app, w, cfg)
    (w / "echo-1.0-py3-none-any.whl").unlink()
    fake_wheel(w, "echo", "2.1")
    fake_wheel(w, "hotel", "1.0")
    with pytest.raises(bundle.BundleError, match="닫힘 밖의 바퀴"):
        bundle.check_closure(app, w, cfg)


def test_the_zip_is_the_same_bytes_for_the_same_input(tmp_path):
    """같은 파일·같은 시각이면 같은 zip — 파일의 수정 시각·권한·만든 순서와 무관하게. 이름 순서, 시각은 준 것 (1980 이후)."""
    def stage(root: Path, order: list[str], mtime: int) -> Path:
        folder = root / "minedocscan-0.0.1-win64"
        for n in order:
            p = folder / n
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(n.encode() * 50)
            os.utime(p, (mtime, mtime))
        return folder

    names = ["b.txt", "a.txt", "wheels/z.whl", "wheels/c.whl"]
    one = bundle.make_zip(stage(tmp_path / "1", names, 1_000_000_000), tmp_path / "one.zip", 1_700_000_000)
    two = bundle.make_zip(stage(tmp_path / "2", list(reversed(names)), 1_600_000_000), tmp_path / "two.zip", 1_700_000_000)
    assert one.read_bytes() == two.read_bytes()
    with zipfile.ZipFile(one) as z:
        assert z.namelist() == sorted(f"minedocscan-0.0.1-win64/{n}" for n in names)
        assert {i.date_time for i in z.infolist()} == {(2023, 11, 14, 22, 13, 20)}
    three = bundle.make_zip(stage(tmp_path / "3", names, 0), tmp_path / "three.zip", 1_700_000_002)
    assert three.read_bytes() != one.read_bytes()                       # 시각은 커밋의 것
    assert bundle.zip_time(0) == (1980, 1, 1, 0, 0, 0)


def opencv_wheel(folder: Path, ffmpeg: bool = True) -> Path:
    files = {"cv2/__init__.py": b"# cv2", "cv2/cv2.pyd": b"MZ" + b"\0" * 64, "cv2/LICENSE-3RD-PARTY.txt": b"ffmpeg LGPL"}
    if ffmpeg:
        files["cv2/opencv_videoio_ffmpeg500_64.dll"] = b"MZ ffmpeg" * 100
    return fake_wheel(folder, "opencv-python-headless", "5.0.0.93", ["numpy>=2"], files, tag="cp37-abi3-win_amd64")


def test_ffmpeg_is_stripped_and_the_record_still_matches(tmp_path):
    src = opencv_wheel(tmp_path / "in")
    assert bundle.record_problems(src) == []
    out = tmp_path / "out" / src.name
    out.parent.mkdir()
    gone = bundle.strip_ffmpeg(src, out, 1_700_000_000)
    assert gone == ["cv2/opencv_videoio_ffmpeg500_64.dll"]
    with zipfile.ZipFile(out) as z:
        assert not [n for n in z.namelist() if "ffmpeg" in n.lower()] and "cv2/cv2.pyd" in z.namelist()
        assert b"opencv_videoio_ffmpeg" not in z.read("opencv_python_headless-5.0.0.93.dist-info/RECORD")
    assert bundle.record_problems(out) == []                            # 모든 항목에 해시·크기가 맞는 RECORD 줄, 모든 줄에 항목
    again = tmp_path / "again.whl"
    bundle.strip_ffmpeg(src, again, 1_700_000_000)
    assert again.read_bytes() == out.read_bytes()                       # 같은 바이트
    with pytest.raises(bundle.BundleError, match="FFmpeg 플러그인"):
        bundle.strip_ffmpeg(opencv_wheel(tmp_path / "none", ffmpeg=False), tmp_path / "x.whl", 1)
    # RECORD 가 어긋난 바퀴는 알린다 (다시 묶은 것의 자기 확인)
    bad = tmp_path / "bad.whl"
    with zipfile.ZipFile(out) as z, zipfile.ZipFile(bad, "w") as y:
        for n in z.namelist():
            y.writestr(n, b"changed" if n == "cv2/__init__.py" else z.read(n))
    assert bundle.record_problems(bad) == ["해시·크기가 다르다: cv2/__init__.py"]


def test_sources_must_match_the_wheel_versions(tmp_path):
    """sources/ 의 sdist 는 잠금의 psycopg·psycopg-binary 와 같은 판 (다르면 멈춘다). 받은 것은 해시로 확인한다. README.txt."""
    cache = tmp_path / "cache"
    cache.mkdir()
    cfg = {"sources": {}}
    for project, file in (("psycopg", "psycopg-3.2.10.tar.gz"), ("psycopg-c", "psycopg_c-3.2.10.tar.gz")):
        (cache / file).write_bytes(file.encode())
        cfg["sources"][project] = {"version": "3.2.10", "url": f"https://example.invalid/{file}",
                                   "sha256": hashlib.sha256(file.encode()).hexdigest()}
    lock = {"psycopg": ("psycopg", "3.2.10", "0" * 64), "psycopg-binary": ("psycopg-binary", "3.2.10", "0" * 64)}
    placed = bundle.place_sources(cfg, lock, cache, tmp_path / "sources")
    assert [p["file"] for p in placed] == ["psycopg-3.2.10.tar.gz", "psycopg_c-3.2.10.tar.gz"]
    assert sorted(p.name for p in (tmp_path / "sources").iterdir()) == ["README.txt", *[p["file"] for p in placed]]
    assert "psycopg_c-3.2.10.tar.gz" in (tmp_path / "sources" / "README.txt").read_text(encoding="utf-8")
    with pytest.raises(bundle.BundleError, match="바퀴 psycopg-binary==3.2.11 와 다르다"):
        bundle.place_sources(cfg, dict(lock, **{"psycopg-binary": ("psycopg-binary", "3.2.11", "0" * 64)}), cache, tmp_path / "s2")
    (cache / "psycopg-3.2.10.tar.gz").write_bytes(b"tampered")
    with pytest.raises(bundle.BundleError, match="SHA-256"):
        bundle.place_sources(cfg, lock, cache, tmp_path / "s3")


def test_the_commit_time_is_the_bundle_time(monkeypatch):
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1700000000")
    assert bundle.source_date_epoch() == 1_700_000_000
    monkeypatch.delenv("SOURCE_DATE_EPOCH")
    if (ROOT / ".git").exists():
        assert bundle.source_date_epoch() > 1_700_000_000               # 이 저장소의 마지막 커밋


def test_text_files_reach_the_bundle_with_lf(tmp_path):
    """윈도우에서 받은 저장소가 CRLF 여도 묶음의 글은 같은 바이트 (.gitattributes 와 copy_text)."""
    src = tmp_path / "a.toml"
    src.write_bytes(b"[x]\r\na = 1\r\n")
    bundle.copy_text(src, tmp_path / "b.toml")
    assert (tmp_path / "b.toml").read_bytes() == b"[x]\na = 1\n"
    attrs = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert "eol=lf" in attrs and "*.png binary" in attrs

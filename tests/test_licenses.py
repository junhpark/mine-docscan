"""제3자 라이선스 목록 (tasks/0009 4.3 나 — scripts/licenses.py): SPDX 표현의 검사, 가짜 바퀴로 만든 묶음의 목록·본문, 허용 목록 밖이면
종료 코드 1, pymupdf(AGPL)를 다시 넣으면 막힌다."""
from __future__ import annotations

import importlib.util
import sys
import tomllib
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def licenses():
    if "mds_licenses" in sys.modules:
        return sys.modules["mds_licenses"]
    spec = importlib.util.spec_from_file_location("mds_licenses", ROOT / "scripts" / "licenses.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod                                      # dataclass 가 모듈을 찾는다
    spec.loader.exec_module(mod)
    return mod


def fake_wheel(folder: Path, name: str, version: str, meta: list[str], files: dict[str, bytes]) -> Path:
    """METADATA 와 파일 몇 개만 든 바퀴 (시험용 — 설치하지 않는다)."""
    info = f"{name.replace('-', '_')}-{version}.dist-info"
    path = folder / f"{name.replace('-', '_')}-{version}-py3-none-any.whl"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(f"{info}/METADATA", "\n".join(["Metadata-Version: 2.4", f"Name: {name}", f"Version: {version}", *meta]) + "\n\n")
        for n, b in files.items():
            z.writestr(n.replace("{info}", info), b)
    return path


def test_spdx_expressions():
    lic = licenses()
    for ok in ("MIT", "Apache-2.0 OR BSD-3-Clause", "BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0", "LGPL-3.0-only",
               "(MIT OR GPL-3.0-only) AND PSF-2.0", "GPL-3.0-only OR MIT", "PostgreSQL"):
        assert lic.allowed(ok), ok
    for bad in ("GPL-3.0-only", "AGPL-3.0-or-later", "MIT AND GPL-2.0-only", "GPL-3.0-or-later WITH GCC-exception-3.1",
                "LicenseRef-Proprietary"):
        assert not lic.allowed(bad), bad
    for broken in ("", "MIT AND", "(MIT", "MIT OR OR BSD-3-Clause", "Apache 2.0"):
        with pytest.raises(ValueError):
            lic.parse_spdx(broken)


def test_a_bundle_lists_every_wheel_with_its_texts_and_blocks_what_is_not_allowed(tmp_path, capsys):
    lic = licenses()
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    fake_wheel(wheels, "alpha", "1.0", ["License-Expression: MIT"], {"{info}/licenses/LICENSE": b"MIT text alpha"})
    fake_wheel(wheels, "bravo", "2.0", ["License: MIT"], {"{info}/LICENSE.txt": b"MIT text bravo"})
    fake_wheel(wheels, "charlie", "3.0", ["License-Expression: BSD-3-Clause"],
               {"{info}/licenses/LICENSE": b"BSD", "{info}/licenses/BUILD_LICENSES/freetype.txt": "Copyright \xa9 FreeType".encode("latin-1"),
                "charlie.libs/libfoo-0123456789abcdef.dll": b"MZ"})
    out = tmp_path / "THIRD_PARTY_NOTICES.txt"
    assert lic.main(["--wheels", str(wheels), "--out", str(out), "--check"]) == 0
    text = out.read_text(encoding="utf-8")
    assert "MIT text alpha" in text and "MIT text bravo" in text and "Copyright © FreeType" in text   # latin-1 본문
    assert "같이 든 라이브러리: libfoo.dll" in text and "python (embeddable)" in text and "pip" in text
    # 허용 목록 밖(AGPL — 지금의 pymupdf 메타데이터 그대로)이나 알 수 없는 라이선스면 --check 는 1
    fake_wheel(wheels, "PyMuPDF", "1.28.0", ["License: Dual Licensed - GNU AFFERO GPL 3.0 or Artifex Commercial License",
                                             "Classifier: License :: OSI Approved :: GNU Affero General Public License v3"],
               {"{info}/COPYING": b"AGPL"})
    capsys.readouterr()
    assert lic.main(["--wheels", str(wheels), "--check"]) == 1
    assert "PyMuPDF 1.28.0" in capsys.readouterr().err
    assert lic.main(["--wheels", str(wheels)]) == 0                   # --check 가 없으면 알리기만
    gpl = tmp_path / "gpl"
    gpl.mkdir()
    fake_wheel(gpl, "delta", "1.0", ["License-Expression: GPL-3.0-only"], {"{info}/licenses/COPYING": b"GPL"})
    fake_wheel(gpl, "echo", "1.0", ["License-Expression: MIT"], {})                       # 본문이 없다 — 목록이 본문 없이 나가지 않게
    fake_wheel(gpl, "foxtrot", "1.0", ["License-Expression: MIT", "License-File: NOTES.md"], {"{info}/licenses/NOTES.md": b"x"})
    fake_wheel(gpl, "golf", "1.0", ["Metadata-Version: 2.1", "License: MIT", "License-File: LICENSES/Apache-2.0.txt"],
               {"{info}/Apache-2.0.txt": b"Apache text golf"})          # 2.1(setuptools): dist-info 에 납작하게 — 이름이 LICENSE 가 아니다
    fake_wheel(gpl, "hotel", "1.0", ["License-Expression: MIT", "License-File: COPYING.md", "License-File: GONE.txt"],
               {"{info}/licenses/COPYING.md": b"x"})                 # 적힌 파일 하나가 없다
    closure = lic.wheels_closure(gpl)
    assert [t[0].rsplit("/", 1)[-1] for d in closure if d.name == "golf" for t in d.texts] == ["Apache-2.0.txt"]
    bad = lic.problems(closure)
    assert [b.split()[0] for b in bad] == ["delta", "echo", "hotel"] and "본문이 없다" in bad[1] and "GONE.txt" in bad[2]
    empty = tmp_path / "empty"
    empty.mkdir()
    assert lic.main(["--wheels", str(empty), "--check"]) == 2         # 바퀴가 없다 — 빈 목록이 통과하지 않는다
    assert lic.main(["--wheels", str(tmp_path / "없는 폴더")]) == 2
    assert (ROOT / "NOTICE").read_text(encoding="utf-8").startswith("(이 프로그램의 저작권·라이선스 문구 — 사람이 정한다)")


def test_the_runtime_dependencies_have_no_pymupdf_and_pass_the_check():
    """런타임 의존성(묶음에 드는 postgres 포함)에 pymupdf(AGPL-3.0)가 없다 (tasks/0009 4.3 가). 지금 환경의 닫힘이 검사를 통과한다
    (CI 의 우분투 작업이 같은 검사를 .[postgres] 로 돈다 — 윈도우의 닫힘은 묶음을 만들 때)."""
    lic = licenses()
    from packaging.requirements import Requirement

    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    bundled = [*project["dependencies"], *project["optional-dependencies"]["postgres"]]     # 묶음에 드는 것
    names = {lic.norm(Requirement(d).name) for d in bundled}
    assert "pymupdf" not in names and "fitz" not in names and "pypdfium2" in names
    dists = lic.installed_closure([])
    assert {lic.norm(d.name) for d in dists} >= {"numpy", "opencv-python-headless", "pypdfium2", "openpyxl", "pyyaml", "rapidfuzz"}
    assert lic.problems(dists + lic.app_extras()) == []


# ── 소스 조건 (tasks/0010 4.5) ────────────────────────────────────────────────
def test_which_licenses_need_their_source_given():
    lic = licenses()
    for yes in ("LGPL-3.0-only", "LGPL-2.1-or-later AND MIT", "GPL-2.0-only", "MPL-2.0", "AGPL-3.0-or-later", "Apache 2.0"):
        assert lic.needs_source(yes), yes
    for no in ("MIT", "Apache-2.0 OR BSD-3-Clause", "MPL-2.0 OR MIT", "GPL-3.0-or-later WITH GCC-exception-3.1", "PostgreSQL"):
        assert not lic.needs_source(no), no


def test_the_bundle_check_wants_the_lgpl_sources_and_no_ffmpeg(tmp_path, capsys):
    """--wheels … --sources … --check: 소스가 없는 LGPL 가짜 배포판 → 1, sources/ 에 넣으면 0. 바퀴 안에 FFmpeg 가 남아 있으면 1.
    --sources 가 없으면 세기만 한다. psycopg-binary 의 소스는 psycopg_c 의 sdist."""
    lic = licenses()
    wheels, sources = tmp_path / "wheels", tmp_path / "sources"
    wheels.mkdir()
    sources.mkdir()
    fake_wheel(wheels, "psycopg-binary", "3.2.10", ["License-Expression: LGPL-3.0-only"],
               {"{info}/licenses/LICENSE.txt": b"LGPL", "psycopg_binary.libs/libpq-0123456789.dll": b"MZ"})
    opencv = fake_wheel(wheels, "opencv-python-headless", "5.0.0", ["License: Apache 2.0"],
                        {"{info}/LICENSE.txt": b"Apache", "cv2/opencv_videoio_ffmpeg500_64.dll": b"MZ"})
    args = ["--wheels", str(wheels), "--sources", str(sources), "--check"]
    assert lic.main(args) == 1
    err = capsys.readouterr().err
    assert "sources/psycopg_c-3.2.10.tar.gz 가 없다" in err and "FFmpeg" in err
    assert lic.main(["--wheels", str(wheels), "--check"]) == 0             # --sources 가 없으면 세기만
    out = capsys.readouterr().out
    assert "소스를 같이 줘야 하는 배포판 1, 바퀴 안에 든 그런 구성요소 1" in out
    (sources / "psycopg_c-3.2.10.tar.gz").write_bytes(b"sdist")
    assert lic.main(args) == 1                                             # FFmpeg 가 남아 있다
    capsys.readouterr()
    opencv.unlink()
    fake_wheel(wheels, "opencv-python-headless", "5.0.0", ["License: Apache 2.0"], {"{info}/LICENSE.txt": b"Apache"})
    notices = tmp_path / "THIRD_PARTY_NOTICES.txt"
    assert lic.main([*args, "--out", str(notices)]) == 0
    text = notices.read_text(encoding="utf-8")
    assert "소스: 같이 준다 — 묶음의 sources/psycopg_c-3.2.10.tar.gz" in text and "FFmpeg 플러그인" in text and "뺐다" in text
    out = capsys.readouterr().out
    assert [line for line in out.splitlines() if line.startswith(("ok ", "NO "))] == [   # 시험 성적서가 읽는 줄은 그대로
        "ok  opencv-python-headless 5.0.0 — Apache-2.0", "ok  psycopg-binary 3.2.10 — LGPL-3.0-only",
        "ok  python (embeddable)  — PSF-2.0", "ok  pip  — MIT"]
    with pytest.raises(SystemExit):
        lic.main(["--installed", "--sources", str(sources)])            # --sources 는 묶음에만

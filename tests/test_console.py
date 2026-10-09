"""윈도우에서 돈다 (tasks/0009 4.4): 출력의 글자, 표준 출력이 없는 프로세스(pythonw), 대소문자·별칭이 다른 경로의 거절.

윈도우의 러너는 cp1252, 한국어 PC 는 cp949 다 — 표준 출력을 파일에 묶으면 파이썬이 그 코드 페이지로 써서 한글에서 UnicodeEncodeError 로
죽었다. 명령을 하위 프로세스로 돌려 표준 출력을 파일 핸들에 묶는다 (PowerShell 의 > 는 다시 인코딩해 시험이 되지 않는다). 윈도우에서는 러너의
기본 코드 페이지 그대로, 리눅스에서는 PYTHONIOENCODING=cp949·ascii 로.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from minedocscan.config import Settings

WINDOWS = sys.platform == "win32"


def run_cli(args: list[str], env: dict, out: Path) -> int:
    with open(out, "wb") as f:                                        # 파일 핸들 — 콘솔이 아니다
        return subprocess.run([sys.executable, "-m", "minedocscan.cli", *args], stdout=f, stderr=subprocess.STDOUT, env=env,
                              timeout=600).returncode


def clean_env(**extra) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("MINEDOCSCAN_") and k not in ("PYTHONIOENCODING", "PYTHONUTF8")}
    env.update(extra)
    return env


@pytest.mark.parametrize("encoding", [None] if WINDOWS else ["cp949", "ascii"])
def test_commands_write_utf8_through_a_file_handle(tmp_path, encoding):
    """info·synth·run·report·watch --once 를 표준 출력을 파일에 묶어 돌려도 종료 코드 0, 파일은 UTF-8 (한글이 그대로)."""
    env = clean_env(**({"PYTHONIOENCODING": encoding} if encoding else {}))
    syn = tmp_path / "합성"
    out = tmp_path / "out.txt"
    paths = ["--site", str(syn / "site"), "--archive-root", str(syn / "scans"), "--work-root", str(tmp_path / "작업")]
    for args in (["info"], ["synth", str(syn), "--days", "1"], ["run", *paths], ["report", *paths], ["watch", "--once", *paths]):
        code = run_cli(args, env, out)
        text = out.read_bytes().decode("utf-8")                       # UTF-8 이 아니면 여기서 깨진다
        assert code == 0, (args, text[-2000:])
        assert any("\uac00" <= ch <= "\ud7a3" for ch in text), args   # 한글이 ? 로 바뀌지 않았다
        assert "UnicodeEncodeError" not in text


def test_main_survives_no_standard_streams(monkeypatch, tmp_path):
    """pythonw(작업 스케줄러)에서는 표준 출력·오류가 None 이다 — cli.main 이 죽지 않는다 (버리는 곳으로 둔다)."""
    from minedocscan import cli

    for k in [k for k in os.environ if k.startswith("MINEDOCSCAN_")]:
        monkeypatch.delenv(k)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    assert cli.main(["info"]) == 0
    assert sys.stdout is not None and sys.stderr is not None
    with pytest.raises(SystemExit):                                   # argparse 의 오류도 찍다 죽지 않는다 (종료 코드 2 로 끝난다)
        cli.main(["없는 명령"])


def test_rejections_see_through_case_and_aliases(tmp_path):
    """거절 검사(접수·보관 폴더 안, git 작업 트리 안)가 대소문자를 바꾼 경로와 같은 폴더의 다른 이름에서도 선다. 윈도우: C:\\WORK 대
    c:\\work, 그리고 관리 공유(\\\\localhost\\C$)로 적은 같은 폴더 (닿으면). 리눅스: 심볼릭 링크로 적은 같은 폴더."""
    from minedocscan.export.writer import ExportError, check_out_dir
    from minedocscan.intake.inbox import InboxError, check_paths, within
    from minedocscan.review.export import inside_git_tree

    inbox, archive, repo = tmp_path / "Scanner", tmp_path / "Archive", tmp_path / "Repo"
    for d in (inbox, archive, repo / ".git"):
        d.mkdir(parents=True)
    st = Settings(inbox=inbox, archive_root=archive)
    aliases = []
    if WINDOWS:
        aliases.append(lambda p: Path(str(p).swapcase()))             # C:\Users\…\Scanner → c:\uSERS\…\sCANNER
        drive, rest = os.path.splitdrive(str(tmp_path))
        unc = Path(f"\\\\localhost\\{drive[0]}$" + rest)
        if os.path.isdir(unc):                                          # 관리 공유에 닿을 때만 (러너는 닿는다)
            aliases.append(lambda p, d=drive: Path(f"\\\\localhost\\{d[0]}$" + os.path.splitdrive(str(p))[1]))
    else:
        link = tmp_path.parent / (tmp_path.name + "-별칭")
        link.symlink_to(tmp_path, target_is_directory=True)
        aliases.append(lambda p: link / Path(p).relative_to(tmp_path))
    assert aliases
    for alias in aliases:
        assert within(alias(inbox) / "엑셀", inbox) and within(alias(archive) / "x" / "y", archive)
        assert not within(alias(tmp_path) / "Scanner2", inbox)        # 이름의 앞부분만 같은 옆 폴더는 아니다
        for out in (alias(inbox) / "엑셀", alias(archive) / "intake2"):
            with pytest.raises(ExportError):
                check_out_dir(out, st)
        assert check_out_dir(alias(tmp_path) / "Excel", st)
        with pytest.raises(InboxError):
            check_paths(alias(archive) / "in", archive, {})
        assert inside_git_tree(alias(repo) / "out" / "엑셀") and not inside_git_tree(alias(tmp_path) / "Excel")

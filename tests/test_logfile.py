"""serve --log-dir (tasks/0009 4.5 나): 날짜별 UTF-8 파일, 날이 바뀌면 새 파일(시계 주입), 30일 지난 것은 지운다, 표준 출력이 None 이어도."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta

import pytest

from minedocscan.logfile import DailyLog


class Clock:
    def __init__(self, t: datetime):
        self.t = t

    def __call__(self) -> float:
        return self.t.timestamp()


def test_a_new_file_each_day_utf8_and_old_ones_are_pruned(tmp_path):
    clock = Clock(datetime(2030, 3, 31, 23, 59, 0))
    old = [tmp_path / f"serve-{(clock.t - timedelta(days=d)):%Y%m%d}.log" for d in (31, 45, 30, 29)]
    for p in old:
        p.write_text("x", encoding="utf-8")
    other = tmp_path / "watch-20200101.log"                           # 다른 접두어 — 건드리지 않는다
    other.write_text("x", encoding="utf-8")
    log = DailyLog(tmp_path, clock=clock)
    assert [p.exists() for p in old] == [False, False, True, True] and other.exists()   # 30일 '지난' 것만
    log.write("처리한 문서 1건\n")
    clock.t += timedelta(minutes=2)                                   # 자정을 넘었다
    log.write("받은 문서 0건 — 끝\n")
    log.close()
    first, second = tmp_path / "serve-20300331.log", tmp_path / "serve-20300401.log"
    assert first.read_bytes().decode("utf-8") == "처리한 문서 1건\n"
    assert second.read_bytes().decode("utf-8") == "받은 문서 0건 — 끝\n"
    assert not (tmp_path / f"serve-{datetime(2030, 3, 1):%Y%m%d}.log").exists()       # 4/1 기준 31일 전 (3/1) 은 지워졌다


def test_serve_log_dir_takes_stdout_even_when_there_is_none(tmp_path, monkeypatch):
    """pythonw 처럼 표준 출력·오류가 None 이어도 serve --log-dir 이 그 폴더의 파일로 쓴다 — 시작하다 멈춘 이유도 거기에."""
    from minedocscan import cli

    for k in [k for k in list(__import__("os").environ) if k.startswith("MINEDOCSCAN_")]:
        monkeypatch.delenv(k)
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    logs = tmp_path / "logs"
    with pytest.raises(SystemExit) as e:
        cli.main(["serve", "--log-dir", str(logs)])                   # --reviewer 가 없다 — 멈춘다
    print(e.value.code, file=sys.stderr)                              # SystemExit 의 글을 찍는 것은 인터프리터 — 같은 흐름이다
    sys.stderr.flush()
    files = list(logs.glob("serve-*.log"))
    assert len(files) == 1 and "검수자를 지정하세요" in files[0].read_text(encoding="utf-8")


def test_lines_are_whole_across_threads_and_a_failed_write_is_dropped(tmp_path):
    """print 는 글과 줄바꿈을 따로 쓴다 — 두 스레드의 줄이 섞이지 않는다. 파일에 쓰지 못하면 그 줄을 버리고 다음 줄에 다시 연다
    (찍다가 serve 의 화면이 죽지 않게)."""
    import threading

    clock = Clock(datetime(2030, 3, 31, 12, 0, 0))
    log = DailyLog(tmp_path, clock=clock)

    def spam(tag: str) -> None:
        for i in range(300):
            print(f"{tag}-{i}", "끝", file=log)

    threads = [threading.Thread(target=spam, args=(t,)) for t in ("가", "나", "다")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    lines = (tmp_path / "serve-20300331.log").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 900 and all(line.count("-") == 1 and line.endswith(" 끝") for line in lines)

    class Broken:
        def write(self, text):
            raise OSError("디스크가 찼다")

        def flush(self):
            pass

        def close(self):
            pass

    log._f = Broken()
    print("버려진다", file=log)                                          # 예외가 나오지 않는다
    assert log.dropped == 1
    print("다시 열었다", file=log)
    log.close()
    assert (tmp_path / "serve-20300331.log").read_text(encoding="utf-8").splitlines()[-1] == "다시 열었다"


def test_a_bad_serve_argument_lands_in_the_log(tmp_path, monkeypatch):
    """pythonw 에서 인자가 틀리면 argparse 가 멈춘 이유도 --log-dir 의 파일에 (인자를 읽기 전에 연다)."""
    from minedocscan import cli

    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    with pytest.raises(SystemExit):
        cli.main(["serve", "--log-dir", str(tmp_path / "logs"), "--port", "열"])
    sys.stderr.flush()
    text = next((tmp_path / "logs").glob("serve-*.log")).read_text(encoding="utf-8")
    assert "--port" in text

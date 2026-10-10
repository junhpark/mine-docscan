"""엑셀의 더러운 범위만 내보내는 흔들기 (tasks/0009 4.1 자): 0007 의 다시 처리 흔들기(test_review_store.reprocess_fuzz — 검수와 결정을
씨앗으로 섞는다)에 정해 둔 걸음(계기·근무 시각·글자·수·메타·장비 이름의 검수, 날짜·버리기·되살리기 — 다른 날짜로 번지는 것 포함)을 이어
넣고, 걸음마다 그 걸음이 건드린 것(처리 + 검수)만 내보낸 바퀴(전체 훑기 없이) 뒤의 엑셀 폴더 = 그 DB 를 새 폴더에 전부 내보낸 것.
견주는 것은 기록 파일의 {경로: 모델 해시}와 폴더에 있는 파일 — xlsx 의 바이트는 만든 시각 때문에 같지 않다 (tasks/0008 12절).

합성 데이터만 — world (conftest: BUNDLES 를 한 번 돌린 DB 의 복사본). 시계는 주입한다 (잠들지 않는다). 씨앗은 싣기의 흔들기
(test_publish_pg.test_reprocess_fuzz_with_dirty_publishing)와 같다. -m slow.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import time
from dataclasses import replace
from pathlib import Path

import pytest

from minedocscan.export.auto import AutoExport
from minedocscan.export.writer import RECORD_NAME, daily_path, export_excel
from minedocscan.forms.sitepack import SitePack
from minedocscan.intake import decisions as decs
from minedocscan.pipeline.runner import Pipeline
from minedocscan.review import store as review_store
from minedocscan.review.store import Review, field_format, field_id_of, save
from minedocscan.touched import Touched

STEPS = 12
START = 1000.0
USAGE = {"u1_2030-01-07": "2030-01-07", "u2_2030-01-08": "2030-01-08", "u3_2030-01-09": "2030-01-09"}   # 가동 일보 묶음 → 날짜


class Clock:
    def __init__(self):
        self.t = START

    def __call__(self) -> float:
        return self.t


class _Stop(Exception):
    """정해진 바퀴 뒤에 흔들기를 멈춘다 (매번 같음을 보는 짧은 판)."""


def recorded(out: Path) -> dict[str, str]:
    """기록 파일의 {경로: 모델 해시}. 폴더에 있는 파일(기록 파일 밖 — 임시 파일도)이 기록의 경로와 같다: 남은 파일·빠진 파일이 없다."""
    data = json.loads((out / RECORD_NAME).read_bytes().decode("utf-8"))
    assert data["site"] == "synthetic"                             # 사본의 주인 (4.1 다)
    shas = {rel: v["sha"] for rel, v in data["files"].items()}
    present = {p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file() and p.name != RECORD_NAME}
    assert present == set(shas), sorted(present ^ set(shas))
    return shas


def exported_whole(con, site, out: Path, machine_values: bool) -> dict[str, str]:
    """그 DB 를 새 폴더에 전부 내보낸 것의 {경로: 모델 해시} (폴더는 지운다)."""
    out.mkdir()
    try:
        r = export_excel(con, site, out, full=True, machine_values=machine_values)
        assert (r.failed, r.kept, r.missing_dir, r.other_site) == ([], 0, False, False), r.as_dict()
        return recorded(out)
    finally:
        shutil.rmtree(out)


def trucks(con) -> list[dict]:
    """운행일보 TRUCK 쪽 (날짜 순) — u1·u2·u3 의 첫 쪽."""
    return [dict(r) for r in con.execute(
        "SELECT page_id, work_date, document_id FROM doc_page WHERE template_name LIKE 'synth_usage_log%' AND page_no = 1 "
        "AND status = 'loaded' ORDER BY work_date")]


def meter(con, page_id: str) -> dict[str, str]:
    u = con.execute("SELECT start_field_id, end_field_id FROM eq_usage_daily WHERE page_id = ?", (page_id,)).fetchone()
    return {"start": u["start_field_id"], "end": u["end_field_id"]}


def continuity(con, pages: list[dict]) -> list[str | None]:
    res = {r[0]: r[1] for r in con.execute("SELECT page_id, result FROM xcheck_usage WHERE check_kind = 'continuity'")}
    return [res.get(p["page_id"]) for p in pages[1:]]


def first_field(con, site, fmt: str | None, region: str) -> str:
    """적재된 쪽의 그 표에서 그 형식의 첫 칸 (field_id 순)."""
    for r in con.execute("SELECT f.field_id, f.field_name, p.template_name FROM doc_field f JOIN doc_page p ON f.page_id = p.page_id "
                         "WHERE f.region = ? AND p.status = 'loaded' ORDER BY f.field_id", (region,)):
        if field_format(site, r["template_name"], region, r["field_name"]) == fmt:
            return r["field_id"]
    raise AssertionError((fmt, region))


def scripted(world, box: Touched, round_) -> None:
    """흔들기 뒤의 정해 둔 걸음 — 씨앗의 계획이 고르지 않을 수 있는 것(계기·근무 시각 칸은 429칸 중 21칸이다, 장비 이름, 버린 것을
    되살리기, 다른 날짜로 번지는 검수·결정)을 씨앗과 관계없이 넣는다. 흔들기가 남긴 상태에서 시작하므로 먼저 가동 일보 셋을 제자리에
    (되살리고 날짜를 정한다). 바퀴마다 견주는 것은 round_ 가 한다 — 여기서는 다른 날짜의 파일이 다시 써졌는지를 더 본다."""
    from minedocscan.tools.synth import SLOTS, T_LOG
    from test_reprocess import RECEIVED

    pipe, site, st = world["pipe"], world["site"], world["st"]
    con = pipe.con
    tick = iter(range(1, 1000))

    def at() -> str:                                               # 흔들기의 검수(2030-01-10)보다 뒤
        n = next(tick)
        return f"2030-01-11T{n // 60:02d}:{n % 60:02d}:00Z"

    def review(fid: str, value: str) -> None:
        t = Touched()
        save(con, site, st, Review(fid, "value", value, "jp", reviewed_at=at()), touched=t)
        box.add(t)

    def decide(items: list[dict]) -> None:
        decs.save(con, st.decisions_path(site.root), items, "jp", received=RECEIVED, now=at())
        pipe.process_pending()
        assert pipe.pending_documents() == []

    ids = world["ids"]
    decide([{"target": t, "kind": k, **({"value": day} if k == "date" else {})}
            for name, day in USAGE.items() for t in (ids[name], f"{ids[name]}-p1", f"{ids[name]}-p2")
            for k in ("restore", "date")])
    round_("제자리")
    pages = trucks(con)
    assert [p["work_date"] for p in pages] == list(USAGE.values())
    # 계기의 사슬: 장비 이름과 계기(100→110→120→130) — 바퀴 하나에 검수 아홉
    for k, p in enumerate(pages):
        review(field_id_of(p["page_id"], "equipment"), "TRUCK")
        m = meter(con, p["page_id"])
        review(m["start"], f"{100 + 10 * k}.0")
        review(m["end"], f"{110 + 10 * k}.0")
    round_("사슬")
    assert continuity(con, pages) == ["match", "match"]
    # 계기 칸 하나 → 다음 날의 연속성
    review(meter(con, pages[0]["page_id"])["end"], "105.0")
    assert daily_path(pages[1]["work_date"]) in round_("계기").written
    second, third = continuity(con, pages)
    assert second != "match" and third == "match"
    # 장비 이름을 바꾼다 → 옛 장비의 다른 날(셋째 날 — 앞 기록이 첫날로)
    review(field_id_of(pages[1]["page_id"], "equipment"), "SHOVEL")
    assert daily_path(pages[2]["work_date"]) in round_("장비 이름").written
    # 근무 시각·글자·수·메타 (한 바퀴에)
    review(first_field(con, site, "time_range", "shifts"), "08:00~17:00")
    review(first_field(con, site, None, "work"), "oil leak")
    review(first_field(con, site, "integer", "haul"), "11")
    log = con.execute("SELECT page_id FROM doc_page WHERE template_name = ? AND status = 'loaded' ORDER BY page_id",
                      (T_LOG,)).fetchone()[0]
    review(field_id_of(log, "vehicle_no"), SLOTS[1][1])
    round_("칸")
    # 첫날의 문서를 버린다 → 셋째 날의 연속성(앞 기록이 없어진다), 되살린다
    decide([{"target": pages[0]["document_id"], "kind": "discard"}])
    assert daily_path(pages[2]["work_date"]) in round_("버리기").written
    decide([{"target": pages[0]["document_id"], "kind": "restore"}])
    assert daily_path(pages[2]["work_date"]) in round_("되살리기").written
    # 쪽의 날짜를 흔들기가 쓰지 않는 날짜로, 다시 다른 날짜로 — 앞의 날짜의 파일이 지워진다
    decide([{"target": pages[2]["page_id"], "kind": "date", "value": "2030-01-12"}])
    assert daily_path("2030-01-12") in round_("쪽 날짜").written
    decide([{"target": pages[2]["page_id"], "kind": "date", "value": "2030-01-13"}])
    r = round_("쪽 날짜 다시")
    assert r.deleted == [daily_path("2030-01-12")] and daily_path("2030-01-13") in r.written


def field_formats(con, site) -> dict[str, str | None]:
    """표 칸의 형식 {field_id: 형식 | None}."""
    return {r["field_id"]: field_format(site, r["template_name"], r["region"], r["field_name"]) for r in con.execute(
        "SELECT f.field_id, f.region, f.field_name, p.template_name FROM doc_field f JOIN doc_page p ON f.page_id = p.page_id "
        "WHERE f.region <> 'fields'")}


def covered(world, formats: dict[str, str | None]) -> tuple[set, set]:
    """검수·결정 파일에 남은 것: ({칸의 형식 | 표 밖 필드의 이름}, {(결정의 종류, 문서 | 쪽)})."""
    st, site = world["st"], world["site"]
    reviewed = {formats[r.field_id] if r.field_id in formats else r.field_id.split(":")[2]
                for _seq, r in review_store.load(st.reviews_path(site.root))[0]}
    decided = {(d.kind, "page" if decs.split_target(d.target)[1] else "doc")
               for _seq, d in decs.load(st.decisions_path(site.root))[0]}
    return reviewed, decided


def dirty_fuzz(world, out: Path, seed: int, stop_after: int | None = None) -> tuple[list[tuple], int]:
    """시작할 때의 전체 훑기 한 번, 그 뒤 걸음마다 그 걸음이 건드린 것만 내보내고 견준다 (흔들기 STEPS 걸음 + 정해 둔 걸음).
    stop_after: 그 수의 더러운 바퀴 뒤에 멈춘다. 돌려주는 값: (바퀴마다 (쓴 파일 수, 지운 파일 수, {경로: 해시}) — 시작할 때의 훑기부터,
    처리가 "전부"를 남긴 바퀴의 수)."""
    from test_review_store import reprocess_fuzz

    pipe, site = world["pipe"], world["site"]
    con = pipe.con
    formats = field_formats(con, site)                             # 흔들기가 고르는 칸 (버린 쪽의 칸도 — 처음의 DB 에서)
    clock = Clock()
    x = AutoExport(replace(world["st"], excel_dir=out, export_sweep_minutes=0.0), site, clock=clock,
                   now=lambda: "2030-02-01T00:00:00Z")
    assert x.enabled, x.reason
    trace: list[tuple] = []
    everything = 0

    def compare(n, r) -> None:
        assert r is None or (r.failed, r.kept, r.missing_dir, r.other_site) == ([], 0, False, False), (seed, n, r.as_dict())
        have = recorded(out)
        want = exported_whole(con, site, out.parent / f"전부-{len(trace)}", x.machine_values)
        assert have == want, (seed, n, sorted(k for k in have.keys() | want.keys() if have.get(k) != want.get(k)))
        trace.append((len(r.written) if r else 0, len(r.deleted) if r else 0, have))

    out.mkdir()
    first = x.after_round(con, Touched())                          # 시작할 때의 전체 훑기
    assert first is not None and first.written and x.last_sweep == START
    compare("start", first)
    box = Touched()

    def round_(n):
        nonlocal everything
        t, pipe.touched = pipe.touched, Touched()
        t.add(box)
        box.__init__()
        everything += t.everything
        before = x.last_sweep
        clock.t += 60.0                                            # 전체 훑기였다면 last_sweep 이 움직인다
        r = x.after_round(con, t)
        # 더러운 범위만 (sweep_minutes = 0). 이 world 의 걸음은 장비 마스터를 바꾸지 않으므로 "전부"도 없어야 한다 — 있으면 흔들기가
        # 더러운 범위의 구멍을 덮어 버린다
        assert not t.everything and x.last_sweep == before, (seed, n)
        compare(n, r)
        if stop_after is not None and len(trace) > stop_after:
            raise _Stop
        return r

    try:
        reprocess_fuzz(world, steps=STEPS, seed=seed, fresh=False, on_touched=box.add, after_step=round_)
        scripted(world, box, round_)
    except _Stop:
        assert stop_after is not None
        return trace, everything
    formats.update(field_formats(con, site))                      # 정해 둔 걸음이 고른 칸 (흔들기 앞에 없던 쪽일 수 있다)
    reviewed, decided = covered(world, formats)
    assert {"integer", "reading", "time_range", None, "equipment", "vehicle_no"} <= reviewed, reviewed
    assert {(k, t) for k in ("date", "discard", "restore") for t in ("doc", "page")} <= decided, decided
    return trace, everything


def another_world(bundles, world_db, root: Path) -> dict:
    """conftest 의 world 와 같은 것을 root 아래에 하나 더 (세션 DB 의 복사본, 원본 경로를 고쳐 둔다 — 검수·결정 파일은 root 아래)."""
    from conftest import BUNDLES
    from test_reprocess import copy_bundles, doc_ids, settings_for

    scans = copy_bundles(bundles, root / "scans", BUNDLES)
    st = settings_for(root, bundles["site"], scans)
    st.work_root.mkdir(parents=True)
    with sqlite3.connect(st.work_root / "minedocscan.db") as dst:
        world_db.backup(dst)
        for doc, rel in dst.execute("SELECT document_id, source_rel FROM doc_document").fetchall():
            dst.execute("UPDATE doc_document SET source_path = ? WHERE document_id = ?", (str(scans / rel), doc))
    dst.close()
    pipe = Pipeline(st, site=SitePack(bundles["site"]))
    return {"pipe": pipe, "site": pipe.site, "st": st, "scans": scans, "root": root, "ids": doc_ids(pipe.con),
            "bundles": bundles}


@pytest.mark.slow
@pytest.mark.parametrize("seed", [11, 12])
def test_dirty_excel_rounds_equal_exporting_the_whole_db(world, bundles, world_db, tmp_path, seed):
    """바퀴마다 더러운 범위만 내보낸 폴더 = 그 DB 를 전부 내보낸 폴더 (기록의 해시, 있는 파일). 검수(수·계기·근무 시각·글자·메타·
    장비 이름)·결정(날짜·버리기·되살리기 — 문서·쪽)이 다 들어갔다. 매번 같다: 같은 씨앗을 새 world 에서 넷째 걸음까지 다시 돌리면
    바퀴마다 쓴·지운 파일 수와 해시가 같다 (steps 를 줄이면 계획이 달라지므로 — rng.choices 가 난수를 덜 쓴다 — 같은 steps 로 돌리고
    멈춘다)."""
    t0 = time.perf_counter()
    trace, everything = dirty_fuzz(world, tmp_path / "엑셀", seed)
    took = time.perf_counter() - t0
    assert len(trace) == 1 + STEPS + 9
    t1 = time.perf_counter()
    other = another_world(bundles, world_db, tmp_path / "다시")
    again, _ = dirty_fuzz(other, tmp_path / "다시" / "엑셀", seed, stop_after=4)
    other["pipe"].con.close()
    assert len(again) == 5 and again == trace[:5]
    print(f"seed {seed}: 흔들기 {took:.1f}s, 다시 넷째 걸음까지 {time.perf_counter() - t1:.1f}s, "
          f"바퀴마다 (쓴, 지운) {[(w, d) for w, d, _h in trace]}, '전부'를 남긴 바퀴 {everything}")


def sliced_fuzz(world, out: Path, seed: int) -> list[int]:
    """조각으로 돈 한 바퀴 = 전체 훑기 (tasks/0009 4.2 가): 걸음마다 검수(건드린 것을 넘기지 않는다 — 다른 프로세스의 검수처럼 훑기만
    잡는다)·결정(처리가 건드린 것은 넘긴다)을 넣고, 바퀴 하나를 끝까지(달마다 작업 바퀴 하나) 돌린 뒤 폴더 = 그 DB 를 전부 내보낸 것.
    돌려주는 값: 바퀴마다의 조각 수."""
    from test_review_store import reprocess_fuzz

    pipe, site, st = world["pipe"], world["site"], world["st"]
    con = pipe.con
    decs.save(con, st.decisions_path(site.root), [{"target": world["ids"]["d_2030-01-08"], "kind": "date",
                                                   "value": "2030-02-08"}], "jp")
    pipe.process_pending()                                         # 달이 둘 (흔들기가 다시 옮길 수 있다)
    pipe.touched = Touched()
    clock = Clock()
    x = AutoExport(replace(st, excel_dir=out, export_sweep_minutes=1.0), site, clock=clock, now=lambda: "2030-02-01T00:00:00Z")
    out.mkdir()
    parts: list[int] = []

    def cycle(first: Touched) -> None:
        clock.t += 61.0                                            # sweep_minutes(1분)이 지났다 — 새 바퀴
        r = x.after_round(con, first)
        assert x.status["sweep"] is not None or x.status["last_sweep_at"], seed
        n = 1
        while x.sweep.running:
            r = x.after_round(con, Touched())
            n += 1
            assert n < 20, seed
        assert r is None or (r.failed, r.kept, r.missing_dir) == ([], 0, False), (seed, r.as_dict())
        parts.append(n)
        have = recorded(out)
        want = exported_whole(con, site, out.parent / f"전부-{len(parts)}", x.machine_values)
        assert have == want, (seed, len(parts), sorted(k for k in have.keys() | want.keys() if have.get(k) != want.get(k)))

    cycle(Touched())

    def step(n):
        t, pipe.touched = pipe.touched, Touched()
        cycle(t)

    reprocess_fuzz(world, steps=STEPS, seed=seed, fresh=False, on_touched=lambda t: None, after_step=step)
    return parts


@pytest.mark.slow
@pytest.mark.parametrize("seed", [21, 22])
def test_a_sliced_sweep_equals_exporting_the_whole_db(world, tmp_path, seed):
    """조각(달)마다 한 작업 바퀴로 한 바퀴를 다 돌면 폴더 = 그 DB 를 전부 내보낸 것 — 더러운 범위에 들지 않은 검수(다른 프로세스)도."""
    parts = sliced_fuzz(world, tmp_path / "엑셀", seed)
    assert len(parts) == 1 + STEPS and max(parts) >= 2, parts      # 달이 둘 이상인 바퀴가 있었다

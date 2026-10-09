"""월별 엑셀과 운반 표 (tasks/0008 단계 3).

운반 표 = 날짜 × 주·야 × 자리 × 광종·편의 일보 횟수. 정답(truth.json)과 견주고, 자리 미정·모름·문서 없음·합계의 규칙을 본다.
세션 픽스처(기본 묶음의 null·oracle, 가동 일보 oracle, 불변식 시험의 묶음)를 쓰고 새로 돌리는 것은 월말에 시작하는 이틀치 하나뿐.
"""
from __future__ import annotations

from collections import Counter
from types import SimpleNamespace

import pytest

from conftest import clone_db
from minedocscan.config import ConfigError, Settings
from minedocscan.export import labels as L
from minedocscan.export.monthly import haul_columns, monthly_book
from minedocscan.export.writer import export_excel, monthly_path
from minedocscan.export.xlsx import read_values
from minedocscan.forms.sitepack import SitePack, _haul_table
from minedocscan.pipeline import Pipeline
from minedocscan.review.store import review_from_field, save
from minedocscan.store.db import delete_pages, read_txn
from minedocscan.tools.synth import generate
from minedocscan.validate.crosscheck import crosscheck_haul

SHIFT = {v: k for k, v in L.SHIFT.items()}


def days_of(con, month: str | None = None) -> list[str]:
    days = [r[0] for r in con.execute("SELECT DISTINCT work_date FROM doc_page WHERE work_date IS NOT NULL ORDER BY 1")]
    return [d for d in days if month is None or d.startswith(month)]


def month_book(con, site, month: str, **kw) -> dict:
    with read_txn(con):
        return monthly_book(con, site, month, days_of(con, month), **kw)


def sheet(b: dict, name: str) -> dict | None:
    return next((s for s in b["sheets"] if s["name"] == name), None)


def haul_table(b: dict):
    """운반 표 → ({(날짜, 주야 키, 블록 제목, 열 이름): 칸}, 블록 제목들, 출처 {(날짜, 블록): 출처})."""
    s = sheet(b, L.SHEETS["haul_table"])
    top, names = s["rows"][0], s["rows"][1]
    starts = [i for i, c in enumerate(top) if c[1] == "title"] + [len(names)]
    spans = {top[a][0]: (a, z) for a, z in zip(starts, starts[1:], strict=False)}
    table, sources = {}, {}
    for row in s["rows"][2:]:
        day, shift = row[0][0], SHIFT.get(row[1][0], row[1][0])
        for title, (a, z) in spans.items():
            for j in range(a, z):
                if names[j][0] == L.SOURCE:
                    if row[j][0]:
                        sources[(day, title)] = row[j][0]
                    continue
                table[(day, shift, title, names[j][0])] = row[j]
    return table, list(spans), sources


def slot_titles(titles: list[str]) -> list[str]:
    return [t for t in titles if not t.startswith(L.UNRESOLVED_SLOT)]


def test_haul_table_matches_the_truth(oracle_run, synth):
    con, site = oracle_run.con, oracle_run.site
    b = month_book(con, site, "2030-01")
    table, titles, _ = haul_table(b)
    truth = {(d["date"], h["slot"], f"{h['material']}|{h['level']}", h["shift"]): h["trips"]
             for d in synth.truth["days"] for h in d["haul_log"]}
    has_log = {(d["date"], t["slot"]) for d in synth.truth["days"] for t in d["trucks"] if t["has_log"]}
    assert slot_titles(titles) == ["T01", "T02", "T03", "T04"] and titles == slot_titles(titles)   # 자리 미정이 없다
    got_values = {}
    no_doc = Counter()
    for (day, shift, title, col), (value, style) in table.items():
        if col == L.HAUL_TABLE_SUM:
            continue
        if (day, title) not in has_log:                           # 일보를 내지 않은 차량 (합성 셋째 날 T02) — 문서 없음
            assert (value, style) == (L.NO_DOC_MARK, "no_doc")
            no_doc[(day, title)] += 1
            continue
        if isinstance(value, int):
            assert style.split()[0] == "value"
            got_values[(day, title, col, shift)] = value
        else:
            assert value in (None, L.PENDING_MARK)
    assert got_values == truth
    assert set(no_doc) == {(d["date"], t["slot"]) for d in synth.truth["days"] for t in d["trucks"] if not t["has_log"]}
    # 교차검증 불일치인 칸은 표시하고(주·야 두 칸) 값은 일보의 것 그대로
    mism = {(r[0], r[1], f"{r[2]}|{r[3]}") for r in con.execute(
        "SELECT work_date, slot, material, level FROM xcheck_haul WHERE status = 'mismatch'")}
    assert mism
    flagged = {(day, title, col) for (day, _s, title, col), (_v, style) in table.items() if "mismatch" in style.split()}
    assert flagged == mism
    for (day, shift, title, col), (value, _style) in table.items():
        if (day, title, col) in mism:
            assert value == truth.get((day, title, col, shift)) or value is None


def line_totals(table: dict):
    """(날짜, 주야, 블록) → (칸들, 합계 칸)."""
    lines: dict[tuple, list] = {}
    totals = {}
    for (day, shift, title, col), c in table.items():
        if col == L.HAUL_TABLE_SUM:
            totals[(day, shift, title)] = c
        else:
            lines.setdefault((day, shift, title), []).append(c)
    return {k: (v, totals[k]) for k, v in lines.items()}


def test_totals_only_for_fully_confirmed_lines(oracle_run, null_run, synth, tmp_path):
    con, site = oracle_run.con, oracle_run.site
    for (_day, _shift, _title), (cells, total) in line_totals(haul_table(month_book(con, site, "2030-01"))[0]).items():
        styles = {c[1].split()[0] for c in cells if c[1]}
        if styles & {"no_doc", "unknown"}:
            assert total[0] is None                                # 문서 없음·모름인 블록에는 합계가 없다
        elif styles <= {"value", "empty"}:
            assert total == [sum(c[0] for c in cells if isinstance(c[0], int)), "value"]
        else:
            assert total == [None, "pending"]
    # null: 값이 있는 칸이 있는 줄의 합계는 비어 있고 표시된다 → 그 줄의 칸을 전부 검수하면 합계가 생긴다
    con = clone_db(null_run.con)
    site = null_run.site
    st = Settings(site=site.root, reviews=tmp_path / "reviews.jsonl")
    lines = line_totals(haul_table(month_book(con, site, "2030-01"))[0])
    key, (cells, total) = next((k, v) for k, v in lines.items() if any(c[1].startswith("pending") for c in v[0]))
    assert total == [None, "pending"]
    day, shift, slot = key
    truth = {(h["material"], h["level"]): h["trips"] for d in synth.truth["days"] if d["date"] == day for h in d["haul_log"]
             if h["slot"] == slot and h["shift"] == shift}
    rows = con.execute("SELECT h.source_field_id, h.material, h.level FROM prod_haul h WHERE h.work_date = ? AND h.slot = ? "
                       "AND h.source_role = 'log' AND h.shift = ? AND h.review_status = 'pending'", (day, slot, shift)).fetchall()
    for fid, m, lv in rows:
        v = truth.get((m, lv))
        save(con, site, st, review_from_field(con, fid, "value" if v else "empty", str(v or ""), "jp"))
    cells2, total2 = line_totals(haul_table(month_book(con, site, "2030-01"))[0])[key]
    assert total2 == [sum(truth.values()), "value"]
    assert all(c[1].split()[0] in ("value", "empty") for c in cells2 if c[1])


def test_unresolved_logs_and_unknown_slots(world_db, bundles):
    """같은 차량의 일보가 한 장 더 있는 날(2030-01-07 의 c#2 — 자리를 a#2 가 가졌다): 둘째 장은 자리 미정 블록, 그 자리의 블록은 첫 장.
    일보가 없는 자리는 그 날 자리 미정 쪽이 있으니 모름. 01-08 은 자리 미정이 없어 문서 없음."""
    site = SitePack(bundles["site"])
    con = world_db
    b = month_book(con, site, "2030-01")
    table, titles, sources = haul_table(b)
    assert f"{L.UNRESOLVED_SLOT} 1" in titles and f"{L.UNRESOLVED_SLOT} 2" not in titles
    assert sources == {("2030-01-07", f"{L.UNRESOLVED_SLOT} 1"): "c_2030-01-07#2"}
    for slot in ("T03", "T04"):
        assert {table[k][1] for k in table if k[0] == "2030-01-07" and k[2] == slot and k[3] != L.HAUL_TABLE_SUM} == {"unknown"}
        assert {table[k][1] for k in table if k[0] == "2030-01-08" and k[2] == slot and k[3] != L.HAUL_TABLE_SUM} == {"no_doc"}
    t01 = {(k[1], k[3]): v for k, v in table.items() if k[0] == "2030-01-07" and k[2] == "T01" and k[3] != L.HAUL_TABLE_SUM}
    a_page = con.execute("SELECT page_id FROM doc_page p JOIN doc_document d ON p.document_id = d.document_id "
                         "WHERE d.source_name = 'a_2030-01-07' AND p.page_no = 2").fetchone()[0]
    marked = {(r["shift"], f"{r['material']}|{r['level']}") for r in con.execute(
        "SELECT * FROM prod_haul WHERE page_id = ? AND (has_value = 1 OR review_status = 'pending')", (a_page,))}
    assert {k for k, v in t01.items() if v[0] is not None and v[1] != "no_doc"} == marked
    check_no_log_value_is_lost(con, b, "2030-01")
    xs = [r for r in xcheck_rows(b) if r["자리"] == L.UNRESOLVED_SLOT]
    assert xs and all(r["차량번호"] is None for r in xs)            # 교차검증의 키(unresolved:<차량번호>)를 싣지 않는다
    # 일별 업무 시트: 자리 미정 쪽의 교차검증 행은 그 쪽의 운반 행이 확정일 때만 횟수 (null 처리 — 값이 있는 칸은 검수 대기)
    from test_export_excel import book, business_rows, check_business

    for day in days_of(con):
        d = book(con, site, day)
        check_business(con, d, day, site)
    rows = [r for r in business_rows(book(con, site, "2030-01-07"), "xcheck_haul") if r["자리"] == L.UNRESOLVED_SLOT]
    assert any(r["판정"].endswith(L.PROVISIONAL) for r in rows) and all(r["일보 횟수"] is None for r in rows
                                                                        if r["판정"].endswith(L.PROVISIONAL))


def xcheck_rows(b: dict) -> list[dict]:
    s = sheet(b, L.LONG["xcheck_haul"])
    names = [c[0] for c in s["rows"][0]]
    return [{n: c[0] for n, c in zip(names, r, strict=True)} for r in s["rows"][1:]]


def check_no_log_value_is_lost(con, b: dict, month: str) -> None:
    """그 달의 일보 운반 행 가운데 값이 있거나 검수 대기인 것의 수 = 운반 표에서 값·검수 대기·판독 불가·● 인 칸의 수 + 겹침 칸이 덮는
    행의 수 (tasks/0009 4.1 가 — 한 쪽에 같은 광종·편·주야의 행이 둘 이상)."""
    n_rows = con.execute("SELECT COUNT(*) FROM prod_haul WHERE source_role = 'log' AND work_date LIKE ? "
                         "AND (has_value = 1 OR review_status = 'pending')", (f"{month}-%",)).fetchone()[0]
    covered = con.execute(
        "SELECT COALESCE(SUM(n), 0) FROM (SELECT SUM(has_value = 1 OR review_status = 'pending') AS n FROM prod_haul "
        "WHERE source_role = 'log' AND work_date LIKE ? GROUP BY page_id, material, level, shift HAVING COUNT(*) > 1)",
        (f"{month}-%",)).fetchone()[0]
    table, _t, _s = haul_table(b)
    n_cells = sum(1 for (_d, _sh, _t2, col), c in table.items() if col != L.HAUL_TABLE_SUM
                  and c[1] and c[1].split()[0] in ("value", "pending", "illegible", "present"))
    assert n_rows == n_cells + covered and n_rows > 0


def test_logs_without_a_matrix_go_to_unresolved_blocks(world_db, bundles):
    """행렬 쪽이 없는 날(01-08 의 행렬을 지운다): 일보가 전부 자리 미정 블록에(쪽의 순서대로, 블록마다 출처), 자리의 블록은 모름."""
    site = SitePack(bundles["site"])
    con = clone_db(world_db)
    matrix = [r[0] for r in con.execute("SELECT DISTINCT page_id FROM prod_haul WHERE source_role = 'matrix' "
                                        "AND work_date = '2030-01-08'")]
    assert matrix
    delete_pages(con, matrix)
    crosscheck_haul(con, dates=["2030-01-08"])
    con.commit()
    b = month_book(con, site, "2030-01")
    table, titles, sources = haul_table(b)
    assert sources[("2030-01-08", f"{L.UNRESOLVED_SLOT} 1")] == "d_2030-01-08#1"
    for slot in slot_titles(titles):
        assert {table[k][1] for k in table if k[0] == "2030-01-08" and k[2] == slot and k[3] != L.HAUL_TABLE_SUM} == {"unknown"}
    check_no_log_value_is_lost(con, b, "2030-01")
    xs = [r for r in xcheck_rows(b) if r["날짜"] == "2030-01-08"]
    assert xs and {r["자리"] for r in xs} == {L.UNRESOLVED_SLOT}


def test_column_and_slot_order(oracle_run):
    con, site = oracle_run.con, oracle_run.site
    log = site.templates["synth_haul_log"]
    rows = [f"{r['material']}|{r['level']}" for r in sorted(log.region("haul")["rows"], key=lambda r: r["row"])]
    s = sheet(month_book(con, site, "2030-01"), L.SHEETS["haul_table"])
    names = [c[0] for c in s["rows"][1]]
    first = names[2:2 + len(rows)]
    assert first == rows
    custom = SimpleNamespace(templates=site.templates, haul_table={"columns": ["WASTE|L1", "ORE|L0"], "slots": ["T03"]})
    s2 = sheet(month_book(con, custom, "2030-01"), L.SHEETS["haul_table"])
    names2 = [c[0] for c in s2["rows"][1]]
    assert names2[2:2 + len(rows)] == ["WASTE|L1", "ORE|L0", *(r for r in rows if r not in ("WASTE|L1", "ORE|L0"))]
    assert [c[0] for c in s2["rows"][0] if c[1] == "title"] == ["T03", "T01", "T02", "T04"]   # 없는 것도 뒤에
    for bad in ({"columns": "ORE|L0"}, {"columns": ["ORE"]}, {"slots": [1]}, {"slots": ["T1", "T1"]}, {"other": 1}):
        with pytest.raises(ConfigError):
            _haul_table({"haul_table": bad})
    assert _haul_table({}) == {"columns": [], "slots": []}


def test_long_tables_and_day_summary(oracle_run, usage_run):
    for pipe in (oracle_run, usage_run["pipe"]):
        con, site = pipe.con, pipe.site
        month = days_of(con)[0][:7]
        days = days_of(con, month)
        b = month_book(con, site, month)
        haul = sheet(b, L.LONG["haul"])
        if haul is not None:
            names = [c[0] for c in haul["rows"][0]]
            rows = [{n: c[0] for n, c in zip(names, r, strict=True)} for r in haul["rows"][1:]]
            assert all(r["횟수"] is not None or r[L.STATE] in (L.ROW_STATE["pending"], L.ROW_STATE["illegible"]) for r in rows)
            n = con.execute("SELECT COUNT(*) FROM prod_haul WHERE work_date LIKE ? AND (has_value = 1 OR review_status = 'pending')",
                            (f"{month}-%",)).fetchone()[0]
            assert len(rows) == n
        for key, sql in (("xcheck_haul", "SELECT COUNT(*) FROM xcheck_haul WHERE work_date LIKE ?"),
                         ("usage", "SELECT COUNT(*) FROM eq_usage_daily WHERE work_date LIKE ?"),
                         ("xcheck_usage", "SELECT COUNT(*) FROM xcheck_usage WHERE work_date LIKE ?"),
                         ("inspection", "SELECT COUNT(*) FROM insp_daily WHERE inspection_date LIKE ?"),
                         ("tally", "SELECT COUNT(*) FROM prod_tally WHERE work_date LIKE ? AND (has_value = 1 OR review_status = 'pending')")):
            n = con.execute(sql, (f"{month}-%",)).fetchone()[0]
            s = sheet(b, L.LONG[key])
            assert (len(s["rows"]) - 1 if s else 0) == n, key
        # 날짜별 요약: 날짜마다 직접 센 것과 같다
        summ = sheet(b, L.SUMMARY)["rows"]
        at = next(i for i, r in enumerate(summ) if r and [c[0] for c in r] == L.MONTH_DAY_COLUMNS)
        got = {r[0][0]: [c[0] for c in r] for r in summ[at + 1:at + 1 + len(days)]}
        assert list(got) == days
        from minedocscan.export.daily import daily_book
        from minedocscan.export.model import UNSURE

        for day in days:
            st = Counter(r[0] for r in con.execute("SELECT status FROM doc_page WHERE work_date = ?", (day,)))
            x = Counter(r[0] for r in con.execute("SELECT status FROM xcheck_haul WHERE work_date = ?", (day,)))
            with read_txn(con):
                d = daily_book(con, site, day)
            pending = next(r[2][0] for r in d["sheets"][0]["rows"] if len(r) == 3 and r[0][0] == L.SEC_PENDING
                           and r[1][0] == L.TOTAL)
            assert pending == sum(1 for s in d["sheets"][1:] if any(r and r[0] == [L.PAGE_HEAD, "h"] for r in s["rows"])
                                  for r in s["rows"] for c in r if c[1] in UNSURE)
            unres = con.execute("SELECT COUNT(DISTINCT page_id) FROM prod_haul WHERE work_date = ? AND source_role = 'log' "
                                "AND slot IS NULL", (day,)).fetchone()[0]
            u = con.execute("SELECT COUNT(*) FROM xcheck_usage WHERE work_date = ? AND result IN ('mismatch', 'gap', 'overlap')",
                            (day,)).fetchone()[0]
            assert got[day] == [day, sum(st.values()), sum(st.values()) - st["loaded"], pending, x["match"], x["mismatch"],
                                x["missing_log"] + x["missing_matrix"], u, unres]


@pytest.mark.slow                                  # 이틀치를 새로 돌린다 — 기본 시험 시간 (tasks/0008 6절)
def test_month_boundary_gives_one_file_per_month(tmp_path):
    syn = generate(tmp_path / "data", days=2, seed=0, start="2030-01-31")
    pipe = Pipeline(Settings(site=syn.site, archive_root=syn.scans, work_root=tmp_path / "work", save_aligned=False))
    pipe.run([syn.scans])
    out = tmp_path / "엑셀"
    out.mkdir()
    r = export_excel(pipe.con, pipe.site, out, full=True)
    assert sorted(r.written) == ["daily/2030-01/2030-01-31.xlsx", "daily/2030-02/2030-02-01.xlsx",
                                 monthly_path("2030-01"), monthly_path("2030-02")]
    for month in ("2030-01", "2030-02"):                             # 파일 = 모델 (되읽어서)
        b = month_book(pipe.con, pipe.site, month)
        got = read_values(out / monthly_path(month))
        assert list(got) == [s["name"] for s in b["sheets"]]
        for s in b["sheets"]:
            want = [[None if c[1].startswith("stamp") else c[0] for c in row] for row in s["rows"]]
            have = got[s["name"]]
            for i, row in enumerate(want):
                while row and row[-1] is None:
                    row.pop()
                hv = have[i] if i < len(have) else []
                if any(c[1].startswith("stamp") for c in s["rows"][i]):
                    assert hv[1]
                    hv, row = hv[:1], row[:1]
                assert hv == row, (s["name"], i)
    r2 = export_excel(pipe.con, pipe.site, out, full=True)
    assert r2.written == [] and r2.unchanged == 4


def test_export_command_month(null_run, tmp_path, capsys):
    """export excel --month: 그 달의 일별 파일과 월별 파일. 범위를 주지 않으면 일별·월별 모두."""
    from minedocscan.cli import main
    from minedocscan.export.writer import daily_path

    s = null_run.settings
    args = ["--site", str(s.site), "--work-root", str(s.work_root)]
    out = tmp_path / "월별"
    out.mkdir()
    assert main(["export", "excel", str(out), "--month", "2030-01", *args]) == 0
    printed = capsys.readouterr().out
    days = days_of(null_run.con, "2030-01")
    assert monthly_path("2030-01") in printed and all(daily_path(d) in printed for d in days)
    assert (out / monthly_path("2030-01")).is_file()
    assert main(["export", "excel", str(out), "--month", "2029-12", *args]) == 0       # 쪽이 없는 달 — 아무것도 쓰지 않는다
    assert not (out / monthly_path("2029-12")).exists()
    with pytest.raises(SystemExit):
        main(["export", "excel", str(out), "--month", "2030-13", *args])


def test_provisional_mismatches_are_not_flagged_in_the_haul_table(null_run, oracle_run):
    """운반 표의 불일치 표시는 판정이 확정일 때만 — null 묶음은 운반 칸이 전부 검수 대기라 불일치 판정이 있어도 표시하지 않는다."""
    con, site = null_run.con, null_run.site
    assert con.execute("SELECT COUNT(*) FROM xcheck_haul WHERE status = 'mismatch'").fetchone()[0] > 0
    table, _titles, _ = haul_table(month_book(con, site, "2030-01"))
    assert not any("mismatch" in style for (_v, style) in table.values())
    table, _titles, _ = haul_table(month_book(oracle_run.con, oracle_run.site, "2030-01"))
    assert any("mismatch" in style for (_v, style) in table.values())


# ── 겹침 (tasks/0009 4.1 가) ──────────────────────────────────────────────────
def test_overlapping_haul_rows_are_not_added(oracle_run):
    """한 일보 쪽에 같은 (광종·편, 주야)의 행이 둘이면(검사 전에 만든 템플릿) 운반 표는 더하지 않고 그 칸에 겹침, 그 줄의 합계를 비운다.
    범례의 겹침 줄은 그 파일에만 있고, 수의 보존(값·?·판독 불가 칸 + 겹침 칸이 덮는 행 = 일보 행)이 선다. 교차검증 시트는 그대로."""
    con, site = oracle_run.con, oracle_run.site
    plain = month_book(con, site, "2030-01")
    assert not any(r and r[1][1] == "overlap" for r in sheet(plain, L.SUMMARY)["rows"] if len(r) > 1)
    con = clone_db(oracle_run.con)
    a, b = con.execute("SELECT h1.haul_id, h2.haul_id FROM prod_haul h1 JOIN prod_haul h2 ON h1.page_id = h2.page_id AND "
                       "h1.shift = h2.shift AND h1.haul_id < h2.haul_id WHERE h1.source_role = 'log' AND h1.slot IS NOT NULL "
                       "AND h1.has_value = 1 AND h2.has_value = 1 AND h1.review_status = 'auto' AND h2.review_status = 'auto' "
                       "LIMIT 1").fetchone()
    ha = con.execute("SELECT * FROM prod_haul WHERE haul_id = ?", (a,)).fetchone()
    con.execute("UPDATE prod_haul SET material = ?, level = ? WHERE haul_id = ?", (ha["material"], ha["level"], b))
    con.commit()
    xs_before = sheet(month_book(clone_db(oracle_run.con), site, "2030-01"), L.LONG["xcheck_haul"])
    book = month_book(con, site, "2030-01")
    table, _t, _s = haul_table(book)
    key = f"{ha['material']}|{ha['level']}"
    hit = {k: c for k, c in table.items() if k[0] == ha["work_date"] and k[2] == ha["slot"] and k[1] == SHIFT.get(ha["shift"], ha["shift"])}
    assert [c for k, c in hit.items() if k[3] != L.HAUL_TABLE_SUM and c[1] and c[1].split()[0] == "overlap"] == [[L.OVERLAP_MARK, "overlap"]]
    assert [c for k, c in hit.items() if k[3] == L.HAUL_TABLE_SUM] == [[None, "pending"]]   # 그 줄의 합계는 비운다
    assert [k[3] for k, c in hit.items() if c[1] == "overlap"] == [dict(haul_columns(site, {key}))[key]]   # 그 광종·편의 칸
    legend = [r for r in sheet(book, L.SUMMARY)["rows"] if len(r) > 1 and r[1][1] == "overlap"]
    assert legend == [[[None, ""], [L.OVERLAP_MARK, "overlap"], [L.LEGEND_OVERLAP[2], ""]]]
    check_no_log_value_is_lost(con, book, "2030-01")
    assert sheet(book, L.LONG["xcheck_haul"]) == xs_before                    # 교차검증 시트는 그대로 (DB 의 교차검증 행)


def test_template_check_finds_haul_overlaps(synth, tmp_path):
    """template check: 운반 일보의 표에서 (광종, 편)이 같은 행이 둘 이상이거나 횟수 열의 shift 가 겹치면 오류 (값은 찍지 않는다)."""
    import shutil

    import yaml

    from minedocscan.tools.tpltools import check_template

    src = synth.site / "templates" / "synth_haul_log"
    assert not [p for p in check_template(src) if "겹" in p]
    for case in ("rows", "shift"):
        d = tmp_path / case / "synth_haul_log"
        shutil.copytree(src, d)
        spec = yaml.safe_load((d / "template.yaml").read_text(encoding="utf-8"))
        reg = spec["regions"][0]
        if case == "rows":
            reg["rows"][1]["material"], reg["rows"][1]["level"] = reg["rows"][0]["material"], reg["rows"][0]["level"]
        else:
            night = next(c for c in reg["columns"] if c.get("shift") == "night")
            night["shift"] = "day"
        (d / "template.yaml").write_text(yaml.safe_dump(spec, allow_unicode=True, sort_keys=False), encoding="utf-8")
        probs = check_template(d)
        want = "(광종, 편)이 같은 행" if case == "rows" else "횟수 열의 shift 가 겹칩니다"
        assert any(want in p for p in probs), probs
        assert not any(reg["rows"][0]["material"] in p for p in probs if want in p)   # 값은 찍지 않는다
    # 핸들러가 적는 그대로: 편 1 과 "1" 은 같은 칸 (prod_haul 은 글자로 적는다), 소수 형식의 숫자 열은 횟수 열이 아니다 (행이 되지 않는다)
    d = tmp_path / "str" / "synth_haul_log"
    shutil.copytree(src, d)
    spec = yaml.safe_load((d / "template.yaml").read_text(encoding="utf-8"))
    reg = spec["regions"][0]
    reg["rows"][0]["level"], reg["rows"][1]["level"] = 1, "1"
    reg["rows"][1]["material"] = reg["rows"][0]["material"]
    night = next(c for c in reg["columns"] if c.get("shift") == "night")
    night["shift"], night["format"] = "day", "decimal"
    (d / "template.yaml").write_text(yaml.safe_dump(spec, allow_unicode=True, sort_keys=False), encoding="utf-8")
    probs = check_template(d)
    assert any("(광종, 편)이 같은 행" in p for p in probs), probs
    assert not any("횟수 열의 shift" in p for p in probs), probs


# ── 작은 것들 (tasks/0009 4.1 사) ──────────────────────────────────────────────────
def test_haul_table_columns_are_trimmed_around_the_bar_before_the_duplicate_check(oracle_run, tmp_path):
    """[haul_table] columns 의 항목은 '|' 의 양쪽을 다듬은 뒤에 겹침을 본다 — " ORE | L0 " 과 "ORE|L0" 은 같은 열이다
    (slots 도 다듬은 뒤에). 다듬은 열은 데이터의 키("광종|편")와 맞아 운반 표의 열 순서를 정한다."""
    with pytest.raises(ConfigError, match="columns 에 겹치는 항목"):
        _haul_table({"haul_table": {"columns": [" ORE | L0 ", "ORE|L0"]}})
    with pytest.raises(ConfigError, match="slots 에 겹치는 항목"):
        _haul_table({"haul_table": {"slots": [" T03", "T03"]}})
    assert _haul_table({"haul_table": {"columns": ["ORE | L0"]}}) == {"columns": ["ORE|L0"], "slots": []}
    root = tmp_path / "site"
    root.mkdir()
    (root / "site.toml").write_text('[haul_table]\ncolumns = [" WASTE | L1", "ORE |L0 "]\nslots = [" T03 "]\n',
                                    encoding="utf-8")
    pack = SitePack(root)
    assert pack.haul_table == {"columns": ["WASTE|L1", "ORE|L0"], "slots": ["T03"]}
    custom = SimpleNamespace(templates=oracle_run.site.templates, haul_table=pack.haul_table)
    s = sheet(month_book(oracle_run.con, custom, "2030-01"), L.SHEETS["haul_table"])
    assert [c[0] for c in s["rows"][1]][2:4] == ["WASTE|L1", "ORE|L0"]
    assert [c[0] for c in s["rows"][0] if c[1] == "title"][0] == "T03"


def test_other_shift_names_follow_day_and_night_in_name_order(oracle_run):
    """월별 운반 표의 주야 순서: 주간 → 야간 → 없음, 그 밖의 이름은 이름 순 (tasks/0009 4.1 사 — 집합을 도는 순서는 프로세스마다
    달라 같은 DB 인데 파일을 다시 썼다). 쪽마다 한 주야의 행을 다른 이름으로 바꾼다 (한 쪽의 한 칸에 두 행이 떨어지지 않게)."""
    con = clone_db(oracle_run.con)
    pages = [r[0] for r in con.execute("SELECT DISTINCT page_id FROM prod_haul WHERE source_role = 'log' ORDER BY page_id")]
    renames = [("day", "b"), ("night", "a"), ("day", "c2"), ("night", "c10"), ("day", None)]
    assert len(pages) >= len(renames)
    for pid, (old, new) in zip(pages, renames, strict=False):
        n = con.execute("UPDATE prod_haul SET shift = ? WHERE page_id = ? AND source_role = 'log' AND shift = ?",
                        (new, pid, old)).rowcount
        assert n > 0, pid
    con.commit()
    want = [L.SHIFT["day"], L.SHIFT["night"], None, "a", "b", "c10", "c2"]
    s = sheet(month_book(con, oracle_run.site, "2030-01"), L.SHEETS["haul_table"])
    by_day: dict[str, list] = {}
    for r in s["rows"][2:]:
        by_day.setdefault(r[0][0], []).append(r[1][0])
    assert by_day and all(v == want for v in by_day.values()), by_day
    assert not any(c[1].split()[0] == "overlap" for r in s["rows"] for c in r if c[1])          # 겹침을 만들지 않았다

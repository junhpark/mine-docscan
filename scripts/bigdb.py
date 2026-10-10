"""규모를 재는 도구 (tasks/0009 4.2 마) — 설치되는 패키지에 들어가지 않는다 (scripts/).

  python scripts/bigdb.py make OUT [--days 252] [--rows-per-day 5400] [--source-days 5] [--seed 0]
      합성 묶음(운반·점검표·가동 일보)을 null 로 한 번 돌린 작업 DB 를 복제해 한 해 규모로 만든다.
      - 평일 하루마다 문서 하나 (실제처럼 하루치 묶음 PDF 하나): 원본의 하루치 쪽을 밀도만큼 되풀이해 **하루의 행 수를 실제(약 5,400행)에**
        맞춘다 (합성 하루는 약 600행 — 날짜만 늘리면 실제의 9분의 1 이다).
      - ID·날짜를 바꾼다 (문서 ID 는 해시, 쪽 ID = <문서>-p<쪽>, 필드 ID 는 쪽 ID 를 앞에). 날짜로 만드는 업무 표(교차검증·배차 관측·
        점검 행·계기 검산)는 베끼지 않고 **마무리(finalize)를 전부 다시** 한다 — 옛 연속성·교차검증 행을 베끼면 다시 계산이 모든 사슬을
        바꿔 수치가 틀린다.
      OUT/site (사이트 팩 — 합성), OUT/work/minedocscan.db, OUT/bigdb.json (만든 것의 수 — 값 없이).

  MINEDOCSCAN_TEST_PG_URL=… python scripts/bigdb.py measure OUT --label before|after --json FILE
      그 체크아웃의 코드(PYTHONPATH 로 고른다 — 고치기 전과 뒤를 같은 기계에서)로 잰다. 일마다 하위 프로세스 하나 —
      최대 메모리 = 그 프로세스의 최대 RSS − import 만 한 기준선 (resource.getrusage). 재는 것:
      - 엑셀: 자동 내보내기의 처음 훑기(조각마다의 시간 — 보고만), 바뀐 것 없는 훑기(조각마다의 시간·최대 메모리),
        가동 일보의 계기 칸 하나를 검수한 바퀴(더러운 날짜의 수, 시간), 명령 export excel 의 시간 (보고만).
      - 싣기 (PostgreSQL — 이 측정만의 스키마, 끝나면 지운다): 명령 publish 의 처음·바뀐 것 없을 때(시간·메모리), 자동 싣기의
        바뀐 것 없는 훑기(조각마다의 시간·최대 메모리), 계기 칸 하나의 바퀴.
      싣기의 대상은 환경변수 MINEDOCSCAN_TEST_PG_URL 로만 받는다 (명령줄에 비밀번호를 적지 않게). 없으면 싣기는 재지 않는다 —
      보고서에서 그 기준은 "재지 않음"(통과가 아니다). URL·비밀번호는 찍지 않는다 (결과에는 서버의 판만).

  python scripts/bigdb.py report BEFORE.json AFTER.json --out docs/test-report/scale-<날짜>.json
      두 측정을 견주어 기준(4.2 마)과 같이 적는다.

합성 데이터만 쓴다. 실데이터의 수치(1절의 표)는 실제 3일치를 복제한 DB 로 쟀던 것이고 여기서는 다시 재지 않는다.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import resource
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import date, timedelta
from pathlib import Path

COPY_TABLES = ("doc_document", "doc_page", "doc_field", "doc_page_meta", "doc_page_sig", "prod_haul", "eq_usage_daily", "prod_tally")
DATE_COLS = {"work_date", "inspection_date"}
# 실제 3일치(83쪽)를 252일로 복제한 DB 의 하루 행 수 (tasks/0009 1절 다 — 135만 행 / 252일)
REAL_ROWS_PER_DAY = 5400


# ── make ────────────────────────────────────────────────────────────────────
def weekdays(start: date, n: int) -> list[str]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def publish_rows(con: sqlite3.Connection) -> int:
    from minedocscan.store.db import PUBLISH_TABLES

    return sum(con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in PUBLISH_TABLES)


def make(out: Path, days: int, rows_per_day: int, source_days: int, seed: int) -> dict:
    from minedocscan.config import Settings
    from minedocscan.forms.sitepack import SitePack
    from minedocscan.pipeline.runner import Pipeline
    from minedocscan.tools.synth import generate

    out.mkdir(parents=True, exist_ok=True)
    src = out / "src"
    if src.exists():
        shutil.rmtree(src)
    g = generate(src, days=source_days, seed=seed, usage_logs=True)
    site_dir = out / "site"
    if site_dir.exists():
        shutil.rmtree(site_dir)
    shutil.copytree(g.site, site_dir)
    st = Settings(site=site_dir, archive_root=g.scans, work_root=out / "src-work", recognizer="null",
                  reviews=out / "src-work" / "reviews.jsonl", save_aligned=False)
    pipe = Pipeline(st, site=SitePack(site_dir))
    pipe.run([g.scans])
    s = pipe.con
    s.row_factory = sqlite3.Row
    src_days = sorted(r[0] for r in s.execute("SELECT DISTINCT work_date FROM doc_page WHERE work_date IS NOT NULL"))
    per_day = publish_rows(s) / len(src_days)
    density = max(1, round(rows_per_day / per_day))
    info = replicate(s, site_dir, out / "work", days, density, seed)
    info.update(source_days=len(src_days), source_rows_per_day=round(per_day))
    (out / "bigdb.json").write_text(json.dumps(info, indent=1), encoding="utf-8")
    return info


def replicate(s: sqlite3.Connection, site_dir: Path, work: Path, days: int, density: int, seed: int = 0) -> dict:
    """작업 DB s 를 work/minedocscan.db 로 복제한다: 평일 days 일, 날마다 문서 하나에 원본 하루치의 쪽을 density 번 (원본의 날짜를
    차례로 돌려 쓴다). 그 뒤 계기를 채우고 마무리를 전부 다시 한다. 시험(slow)도 이것을 쓴다 — 세션의 작업 DB 에서."""
    from minedocscan.config import Settings
    from minedocscan.forms.sitepack import SitePack
    from minedocscan.pipeline.runner import Pipeline
    from minedocscan.store.db import open_db, upsert

    s.row_factory = sqlite3.Row
    src_days = sorted(r[0] for r in s.execute("SELECT DISTINCT work_date FROM doc_page WHERE work_date IS NOT NULL"))
    out = work.parent
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    dst = open_db(f"sqlite:///{(work / 'minedocscan.db').as_posix()}")
    cols = {t: [r[1] for r in s.execute(f"PRAGMA table_info({t})")] for t in COPY_TABLES}
    upsert(dst, "eq_equipment", [dict(r) for r in s.execute("SELECT * FROM eq_equipment")])
    by_day: dict[str, list[sqlite3.Row]] = {}
    for p in s.execute("SELECT * FROM doc_page WHERE work_date IS NOT NULL ORDER BY work_date, document_id, page_no"):
        by_day.setdefault(p["work_date"], []).append(p)
    targets = weekdays(date(2031, 1, 6), days)
    t0 = time.monotonic()
    n_pages = 0
    for i, day in enumerate(targets):
        src_day = src_days[i % len(src_days)]
        doc = hashlib.sha256(f"bigdb|{seed}|{day}".encode()).hexdigest()[:16]
        k = 0
        page_rows = []
        for _c in range(density):
            for p in by_day[src_day]:
                k += 1
                new_page = f"{doc}-p{k}"
                old_page = p["page_id"]
                page_rows.append((old_page, new_page))
        upsert(dst, "doc_document", {"document_id": doc, "source_path": str(out / "scans" / f"{day}.pdf"),
                                     "source_rel": f"bigdb/{day}.pdf", "source_name": f"{day}.pdf", "work_date": day,
                                     "date_source": "filename", "n_pages": k, "status": "processed", "error": None,
                                     "warning": None, "created_at": "2031-01-01T00:00:00Z", "received_at": None,
                                     "work_requested": 0, "work_done": 0})
        for old_page, new_page in page_rows:
            def fix(row: sqlite3.Row, table: str, old_page=old_page, new_page=new_page, src_day=src_day, day=day,
                    doc=doc) -> dict:
                d = {}
                for col in cols[table]:
                    v = row[col]
                    if isinstance(v, str):
                        if v == old_page:
                            v = new_page
                        elif v.startswith(old_page + ":"):
                            v = new_page + v[len(old_page):]
                        elif col in DATE_COLS and v == src_day:
                            v = day
                        elif col == "document_id":
                            v = doc
                    d[col] = v
                return d
            p = s.execute("SELECT * FROM doc_page WHERE page_id = ?", (old_page,)).fetchone()
            row = fix(p, "doc_page")
            row["document_id"], row["page_no"] = doc, int(new_page.rsplit("-p", 1)[1])
            row["duplicate_of"], row["aligned_image"] = None, None
            upsert(dst, "doc_page", row)
            for t in COPY_TABLES[2:]:
                rows = [fix(r, t) for r in s.execute(f"SELECT * FROM {t} WHERE page_id = ?", (old_page,))]
                if t == "doc_page_meta":
                    for r in rows:
                        if r["meta_key"] == "date" and r["value"] == src_day:
                            r["value"] = day
                if rows:
                    upsert(dst, t, rows)
            n_pages += 1
        dst.commit()
    copied_s = time.monotonic() - t0
    meters = fill_meters(dst, SitePack(site_dir))
    t1 = time.monotonic()
    rep = Pipeline(Settings(site=site_dir, work_root=work, recognizer="null", reviews=work / "reviews.jsonl"),
                   site=SitePack(site_dir), con=dst)
    rep.finalize()                                      # 날짜로 만드는 업무 표를 처음부터 (범위 없이 — 전부)
    dst.commit()
    info = {"days": days, "seed": seed, "density": density,
            "documents": dst.execute("SELECT COUNT(*) FROM doc_document").fetchone()[0],
            "pages": n_pages, "rows": publish_rows(dst),
            "rows_by_table": {t: dst.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                              for t in ("doc_field", "prod_haul", "eq_usage_daily", "prod_tally", "insp_daily", "xcheck_haul",
                                        "xcheck_usage", "eq_assignment_obs")},
            "months": len({d[:7] for d in targets}), "meter_records": meters, "copy_s": round(copied_s, 1),
            "finalize_s": round(time.monotonic() - t1, 1)}
    info["rows_per_day"] = round(info["rows"] / days)
    dst.close()
    return info


def fill_meters(con: sqlite3.Connection, site) -> int:
    """계기 칸을 검수한 것처럼 채운다: null 인식기로 돌린 합성 가동 일보는 계기 칸이 전부 검수 대기라 연속성이 다 '모름'이다 — 실제처럼
    장비마다 이어지는 값(오늘 시작 = 어제 종료)을 사람이 넣은 값으로 적고 그 쪽의 업무 행을 핸들러로 다시 만든다 (마무리가 연속성을 낸다).
    값은 합성 — 장비마다 1000 부터 하루 8.0–10.0 씩."""
    from minedocscan.handlers.usage import rebuild_page
    from minedocscan.validate.usage import equipment_ref

    names = sorted(site.equipment_aliases)                # 대응표의 이름 (합성 — LOADER, SHOVEL …)
    chain: dict[str, float] = {}
    pos: dict[str, int] = {}
    n = 0
    for u in con.execute("SELECT * FROM eq_usage_daily WHERE reading_kind = 'pending' ORDER BY work_date, page_id").fetchall():
        k = pos[u["work_date"]] = pos.get(u["work_date"], -1) + 1
        name = names[k % len(names)]                      # 장비명 필드도 검수한 것처럼 (null 은 읽지 않는다 — 장비가 없으면 사슬이 없다)
        con.execute("UPDATE doc_page_meta SET value = ?, source = 'review' WHERE page_id = ? AND meta_key = 'equipment'",
                    (name, u["page_id"]))
        ref = equipment_ref({"equipment_id": site.equipment_id_of(name), "equipment": name})
        start = chain.get(ref, 1000.0 + 37.0 * len(chain))
        end = round(start + 8.0 + (n % 5) * 0.5, 1)
        for fid, v in ((u["start_field_id"], start), (u["end_field_id"], end), (u["total_field_id"], None)):
            if fid:
                con.execute("UPDATE doc_field SET value_final = ?, has_value = ?, review_status = 'reviewed', reviewed_by = 'bigdb', "
                            "reviewed_at = '2031-01-01T00:00:00Z' WHERE field_id = ?",
                            ("" if v is None else f"{v:.1f}", 0 if v is None else 1, fid))
        chain[ref] = end
        tpl = site.templates[u["source_form"]]
        rebuild_page(con, site, tpl, {"page_id": u["page_id"], "work_date": u["work_date"], "template_name": tpl.name})
        n += 1
    con.commit()
    return n


# ── measure (하위 프로세스 하나에 일 하나) ───────────────────────────────────────────
def _rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024           # 리눅스: KB


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self) -> float:
        return self.t


def _settings(out: Path, **kw):
    from dataclasses import replace

    from minedocscan.config import Settings

    st = Settings(site=out / "site", work_root=out / "work", recognizer="null", reviews=out / "reviews.jsonl")
    return replace(st, **kw)


def _cycle(job, con, touched_first=None, max_rounds=200) -> list[float]:
    """한 번의 전체 훑기를 다 돌 때까지 바퀴를 돈다 (고치기 전에는 바퀴 하나, 뒤에는 조각마다 하나). 바퀴마다의 시간."""
    from minedocscan.touched import Touched

    start = job.last_sweep
    times = []
    for n in range(max_rounds):
        t = touched_first if (n == 0 and touched_first is not None) else Touched()
        t0 = time.perf_counter()
        r = job.after_round(con, t)
        times.append(time.perf_counter() - t0)
        if getattr(r, "kind", None):
            raise RuntimeError(f"바퀴가 실패했다: {r.kind}")
        if job.last_sweep is not None and job.last_sweep != start:
            return times
        job.clock.t += 1.0
    raise RuntimeError("전체 훑기가 끝나지 않았다")


def _meter_review(con, site, st):
    """가동 일보의 계기 칸 하나를 검수한다 (가운데 날짜의 첫 계기 기록의 시작 칸) — 돌려주는 값: 건드린 것."""
    from minedocscan.review.store import Review, save
    from minedocscan.touched import Touched

    days = [r[0] for r in con.execute("SELECT DISTINCT work_date FROM eq_usage_daily WHERE reading_kind = 'meter' ORDER BY 1")]
    day = days[len(days) // 2]
    fid, start = con.execute("SELECT start_field_id, meter_start FROM eq_usage_daily WHERE reading_kind = 'meter' AND "
                             "work_date = ? AND start_field_id IS NOT NULL ORDER BY page_id LIMIT 1", (day,)).fetchone()
    t = Touched()
    save(con, site, st, Review(fid, "value", f"{(start or 1000.0) + 1.0:.1f}", "scale", reviewed_at="2031-12-31T00:00:00Z"),
         touched=t)
    return t


def job(name: str, out: Path, pg: str | None, label: str) -> dict:
    from minedocscan.forms.sitepack import SitePack
    from minedocscan.store.db import open_db

    res: dict = {}
    site = SitePack(out / "site")
    schema = f"scale_{label}"
    if name == "baseline":
        import openpyxl  # noqa: F401

        import minedocscan.cli  # noqa: F401
        import minedocscan.export.auto  # noqa: F401
        import minedocscan.export.monthly  # noqa: F401
        import minedocscan.publish.auto  # noqa: F401
        if pg:
            import psycopg  # noqa: F401 — 싣기를 잴 때만 (기준선에 드라이버의 몫을 넣는다)
        con = open_db(f"sqlite:///{(out / 'work' / 'minedocscan.db').as_posix()}")
        con.close()
    elif name in ("excel_first", "excel_unchanged"):
        from minedocscan.export.auto import AutoExport

        xdir = out / f"excel-{label}"
        if name == "excel_first" and xdir.exists():
            shutil.rmtree(xdir)
        xdir.mkdir(exist_ok=True)
        con = open_db(f"sqlite:///{(out / 'work' / 'minedocscan.db').as_posix()}")
        clock = Clock()
        x = AutoExport(_settings(out, excel_dir=xdir), site, clock=clock)
        res["rounds_s"] = [round(v, 3) for v in _cycle(x, con)]
        res["files"] = len(list(xdir.rglob("*.xlsx")))
    elif name == "excel_meter":
        from minedocscan.export.auto import AutoExport

        tmp = out / f"tmp-{label}"
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir()
        shutil.copy(out / "work" / "minedocscan.db", tmp / "minedocscan.db")
        shutil.copytree(out / f"excel-{label}", tmp / "excel")
        st = _settings(out, work_root=tmp, excel_dir=tmp / "excel", reviews=tmp / "reviews.jsonl")
        con = open_db(st.resolved_db_url)
        clock = Clock()
        x = AutoExport(st, site, clock=clock)
        x.last_sweep = clock.t                     # 전체 훑기는 이미 했다 — 이 바퀴는 더러운 범위만
        t = _meter_review(con, site, st)
        res["dirty_dates"] = len(t.all_dates(con))
        t0 = time.perf_counter()
        r = x.after_round(con, t)
        res["round_s"] = round(time.perf_counter() - t0, 3)
        res["written"] = len(r.written) if r is not None else 0
    elif name == "export_cmd":
        from minedocscan.export.writer import export_excel

        xdir = out / f"excel-cmd-{label}"
        shutil.rmtree(xdir, ignore_errors=True)
        xdir.mkdir()
        con = open_db(f"sqlite:///{(out / 'work' / 'minedocscan.db').as_posix()}")
        t0 = time.perf_counter()
        r = export_excel(con, site, xdir, full=True)
        res["s"], res["files"] = round(time.perf_counter() - t0, 2), len(r.written)
        shutil.rmtree(xdir)
    elif name in ("publish_first", "publish_nochange"):
        from minedocscan.publish import core

        if name == "publish_first":
            import psycopg

            with psycopg.connect(pg, autocommit=True) as c:
                c.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        con = open_db(f"sqlite:///{(out / 'work' / 'minedocscan.db').as_posix()}")
        t0 = time.perf_counter()
        r = core.run(con, _settings(out, publish_url=pg, publish_schema=schema), full=True)
        res["s"], res["rows"], res["replaced"] = round(time.perf_counter() - t0, 2), r.rows, sum(r.replaced.values())
    elif name in ("publish_auto_unchanged", "publish_meter"):
        from minedocscan.publish.auto import AutoPublish

        st = _settings(out, publish_url=pg, publish_schema=schema, publish_retry_seconds=0.0)
        con = open_db(st.resolved_db_url)
        clock = Clock()
        p = AutoPublish(st, clock=clock)
        if name == "publish_auto_unchanged":
            res["rounds_s"] = [round(v, 3) for v in _cycle(p, con)]
        else:
            tmp = out / f"tmp-pub-{label}"
            shutil.rmtree(tmp, ignore_errors=True)
            tmp.mkdir()
            shutil.copy(out / "work" / "minedocscan.db", tmp / "minedocscan.db")
            st2 = _settings(out, work_root=tmp, reviews=tmp / "reviews.jsonl", publish_url=pg, publish_schema=schema)
            con = open_db(st2.resolved_db_url)
            p = AutoPublish(st2, clock=clock)
            p.last_sweep = clock.t
            t = _meter_review(con, site, st2)
            res["dirty_dates"] = len(t.all_dates(con))
            t0 = time.perf_counter()
            r = p.after_round(con, t)
            res["round_s"] = round(time.perf_counter() - t0, 3)
            res["replaced"] = sum(r.replaced.values()) if hasattr(r, "replaced") else None
    else:
        raise SystemExit(f"모르는 일: {name}")
    res["rss_mb"] = round(_rss_mb(), 1)
    return res


def _pg_version(pg: str) -> str:
    """대상 서버의 판 (성적서에는 판만 — 호스트·DB 이름은 적지 않는다)."""
    import psycopg

    with psycopg.connect(pg) as c:
        return c.execute("SHOW server_version").fetchone()[0].split()[0]


def measure(out: Path, pg: str | None, label: str, path: Path) -> dict:
    names = ["baseline", "excel_first", "excel_unchanged", "excel_meter", "export_cmd"]
    if pg:
        names += ["publish_first", "publish_nochange", "publish_auto_unchanged", "publish_meter"]
    result = {"label": label, "db": json.loads((out / "bigdb.json").read_text(encoding="utf-8")),
              "postgres": _pg_version(pg) if pg else None, "jobs": {}}
    for n in names:
        p = subprocess.run([sys.executable, __file__, "_job", n, str(out), "--label", label], env=dict(os.environ, BIGDB_PG=pg or ""),
                           capture_output=True, text=True)
        if p.returncode != 0:
            raise SystemExit(f"{n} 이 실패했다:\n{p.stderr[-3000:]}")
        result["jobs"][n] = json.loads(p.stdout.strip().splitlines()[-1])
        print(n, json.dumps(result["jobs"][n])[:300], flush=True)
    base = result["jobs"]["baseline"]["rss_mb"]
    for v in result["jobs"].values():
        v["rss_over_baseline_mb"] = round(v["rss_mb"] - base, 1)
    if pg:
        import psycopg

        with psycopg.connect(pg, autocommit=True) as c:
            c.execute(f'DROP SCHEMA IF EXISTS "scale_{label}" CASCADE')
    path.write_text(json.dumps(result, indent=1, ensure_ascii=False), encoding="utf-8")
    return result


def report(before: dict, after: dict, path: Path) -> dict:
    """두 측정을 기준(tasks/0009 4.2 마)과 같이 — 바뀐 것 없는 조각 하나가 엑셀·싣기 각각 5초 안, 최대 메모리 300 MB 안(기준선을 뺀 것),
    계기 칸 하나 → 더러운 날짜 3일 안, 명령 publish 의 시간은 고치기 전의 1.5배 안. 처음 훑기의 조각·명령 export excel 은 보고만."""
    def jobs(m, n):
        return m["jobs"].get(n, {})

    a, b = after, before

    def get(m, n, key, agg=None):
        v = jobs(m, n).get(key)
        return None if v is None else (agg(v) if agg else v)

    def ratio(n):
        x, y = get(a, n, "s"), get(b, n, "s")
        return None if x is None or not y else round(x / y, 2)

    crit = {
        "excel_unchanged_slice_max_s": (get(a, "excel_unchanged", "rounds_s", max), 5.0),
        "publish_unchanged_slice_max_s": (get(a, "publish_auto_unchanged", "rounds_s", max), 5.0),
        "excel_unchanged_rss_mb": (get(a, "excel_unchanged", "rss_over_baseline_mb"), 300.0),
        "publish_auto_unchanged_rss_mb": (get(a, "publish_auto_unchanged", "rss_over_baseline_mb"), 300.0),
        "publish_first_rss_mb": (get(a, "publish_first", "rss_over_baseline_mb"), 300.0),
        "publish_nochange_rss_mb": (get(a, "publish_nochange", "rss_over_baseline_mb"), 300.0),
        "meter_review_dirty_dates": (get(a, "excel_meter", "dirty_dates"), 3),
        "publish_first_vs_before": (ratio("publish_first"), 1.5),
        "publish_nochange_vs_before": (ratio("publish_nochange"), 1.5),
    }
    out = {"criteria": {k: {"value": v, "limit": lim, "ok": v is not None and v <= lim, **({"not_measured": True} if v is None else {})}
                        for k, (v, lim) in crit.items()},
           "db": a["db"], "postgres": a.get("postgres"), "before": b, "after": a,
           "report_only": {"excel_first_slices_s": jobs(a, "excel_first")["rounds_s"],
                           "export_cmd_s": {"before": jobs(b, "export_cmd")["s"], "after": jobs(a, "export_cmd")["s"]}},
           "machine": {"cpus": os.cpu_count(), "python": sys.version.split()[0]}}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="bigdb", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("make")
    m.add_argument("out")
    m.add_argument("--days", type=int, default=252)
    m.add_argument("--rows-per-day", type=int, default=REAL_ROWS_PER_DAY)
    m.add_argument("--source-days", type=int, default=5)
    m.add_argument("--seed", type=int, default=0)
    me = sub.add_parser("measure")
    me.add_argument("out")
    me.add_argument("--label", required=True)
    me.add_argument("--json", required=True)
    j = sub.add_parser("_job")
    j.add_argument("name")
    j.add_argument("out")
    j.add_argument("--label", required=True)
    r = sub.add_parser("report")
    r.add_argument("before")
    r.add_argument("after")
    r.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    if a.cmd == "make":
        print(json.dumps(make(Path(a.out), a.days, a.rows_per_day, a.source_days, a.seed), indent=1))
    elif a.cmd == "measure":
        measure(Path(a.out), os.environ.get("MINEDOCSCAN_TEST_PG_URL") or None, a.label, Path(a.json))
    elif a.cmd == "_job":
        print(json.dumps(job(a.name, Path(a.out), os.environ.get("BIGDB_PG") or None, a.label)))
    else:
        out = report(json.loads(Path(a.before).read_text()), json.loads(Path(a.after).read_text()), Path(a.out))
        print(json.dumps(out["criteria"], indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

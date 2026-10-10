"""시험 성적서 (tasks/0009 4.9) — CI 의 산출물을 모아 `시험성적서-<판>.md` 를 만든다. 설치되는 패키지에 들어가지 않는다 (scripts/).

    python scripts/test_report.py env --out env.json              # 이 작업의 OS·파이썬·의존성 (각 시험 작업이 junit 옆에 둔다)
    python scripts/test_report.py build --artifacts DIR --out DIR [--needs JSON] [--version V]

artifacts 폴더 (actions/download-artifact 가 산출물마다 하위 폴더로 받는다 — 그 안의 자리는 찾아서 읽는다):
  junit-<작업>/  junit.xml, env.json             자동 시험 (pytest --junitxml) 과 그 작업의 환경 (test-3.11·test-3.12·lowest·slow·postgres·windows)
  junit-test-3.12/  selftest/selftest.json, licenses.txt   우분투의 자가 시험, 설치한 환경의 라이선스 목록
  windows-install-report/  bundle-report.json, licenses.txt, selftest/selftest.json, install-steps.json   설치 시험 (윈도우)
  v2-metrics/v2-metrics.json                      확장성 표 (scripts/v2_metrics.py — slow 작업)
규모는 저장소의 docs/test-report/scale-*.json (가장 새 것). 작업마다의 결과는 --needs 또는 환경 변수 NEEDS (report 작업의 `toJSON(needs)`).

성적서에는 수만 — 경로·사용자 이름·호스트 이름을 싣지 않는다 (실패한 시험의 글도 싣지 않는다 — 이름과 수만). 8절(실데이터)은 칸과 명령만이고 사람이 채운다.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JOBS = [("test-3.11", "우분투 · 파이썬 3.11"), ("test-3.12", "우분투 · 파이썬 3.12"), ("lowest", "우분투 · 하한 판"),
        ("slow", "우분투 · slow"), ("postgres", "우분투 · postgres"), ("windows", "윈도우 · 파이썬 3.12")]
NEEDS_JOB = {"test-3.11": "test", "test-3.12": "test", "lowest": "lowest", "slow": "slow", "postgres": "postgres",
             "windows": "windows", "windows-install": "windows-install"}
DEPS = ("numpy", "cv2", "pypdfium2", "openpyxl", "psycopg")
INSTALL_STEPS = [("bundle", "묶음 만들기 (내려받은 것의 해시, 라이선스 검사)"), ("demo", "합성 사이트 팩과 데이터 폴더"),
                 ("install", "설치 (망을 막고)"), ("fresh", "새 창처럼 — PATH·설정, 판, 자가 시험"),
                 ("serve", "작업 스케줄러의 serve — /api/home, 로그 UTF-8"), ("upgrade", "같은 묶음으로 다시 설치 (설정 그대로)"),
                 ("tampered", "손상된 묶음은 멈춘다"), ("uninstall", "지우기 — 프로그램만")]
REALDATA = [
    ("회귀", "`regress` 의 기준과 같음/다름 (다른 수치의 수)", "minedocscan regress"),
    ("정합·분류", "쪽 수, 분류된 쪽, 정합 통과율, 괘선 오차의 중앙", "minedocscan report --json"),
    ("값 유무", "정밀도·재현율 (검수한 칸에서)", "minedocscan eval --answers answers.json --target raw --only-listed --split test"),
    ("숫자 인식기", "CER, 자동 적재율, 자동 적재 오류율(상한)", "minedocscan recognizer eval --crops … --model … --split test"),
    ("쪽 메타", "키마다 정확도·자동 적재 오류율", "minedocscan eval --meta --split test"),
    ("✓ 판정", "정확도(구간), 판정 불가, column_unused", "minedocscan eval --checks --split test"),
    ("처리 시간", "하루치(쪽 수)의 run 시간, watch 한 바퀴", "minedocscan run --fresh … (시간을 잰다)"),
]
_PATHS = re.compile(r"(?:[A-Za-z]:\\|\\\\|/(?:home|Users|tmp|var|runner|github|opt|root|private)/)[^\s'\"|)]*")


def scrub(text: str) -> str:
    """절대 경로를 지운다 (성적서에는 수만)."""
    return _PATHS.sub("<경로>", str(text))


# ── env ───────────────────────────────────────────────────────────────────
def cmd_env(a) -> int:
    sys.path.insert(0, str(ROOT / "src"))
    from minedocscan.selftest import environment

    a.out.parent.mkdir(parents=True, exist_ok=True)                    # pytest 가 돌지 못해 junit/ 이 없을 때도
    a.out.write_text(json.dumps(environment(), ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return 0


# ── 읽기 ──────────────────────────────────────────────────────────────────
def _load(p: Path) -> dict | None:
    try:
        return json.loads(p.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None


def junit(path: Path) -> dict | None:
    """pytest --junitxml → 시험 수·통과·실패·오류·건너뜀·시간, 건너뛴 이유별 수."""
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError):
        return None
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    n = {k: sum(int(s.get(k, 0) or 0) for s in suites) for k in ("tests", "failures", "errors", "skipped")}
    n["time"] = round(sum(float(s.get("time", 0) or 0) for s in suites), 1)
    n["passed"] = n["tests"] - n["failures"] - n["errors"] - n["skipped"]
    reasons = Counter()
    failed = []
    for case in root.iter("testcase"):
        sk = case.find("skipped")
        if sk is not None:
            msg = (sk.get("message") or "").removeprefix("Skipped: ")
            reasons[scrub(msg)[:120] or "(이유 없음)"] += 1
        if case.find("failure") is not None or case.find("error") is not None:
            failed.append(f"{case.get('classname', '')}::{case.get('name', '')}")
    n["skip_reasons"] = dict(reasons.most_common())
    n["failed"] = failed
    return n


def find(art: Path, folder: str, name: str) -> Path:
    """산출물 폴더 안의 파일 (올린 자리에 따라 한 겹 아래일 수 있다). 없으면 있지 않은 경로."""
    hits = sorted((art / folder).rglob(name)) if (art / folder).is_dir() else []
    return hits[0] if hits else art / folder / name


def latest(pattern: str, where: Path) -> Path | None:
    files = sorted(where.glob(pattern))
    return files[-1] if files else None


def licenses(path: Path) -> dict | None:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    by, bad, n = Counter(), [], 0
    for line in lines:
        m = re.match(r"(ok |NO )\s*(.*?) — (.*)$", line)               # 이름·판에는 빈칸이 있을 수 있다 (python (embeddable), 판 없음)
        if m:
            n += 1
            by[m.group(3).strip()] += 1
            if m.group(1) != "ok ":
                bad.append(" ".join(m.group(2).split()))
    return {"distributions": n, "by_license": dict(by.most_common()), "not_allowed": bad}


# ── 쓰기 ──────────────────────────────────────────────────────────────────
def _table(head: list[str], rows: list[list]) -> list[str]:
    cell = lambda v: "" if v is None else str(v).replace("|", "\\|").replace("\n", " ")   # noqa: E731
    return ["| " + " | ".join(head) + " |", "|" + "---|" * len(head), *("| " + " | ".join(cell(v) for v in r) + " |" for r in rows), ""]


def _result(needs: dict, job: str, art: Path | None = None) -> str:
    """작업의 결과 — 그 작업이 남긴 job-result.txt (행렬의 갈래마다), 없으면 needs."""
    r = None
    if art is not None:
        try:
            r = find(art, f"junit-{job}", "job-result.txt").read_text(encoding="utf-8-sig").strip() or None
        except OSError:
            r = None
    r = r or (needs.get(NEEDS_JOB.get(job, job)) or {}).get("result")
    return {"success": "통과", "failure": "실패", "cancelled": "취소", "skipped": "건너뜀"}.get(r, r or "?")


def build(art: Path, needs: dict, version: str, scale_dir: Path) -> str:
    out = [f"# 시험 성적서 — minedocscan {version}", "",
           "CI 의 산출물에서 `scripts/test_report.py` 가 만들었다 (tasks/0009 4.9). 합성 데이터만 — 실데이터 수치는 8절에 사람이 넣는다 (수만).",
           "합성 데이터의 수치로 인식률을 말하지 않는다 (CLAUDE.md).", ""]
    jobs = {job: (junit(find(art, f"junit-{job}", "junit.xml")), _load(find(art, f"junit-{job}", "env.json"))) for job, _l in JOBS}

    out += ["## 1. 시험 환경", ""]
    rows = []
    for job, label in JOBS:
        env = jobs[job][1]
        if not env:
            rows.append([label, "산출물 없음", "", ""])
            continue
        deps = env.get("dependencies", {})
        rows.append([label, scrub(env.get("os", "")), env.get("python", ""),
                     ", ".join(f"{d} {deps[d]}" for d in DEPS if d in deps) or ", ".join(f"{k} {v}" for k, v in deps.items())])
    out += _table(["작업", "OS", "파이썬", "의존성"], rows)

    out += ["## 2. 자동 시험", "", "`pytest --junitxml` — 작업마다. 시간은 pytest 가 잰 것(초).", ""]
    rows = []
    for job, label in JOBS:
        j = jobs[job][0]
        if j is None:
            rows.append([label, _result(needs, job, art), "", "", "", "", "산출물 없음"])
            continue
        rows.append([label, _result(needs, job, art), j["tests"], j["passed"], j["failures"] + j["errors"], j["skipped"], j["time"]])
    out += _table(["작업", "결과", "시험", "통과", "실패·오류", "건너뜀", "시간(초)"], rows)
    for job, label in JOBS:
        j = jobs[job][0]
        if j and j["failed"]:
            out += [f"{label} 의 실패: " + ", ".join(f"`{scrub(f)}`" for f in j["failed"][:30]), ""]
    skipped = [(label, r, c) for job, label in JOBS if jobs[job][0] for r, c in jobs[job][0]["skip_reasons"].items()]
    if skipped:
        out += ["건너뛴 이유 (작업마다):", ""] + _table(["작업", "이유", "수"], [list(x) for x in skipped])

    out += ["## 3. 자가 시험", "", "`minedocscan selftest` — 우분투는 test (3.12) 작업, 윈도우는 설치한 묶음으로 (windows-install).", ""]
    for label, p in (("우분투", find(art, "junit-test-3.12", "selftest.json")),
                     ("윈도우 (설치한 묶음)", find(art, "windows-install-report", "selftest.json"))):
        r = _load(p)
        if not r:
            out += [f"**{label}**: 산출물 없음", ""]
            continue
        env = r.get("environment", {})
        out += [f"**{label}** — {'통과' if r.get('passed') else '실패'}, {r.get('seconds')}초 ({scrub(env.get('os', ''))}, 파이썬 "
                f"{env.get('python', '')}, CPU {env.get('cpu_count', '?')}, 메모리 {env.get('memory_gb', '?')} GB)", ""]
        out += _table(["검사", "무엇을", "결과", "시간(초)", "비고"],
                      [[c["name"], c["title"], {"passed": "통과", "failed": "실패", "skipped": "건너뜀"}.get(c["status"], c["status"]),
                        c["seconds"], scrub(c.get("reason", ""))] for c in r.get("checks", [])])

    out += ["## 4. 설치 시험", "", f"windows-install 작업 (Windows PowerShell 5.1, 망을 막고) — 작업 결과: {_result(needs, 'windows-install')}. "
            "단계마다 결과.", ""]
    steps = _load(find(art, "windows-install-report", "install-steps.json")) or {}
    out += _table(["단계", "결과", "수치"],
                  [[label, {"success": "통과", "failure": "실패", "skipped": "건너뜀", "cancelled": "취소"}.get(
                      (steps.get(sid) or {}).get("outcome"), "산출물 없음" if not steps else "기록 없음"),
                    ", ".join(f"{k} {v}" for k, v in sorted(((steps.get(sid) or {}).get("outputs") or {}).items()))]
                   for sid, label in INSTALL_STEPS])
    rep = _load(find(art, "windows-install-report", "bundle-report.json"))
    if rep:
        dll = rep.get("dll", {})
        out += [f"묶음: {rep.get('zip_bytes', 0) / 2**20:.1f} MB, 바퀴 {rep.get('wheels')}개, 앱 전용 파이썬 {rep.get('python', '?')}. "
                f"PC 에 있어야 하는 VC 런타임: {', '.join(dll.get('needed_from_system') or []) or '없음'} "
                f"(msvcp140.dll {'필요' if dll.get('msvcp140_needed_from_system') else '필요 없음'}).", ""]

    out += ["## 5. 규모", ""]
    sp = latest("scale-*.json", scale_dir)
    sc = _load(sp) if sp else None
    if sc:
        db = sc.get("db", {})
        out += [f"`{sp.name}` — 복제 DB {db.get('days')}일, 쪽 {db.get('pages'):,}, 행 {db.get('rows'):,} (하루 약 {db.get('rows_per_day'):,}행), "
                f"CPU {sc.get('machine', {}).get('cpus')}, 파이썬 {sc.get('machine', {}).get('python')}, PostgreSQL {sc.get('postgres')}.", ""]
        out += _table(["기준", "값", "한도", "충족"], [[k, v.get("value"), v.get("limit"), "예" if v.get("ok") else "아니오"]
                                                    for k, v in sc.get("criteria", {}).items()])
    else:
        out += ["(docs/test-report/scale-*.json 이 없다)", ""]

    out += ["## 6. 확장성 (가상 양식 V2)", "",
            "`synth --v2-forms` (보통 글씨)와 `--rough` (거친 글씨) — 양식마다 쪽 수, 분류, 정합 통과, 괘선 오차의 중앙, 칸 수, 인식기에 보낸 칸의 "
            "`oracle` CER, 값 유무의 정밀도·재현율(`null` — 정답의 잉크 유무와). 템플릿만으로 새 양식이 들어가는지의 증거이지 인식률이 아니다.", ""]
    v2 = _load(find(art, "v2-metrics", "v2-metrics.json"))
    if v2:
        rows = []
        for kind, label in (("normal", "보통"), ("rough", "거친")):
            for form, m in sorted((v2.get(kind) or {}).items()):
                rows.append([form, label, m.get("pages"), m.get("classified"), m.get("loaded"), m.get("grid_err_median"), m.get("cells"),
                             m.get("sent_cells"), m.get("oracle_cer"), m.get("presence_precision"), m.get("presence_recall")])
        out += [f"합성 {v2.get('days')}일, seed {v2.get('seed')}.", ""]
        out += _table(["양식", "글씨", "쪽", "분류", "적재", "괘선 오차 중앙(px)", "칸", "보낸 칸", "oracle CER", "정밀도", "재현율"], rows)
    else:
        out += ["산출물 없음 (slow 작업의 v2-metrics)", ""]

    out += ["## 7. 라이선스", "", "`scripts/licenses.py --check` — 허용 목록 밖·알 수 없는 배포판이 있으면 실패한다.", ""]
    for where, folder in (("윈도우 설치 묶음 (wheels/ 와 앱 전용 파이썬)", "windows-install-report"),
                          ("우분투 — 설치한 환경의 닫힘 ([postgres])", "junit-test-3.12")):
        lic = licenses(find(art, folder, "licenses.txt"))
        if not lic:
            out += [f"**{where}** — 산출물 없음", ""]
            continue
        out += [f"**{where}** — 배포판 {lic['distributions']}개, 허용 목록 밖 {len(lic['not_allowed'])}개"
                + (f" ({', '.join(lic['not_allowed'])})" if lic["not_allowed"] else ""), ""]
        out += _table(["라이선스", "배포판 수"], [[k, v] for k, v in lic["by_license"].items()])

    out += ["## 8. 실데이터", "", "**사람이 채운다** — 수만 (이름·차량번호·파일명 없이). 실데이터가 있는 PC 에서 칸마다의 명령으로 (docs/DATA.md).", ""]
    out += _table(["항목", "무엇을", "값", "명령"], [[a, b, "", f"`{c}`"] for a, b, c in REALDATA])
    return "\n".join(out).rstrip() + "\n"


def cmd_build(a) -> int:
    raw = a.needs or os.environ.get("NEEDS") or ""
    needs = json.loads(raw) if raw.strip() else {}
    version = a.version
    if not version:
        sys.path.insert(0, str(ROOT / "src"))
        from minedocscan import __version__ as version
    text = build(a.artifacts, needs, version, a.scale_dir)
    a.out.mkdir(parents=True, exist_ok=True)
    p = a.out / f"시험성적서-{version}.md"
    p.write_text(text, encoding="utf-8")
    print(f"{p} ({len(text.splitlines())}줄)")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("env", help="이 작업의 OS·파이썬·의존성을 JSON 으로")
    e.add_argument("--out", type=Path, required=True)
    b = sub.add_parser("build", help="산출물 → 시험성적서-<판>.md")
    b.add_argument("--artifacts", type=Path, required=True)
    b.add_argument("--out", type=Path, required=True)
    b.add_argument("--needs", help="report 작업의 toJSON(needs) — 작업마다 결과")
    b.add_argument("--version", help="판 (기본: 이 저장소의 minedocscan)")
    b.add_argument("--scale-dir", type=Path, default=ROOT / "docs" / "test-report")
    a = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and (stream.encoding or "").lower().replace("-", "") != "utf8":
            stream.reconfigure(encoding="utf-8", errors="replace")
    return cmd_env(a) if a.cmd == "env" else cmd_build(a)


if __name__ == "__main__":
    sys.exit(main())

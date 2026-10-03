"""명령줄 진입점: `minedocscan <명령>`.

  info       설정·사이트 팩·등록된 백엔드 확인
  run        스캔 파일/폴더 → 분류 → 정합 → 추출 → 인식 → 검증 → 적재 → 교차검증
  report     DB 현황 (양식별 페이지, 정합 품질, 검수 대기, 교차검증). --by-month 는 양식 × 월 진단
  pages      쪽 목록 (상태·양식·분류 여유로 거름). --thumbs 는 원본 쪽의 미리보기 PNG
  eval       정답과 비교 (CER, 필드 정확도, 자동 적재율)
  regress    사이트 팩의 기준 수치와 비교하는 실데이터 회귀 검사
  template   새 양식의 템플릿 뼈대 만들기
  synth      개인정보 없는 합성 사이트 팩과 스캔 문서 만들기
  review     검수: serve(로컬 화면), stats(진행 현황), export-answers(검수값 → 정답 파일)

경로는 --site / --archive-root / --work-root 또는 환경변수·설정 파일로 준다 (config.py).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from . import __version__
from .config import Settings, load_settings


def _common() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--config", help="설정 파일 (기본: ./minedocscan.toml 또는 MINEDOCSCAN_CONFIG)")
    p.add_argument("--site", help="사이트 팩 폴더")
    p.add_argument("--archive-root", help="스캔 원본 폴더 (읽기 전용으로 취급)")
    p.add_argument("--work-root", help="작업 폴더 (DB·정합 이미지·리포트)")
    p.add_argument("--db-url", help="DB 주소 (기본: sqlite:///<work-root>/minedocscan.db)")
    p.add_argument("--json", action="store_true", help="결과를 JSON 으로 출력")
    return p


def build_parser() -> argparse.ArgumentParser:
    common = _common()
    ap = argparse.ArgumentParser(prog="minedocscan", description="광산 현장 수기 문서 스캔 → 데이터베이스 파이프라인")
    ap.add_argument("--version", action="version", version=f"minedocscan {__version__}")
    sub = ap.add_subparsers(dest="command", required=True)

    sub.add_parser("info", parents=[common], help="설정·사이트 팩·백엔드 확인")

    p = sub.add_parser("run", parents=[common], help="스캔 파일/폴더를 처리해 DB 에 적재")
    p.add_argument("paths", nargs="*", help="파일 또는 폴더. 없으면 archive_root 전체. 상대경로는 archive_root 기준으로도 찾는다")
    p.add_argument("--recognizer", help="인식 백엔드 (기본: 설정값)")
    p.add_argument("--corrector", help="교정 백엔드 (기본: 설정값)")
    p.add_argument("--template", help="양식 분류를 건너뛰고 이 템플릿으로 처리")
    p.add_argument("--answers", help="oracle 백엔드용 정답 JSON")
    p.add_argument("--inspection-csv", metavar="DIR", help="oracle 백엔드용 점검표 정답 CSV 폴더")
    p.add_argument("--fresh", action="store_true", help="기존 SQLite 파일을 지우고 시작")
    p.add_argument("--skip-existing", action="store_true",
                   help="이미 끝까지 처리된 파일(같은 해시)은 건너뛴다. failed 는 다시 한다. "
                        "템플릿이나 인식기를 바꾼 뒤에는 쓰지 않는다 — 그때는 --fresh")
    p.add_argument("--strict", action="store_true", help="첫 오류에서 멈춘다 (디버깅용). 기본은 실패를 기록하고 계속")

    p = sub.add_parser("report", parents=[common], help="DB 현황")
    p.add_argument("--by-month", action="store_true", help="양식 × 월: 쪽 수, 적재, 정합 실패, 괘선 오차, 분류 여유")

    p = sub.add_parser("pages", parents=[common], help="쪽 목록")
    p.add_argument("--status", help="unknown_form | classified_only | align_failed | loaded | error")
    p.add_argument("--template", help="이 양식만")
    p.add_argument("--low-margin", action="store_true", help="분류 여유가 classify_min_margin 아래인 쪽만")
    p.add_argument("--thumbs", nargs="?", const="", metavar="DIR",
                   help="목록의 쪽을 1/4 로 줄인 PNG 로 쓴다 (기본 WORK_ROOT/thumbs/<상태>/). 저장소 밖에만")

    p = sub.add_parser("eval", parents=[common], help="정답과 비교")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--answers", help="정답 JSON (synth 가 만드는 answers.json 형식)")
    g.add_argument("--inspection-csv", metavar="DIR", help="점검표 정답 CSV 폴더 (YYMMDD.csv)")
    p.add_argument("--template", help="--inspection-csv 가 가리키는 템플릿 (기본: 핸들러가 inspection 인 유일한 템플릿)")
    p.add_argument("--target", choices=["final", "raw"], default="final",
                   help="final = 최종값(교정·검수 후), raw = 기계가 읽은 값. 검수값과 비교할 때는 raw")
    p.add_argument("--only-listed", action="store_true",
                   help="정답에 있는 셀만 평가 (표본 검수로 만든 정답). 기본은 정답이 있는 표의 셀 전부(없는 셀은 빈 칸)")
    p.add_argument("--split", choices=["all", "test", "train"], default="all",
                   help="날짜 분할 ([eval] split_salt). test 는 학습·사전·임계값 조정에 쓰지 않는다")

    p = sub.add_parser("regress", parents=[common], help="사이트 팩 기준 수치와 비교 (실데이터 회귀)")
    p.add_argument("--update", action="store_true", help="지금 결과를 새 기준으로 저장")
    p.add_argument("--inputs", nargs="+", help="처음 기준을 만들 때: archive_root 기준 상대경로들")

    p = sub.add_parser("template", parents=[common], help="템플릿 도구")
    tsub = p.add_subparsers(dest="template_command", required=True)
    t = tsub.add_parser("init", parents=[common], help="기준 이미지에서 괘선을 검출해 템플릿 뼈대를 만든다")
    t.add_argument("image", help="빈 양식 또는 깨끗한 스캔 (이미지·PDF)")
    t.add_argument("--name", required=True, help="템플릿 이름 (영문 소문자·밑줄)")
    t.add_argument("--roi", help="표 영역 x0,y0,x1,y1 (200 dpi 픽셀). 없으면 페이지 전체")
    t.add_argument("--header-rows", type=int, default=1)
    t.add_argument("--page", type=int, default=1)
    t.add_argument("--handler", default="generic")
    t.add_argument("--overwrite", action="store_true")

    p = sub.add_parser("synth", parents=[common], help="합성 사이트 팩 + 스캔 문서 + 정답 생성")
    p.add_argument("out", help="출력 폴더 (site/, scans/, truth.json, answers.json)")
    p.add_argument("--days", type=int, default=3)
    p.add_argument("--seed", type=int, default=0)

    p = sub.add_parser("review", parents=[common], help="검수 도구")
    rsub = p.add_subparsers(dest="review_command", required=True)
    r = rsub.add_parser("serve", parents=[common], help="로컬 검수 화면 (127.0.0.1)")
    r.add_argument("--queue", default="haul-numbers", choices=["haul-numbers", "mismatch", "pending", "page-fields"])
    r.add_argument("--n", type=int, default=1500, help="haul-numbers 표본 크기 (기본 1500)")
    r.add_argument("--seed", type=int, default=0, help="표본의 순서를 정하는 씨앗. 같은 값이면 같은 표본")
    r.add_argument("--empty-share", type=float, default=0.1, help="표본 중 빈 칸 비율 (기본 0.1)")
    r.add_argument("--template", help="pending: 이 템플릿만")
    r.add_argument("--kind", choices=["handwritten_number", "handwritten_text"], help="pending: 이 종류만")
    r.add_argument("--reviewer", help="검수자 식별자 (짧은 영문). 없으면 서버를 띄우지 않는다")
    r.add_argument("--port", type=int, default=8765)
    rsub.add_parser("stats", parents=[common], help="검수 진행 현황")
    r = rsub.add_parser("export-answers", parents=[common], help="유효한 검수(value, empty) → answers.json")
    r.add_argument("out", help="출력 파일 (eval --answers 로 읽는 형식)")
    r.add_argument("--split", choices=["all", "test", "train"], default="all", help="날짜 분할")
    r = rsub.add_parser("export-crops", parents=[common], help="검수한 셀의 이미지 + 라벨 (인식기 학습·평가용)")
    r.add_argument("out", help="출력 폴더. git 작업 트리 안이면 거절한다")
    r.add_argument("--split", choices=["all", "test", "train"], default="all")
    r.add_argument("--kind", choices=["handwritten_number", "handwritten_text"], help="이 종류의 셀만")
    r.add_argument("--res", choices=["auto", "source", "aligned"], default="auto",
                   help="source = 원본 해상도(호모그래피로 다시 정합), aligned = 200 dpi 정합 이미지, auto = 원본이 닿으면 원본")
    r.add_argument("--scale", type=float, default=1.5,
                   help="템플릿 좌표(200 dpi) 대비 배율, 0 초과 6 이하 (기본 1.5 = 300 dpi 원본 그대로)")
    r.add_argument("--pad", type=int, help="셀 둘레 여유(템플릿 px, 0 이상). 기본은 화면과 같이 행 높이의 절반(최소 8)")
    r.add_argument("--allow-in-repo", action="store_true", help="git 작업 트리 안에도 쓴다 (글씨가 들어 있다 — 커밋하지 말 것)")
    return ap


def _settings(a: argparse.Namespace, **extra) -> Settings:
    return load_settings(a.config, site=a.site, archive_root=a.archive_root, work_root=a.work_root,
                         db_url=a.db_url, **extra)


def _emit(a: argparse.Namespace, data: dict, text: str) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=1) if a.json else text)


def _need_site(s: Settings):
    from .forms.sitepack import SitePack

    if s.site is None:
        raise SystemExit("사이트 팩이 지정되지 않았습니다: --site 또는 MINEDOCSCAN_SITE")
    return SitePack(s.site)


# ── 명령 ───────────────────────────────────────────────────────────────────
def cmd_info(a) -> int:
    from .correct import REGISTRY as CORRECTORS
    from .handlers import REGISTRY as HANDLERS
    from .recognize import REGISTRY as RECOGNIZERS

    s = _settings(a)
    data = {
        "version": __version__,
        "settings": {"site": str(s.site) if s.site else None,
                     "archive_root": str(s.archive_root) if s.archive_root else None,
                     "work_root": str(s.work_root), "db_url": s.resolved_db_url, "dpi": s.dpi,
                     "recognizer": s.recognizer, "corrector": s.corrector, "auto_accept_conf": s.auto_accept_conf},
        "backends": {"recognizers": sorted(RECOGNIZERS), "correctors": sorted(CORRECTORS), "handlers": sorted(HANDLERS)},
        "site": None,
    }
    lines = [f"minedocscan {__version__}"] + [f"  {k}: {v}" for k, v in data["settings"].items()]
    lines.append("백엔드: " + ", ".join(f"{k}={v}" for k, v in data["backends"].items()))
    if s.site and Path(s.site).is_dir():
        site = _need_site(s)
        tpls = [{"name": t.name, "title": t.title, "handler": t.handler, "regions": len(t.regions),
                 "cells": len(t.cells()) + len(t.fields), "status": "cells" if t.has_cells else "classify_only",
                 "family": t.family, "valid_from": t.valid_from, "valid_to": t.valid_to}
                for t in site.templates.values()]
        data["site"] = {"name": site.name, "templates": tpls, "labels": len(site.labels)}
        lines.append(f"사이트 팩: {site.name} — 템플릿 {len(tpls)}종, 페이지 라벨 {len(site.labels)}개")
        for t in tpls:
            valid = (f"  계열 {t['family']} {t['valid_from'] or '…'}~{t['valid_to'] or '…'}" if t["family"] else "")
            lines.append(f"  {t['name']:<24} handler={t['handler']:<11} 표 {t['regions']}개, 셀 {t['cells']}개"
                         + ("" if t["status"] == "cells" else "  (분류 전용 — 셀 정의 없음)") + valid)
    else:
        lines.append("사이트 팩: 지정되지 않았거나 폴더가 없습니다")
    _emit(a, data, "\n".join(lines))
    return 0


def cmd_run(a) -> int:
    from .pipeline import Pipeline
    from .recognize import OracleRecognizer, build_recognizer, load_answers_json
    from .report import build_report, format_report, xcheck_by_date

    s = _settings(a, recognizer=a.recognizer, corrector=a.corrector)
    site = _need_site(s)
    paths = [_resolve(p, s) for p in a.paths] or ([s.archive_root] if s.archive_root else [])
    if not paths:
        raise SystemExit("처리할 경로가 없습니다: PATH 를 주거나 archive_root 를 지정하세요")
    if a.fresh and s.resolved_db_url.startswith("sqlite:///"):
        Path(s.resolved_db_url[len("sqlite:///"):]).unlink(missing_ok=True)
    if a.answers or a.inspection_csv:
        answers = load_answers_json(a.answers) if a.answers else {}
        if a.inspection_csv:
            from .evaluate.inspection_csv import load_answers

            answers |= load_answers(a.inspection_csv, site.templates[_only_inspection_template(site)])
        recognizer = OracleRecognizer(answers)
    elif s.recognizer == "oracle":
        raise SystemExit("oracle 백엔드는 --answers 또는 --inspection-csv 가 필요합니다")
    else:
        try:
            recognizer = build_recognizer(s, site)
        except (KeyError, ValueError, FileNotFoundError) as e:
            raise SystemExit(f"인식 백엔드를 준비할 수 없습니다: {e}") from e
    pipe = Pipeline(s, site=site, recognizer=recognizer)
    files = pipe.expand(paths)
    if not files:
        raise SystemExit(f"처리할 파일이 없습니다: {[str(p) for p in paths]}")
    t0 = time.monotonic()
    for i, f in enumerate(files, 1):
        r = pipe.process_file(f, template=a.template, skip_existing=a.skip_existing, strict=a.strict)
        if not a.json:
            el = time.monotonic() - t0
            eta = el / i * (len(files) - i)
            what = ("건너뜀" if r.get("skipped") else f"실패: {r['error']}" if r["status"] == "failed" else f"{len(r['pages'])}쪽"
                    + (f" · 경고: {r['warning']}" if r.get("warning") else ""))
            print(f"[{i}/{len(files)}] {f.name} · {what} · 지난 {_hms(el)} · 남은 약 {_hms(eta)}", file=sys.stderr)
    summary = pipe.finalize()
    rep = build_report(pipe.con)
    text = (f"이번 실행: 문서 {summary['documents']}건, 페이지 {summary['pages']}장, 건너뜀 {summary['skipped']}건, "
            f"실패 {len(summary['failed'])}건 (인식 백엔드: {recognizer.name})\n"
            f"분류 여유가 낮은 페이지: {len(summary['low_margin'])}장, 오류 난 쪽: {len(summary['page_errors'])}장\n"
            f"── DB 현황 ({s.resolved_db_url}) ──\n" + format_report(rep, xcheck_by_date(pipe.con)))
    if summary["failed"]:
        text += "\n실패한 문서:\n" + "\n".join(f"  {d['source_name']}: {d['error']}" for d in summary["failed"])
    if summary["page_errors"]:
        text += "\n오류 난 쪽:\n" + "\n".join(f"  {d['page_id']}: {d['error']}" for d in summary["page_errors"])
    if summary["warnings"]:
        text += "\n경고가 있는 문서:\n" + "\n".join(f"  {d['source_name']}: {d['warning']}" for d in summary["warnings"])
    _emit(a, {"run": summary, "report": rep}, text)
    return 1 if (summary["failed"] or summary["page_errors"]) else 0


def _hms(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}" if sec >= 3600 else f"{sec // 60}:{sec % 60:02d}"


def cmd_report(a) -> int:
    from .report import build_report, by_month, format_by_month, format_report, xcheck_by_date
    from .store.db import open_db

    s = _settings(a)
    con = open_db(s.resolved_db_url)
    if a.by_month:
        rows = by_month(con, s.classify_min_margin)
        _emit(a, {"by_month": rows}, format_by_month(rows))
        return 0
    rep, by_date = build_report(con), xcheck_by_date(con)
    _emit(a, {"report": rep, "xcheck_by_date": by_date}, format_report(rep, by_date))
    return 0


def cmd_pages(a) -> int:
    from .report import format_pages, list_pages
    from .store.db import open_db

    s = _settings(a)
    con = open_db(s.resolved_db_url)
    rows = list_pages(con, status=a.status, template=a.template,
                      low_margin=s.classify_min_margin if a.low_margin else None)
    written = []
    if a.thumbs is not None:
        from .tools.thumbs import write_thumbs

        written = write_thumbs(s, rows, a.thumbs or None)
    text = format_pages(rows) + f"\n쪽 {len(rows)}개"
    if a.thumbs is not None:
        text += f", 미리보기 {len(written)}개 → {(a.thumbs or (s.work_root / 'thumbs'))}"
    _emit(a, {"pages": rows, "thumbs": [str(p) for p in written]}, text)
    return 0


def cmd_eval(a) -> int:
    from .evaluate.fields import evaluate_fields, evaluate_presence
    from .evaluate.inspection_csv import evaluate_inspection, load_answers
    from .recognize import load_answers_json
    from .store.db import open_db

    s = _settings(a)
    con = open_db(s.resolved_db_url)
    kw = {"target": a.target, "only_listed": a.only_listed, "split": a.split,
          "site": _need_site(s) if (a.split != "all" or s.site) and s.site and Path(s.site).is_dir() else None}
    if a.split != "all" and kw["site"] is None:
        raise SystemExit("--split 에는 사이트 팩이 필요합니다: --site 또는 MINEDOCSCAN_SITE")
    if a.answers:
        data = {"fields": evaluate_fields(con, load_answers_json(a.answers), **kw)}
    else:
        site = kw["site"] or _need_site(s)
        name = a.template or _only_inspection_template(site)
        answers = load_answers(a.inspection_csv, site.templates[name])
        data = {"fields": evaluate_fields(con, answers, **kw),
                "insp_daily": evaluate_inspection(con, answers, site.templates[name])}
    data["presence"] = evaluate_presence(con)
    f, pr = data["fields"], data["presence"]
    lines = [f"필드 {f['n']}개 ({a.target}, 분할 {a.split}): CER {f['cer']}, 필드 정확도 {f['field_accuracy']}, "
             f"자동 적재율 {f['auto_rate']}"
             f" (DB 에 없는 정답 {f['answers_not_in_db']}개)",
             f"  정답에 값이 있는 셀 {f['n_value']}개 정확도 {f['accuracy_value']}, 빈 칸 {f['n_empty']}개 정확도 {f['accuracy_empty']}"]
    for k, v in f["by_field_kind"].items():
        lines.append(f"  {k}: n={v['n']} CER {v['cer']} 정확도 {v['field_accuracy']} (값 {v['accuracy_value']} / 빈 칸 "
                     f"{v['accuracy_empty']}) 자동 {v['auto_rate']}")
    lines.append(f"값 유무 판단 (검수 {pr['n']}셀): 정밀도 {pr['precision']}, 재현율 {pr['recall']} "
                 f"(tp {pr['tp']}, fp {pr['fp']}, fn {pr['fn']}, tn {pr['tn']})")
    if "insp_daily" in data:
        d = data["insp_daily"]
        lines.append(f"점검 행 {d['rows']}개: 점검내역 CER {d['remark_corpus_cer']}, "
                     f"정확도 {d['remark_field_accuracy']}, 행 자동 적재율 {d['auto_rate']}")
    _emit(a, data, "\n".join(lines))
    return 0


def cmd_regress(a) -> int:
    from .evaluate.regression import run_regression

    s = _settings(a)
    res = run_regression(s, _need_site(s), update=a.update, inputs=a.inputs)
    lines = []
    if res["diffs"]:
        lines.append(f"기준과 다른 항목 {len(res['diffs'])}개 ({res['baseline']}):")
        lines += [f"  {k}: 기준 {e} → 지금 {v}" for k, e, v in res["diffs"]]
    elif res["had_baseline"]:
        lines.append("기준과 같습니다.")
    else:
        lines.append("기준이 아직 없습니다.")
    if res["updated"]:
        lines.append(f"기준을 저장했습니다: {res['baseline']}")
    _emit(a, {k: v for k, v in res.items() if k != "report"} | {"report": res["report"]}, "\n".join(lines))
    return 0 if (res["ok"] or res["updated"]) else 1


def cmd_template(a) -> int:
    from .tools.mktemplate import init_template

    s = _settings(a)
    if s.site is None:
        raise SystemExit("사이트 팩이 지정되지 않았습니다: --site 또는 MINEDOCSCAN_SITE")
    roi = tuple(int(v) for v in a.roi.split(",")) if a.roi else None
    if roi and len(roi) != 4:
        raise SystemExit("--roi 는 x0,y0,x1,y1 네 숫자입니다")
    path = init_template(a.image, a.name, Path(s.site) / "templates", roi=roi, header_rows=a.header_rows,
                         page=a.page, dpi=s.dpi, handler=a.handler, overwrite=a.overwrite)
    _emit(a, {"template": str(path)}, f"템플릿 뼈대를 만들었습니다: {path}\n열 이름·kind·행 키를 채우세요 (docs/SITE_PACK.md).")
    return 0


def cmd_synth(a) -> int:
    from .tools.synth import generate

    r = generate(a.out, days=a.days, seed=a.seed)
    text = (f"합성 데이터를 만들었습니다: {r.root}\n"
            f"  사이트 팩  {r.site}\n  스캔 문서  {r.scans}\n  정답       {r.truth_path}, {r.answers_path}\n"
            f"실행 예: minedocscan run --site {r.site} --archive-root {r.scans} --work-root {r.root / 'work'}")
    _emit(a, {"root": str(r.root), "site": str(r.site), "scans": str(r.scans), "truth": str(r.truth_path),
              "answers": str(r.answers_path), "expected": r.truth["expected"]}, text)
    return 0


def _resolve(p: str, s: Settings) -> Path:
    path = Path(p)
    if not path.exists() and s.archive_root and (s.archive_root / p).exists():
        return s.archive_root / p
    if not path.exists():
        raise SystemExit(f"경로가 없습니다: {p}")
    return path


def _only_inspection_template(site) -> str:
    names = [t.name for t in site.templates.values() if t.handler == "inspection"]
    if len(names) != 1:
        raise SystemExit(f"--template 을 지정하세요 (inspection 핸들러 템플릿: {names})")
    return names[0]


def _review_db(a):
    """검수 명령 공통: 사이트 팩, DB, 검수 파일 읽어 들이기."""
    from .review.store import import_into
    from .store.db import open_db

    s = _settings(a)
    site = _need_site(s)
    con = open_db(s.resolved_db_url)
    imported = import_into(con, s.reviews_path(site.root))
    return s, site, con, imported


def cmd_review(a) -> int:
    if a.review_command == "serve":
        from .review.server import ReviewApp, serve

        if not a.reviewer:
            raise SystemExit("검수자를 지정하세요: --reviewer <짧은 영문 식별자>. 검수 기록마다 남습니다.")
        s, site, con, imported = _review_db(a)
        if imported["skipped"]:
            print(f"주의: 검수 파일에서 깨진 줄 {imported['skipped']}개를 건너뛰었습니다 ({imported['path']})", file=sys.stderr)
        app = ReviewApp(con, site, s, a.reviewer, a.queue,
                        {"n": a.n, "seed": a.seed, "empty_share": a.empty_share, "template": a.template, "kind": a.kind})
        try:
            serve(app, port=a.port)
        except OSError as e:
            raise SystemExit(str(e)) from e
        return 0
    if a.review_command == "stats":
        from .review.store import stats

        s, site, con, imported = _review_db(a)
        st = stats(con, site)
        kv = lambda d: ", ".join(f"{k} {v}" for k, v in d.items()) or "-"      # noqa: E731
        lines = [f"검수 파일: {imported['path']} — 기록 {st['records']}건 (깨진 줄 {imported['skipped']}개), 필드 {st['fields']}개",
                 "판정별: " + kv(st["by_verdict"]), "양식별: " + kv(st["by_template"]), "날짜별: " + kv(st["by_date"]),
                 "검수자별(기록): " + kv(st["by_reviewer"]),
                 "분할별(value·empty): " + (", ".join(f"{k} {v['fields']}셀/{v['dates']}일" for k, v in st["by_split"].items()) or "-")
                 + f"  (소금값 {site.split_salt}, test 비율 {site.test_share})",
                 f"템플릿 좌표가 달라진 기록 {st['bbox_changed']}개, 이 DB 에 없는 필드 {st['fields_not_in_db']}개"]
        _emit(a, {"reviews": imported, "stats": st}, "\n".join(lines))
        return 0
    if a.review_command == "export-answers":
        from .review.store import export_answers

        s, site, con, _imported = _review_db(a)
        n = export_answers(con, a.out, split=a.split, site=site)
        sp = "" if a.split == "all" else f" --split {a.split}"
        _emit(a, {"out": a.out, "answers": n, "split": a.split},
              f"정답 {n}개를 썼습니다 (분할 {a.split}): {a.out}\n"
              f"비교: minedocscan eval --answers {a.out} --target raw --only-listed{sp}")
        return 0
    if a.review_command == "export-crops":
        from .review.export import ExportError, export_crops

        s, site, con, _imported = _review_db(a)
        try:
            r = export_crops(con, site, s, a.out, split=a.split, kind=a.kind, res=a.res, out_scale=a.scale, pad=a.pad,
                             allow_in_repo=a.allow_in_repo)
        except (ExportError, ValueError) as e:
            raise SystemExit(str(e)) from e
        _emit(a, r, f"크롭 {r['written']}개를 썼습니다: {r['out']} — 분할별 {r['by_split']}, 해상도별 {r['by_source']}, "
                    f"읽을 수 없음 제외 {r['skipped_illegible']}개\n저장소에 넣지 마세요 — 현장의 글씨가 들어 있습니다.")
        return 0
    raise SystemExit(f"알 수 없는 review 명령: {a.review_command}")


COMMANDS = {"info": cmd_info, "run": cmd_run, "report": cmd_report, "pages": cmd_pages, "eval": cmd_eval,
            "regress": cmd_regress, "template": cmd_template, "synth": cmd_synth, "review": cmd_review}


def main(argv: list[str] | None = None) -> int:
    from .store.db import SchemaVersionError

    args = build_parser().parse_args(argv)
    try:
        return COMMANDS[args.command](args)
    except SchemaVersionError as e:                 # 안내문만 보이면 된다. 트레이스백은 필요 없다
        raise SystemExit(str(e)) from e


if __name__ == "__main__":
    raise SystemExit(main())

"""명령줄 진입점: `minedocscan <명령>`.

  info       설정·사이트 팩·등록된 백엔드 확인
  run        스캔 파일/폴더 → 분류 → 정합 → 추출 → 인식 → 검증 → 적재 → 교차검증
  report     DB 현황 (양식별 페이지, 정합 품질, 검수 대기, 교차검증). --by-month 는 양식 × 월 진단
  pages      쪽 목록 (상태·양식·분류 여유로 거름). --thumbs 는 원본 쪽의 미리보기 PNG
  eval       정답과 비교 (CER, 필드 정확도, 자동 적재율)
  regress    사이트 팩의 기준 수치와 비교하는 실데이터 회귀 검사
  template   템플릿 도구: init(뼈대), check(오류 전부), preview(칸을 그린 그림, --print), print-layer(인쇄 층),
             variant(같은 날 섞여 쓰이는 판 — 괘선만 다시 잡는다), add-region(표 하나를 더한다)
  synth      개인정보 없는 합성 사이트 팩과 스캔 문서 만들기
  review     검수: serve(로컬 화면), stats(진행 현황), export-answers(검수값 → 정답 파일), export-crops(학습용 크롭)
  recognizer 숫자 인식기: train(학습, torch 필요), list(사이트 팩의 모델), eval(크롭에서 바로 평가)

경로는 --site / --archive-root / --work-root 또는 환경변수·설정 파일로 준다 (config.py).
"""
from __future__ import annotations

import argparse
import json
import re
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
    p.add_argument("--recognizer", help="기본 인식 백엔드 ([recognize] backend 대신). [recognize.by_kind] 에 적힌 종류는 그쪽 "
                                        "백엔드가 읽는다 — 전부를 덮는 것은 --answers(oracle)뿐")
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
    p.add_argument("--status", help="unknown_form | blank | classified_only | align_failed | duplicate | loaded | discarded | error")
    p.add_argument("--template", help="이 양식만")
    p.add_argument("--low-margin", action="store_true", help="분류 여유가 classify_min_margin 아래인 쪽만")
    p.add_argument("--thumbs", nargs="?", const="", metavar="DIR",
                   help="목록의 쪽을 1/4 로 줄인 PNG 로 쓴다 (기본 WORK_ROOT/thumbs/<상태>/). 저장소 밖에만")
    p.add_argument("--meta-mismatch", action="store_true",
                   help="기계가 읽은 메타 값이 사람·파일명의 값과 다른 쪽 (날짜 포함). 값은 찍지 않는다 — 검수 화면 meta-check 에서 본다")
    p.add_argument("--meta-key", help="--meta-mismatch: 이 키만 (vehicle_no, operator, date.day …)")
    p.add_argument("--variants", action="store_true",
                   help="같은 날 섞여 쓰이는 판(concurrent)마다 정합한 쪽 중 두 판의 괘선 오차 차이가 1 px 미만인 쪽 "
                        "(판마다의 오차를 같이 낸다. 두 판 모두 정합에 실패한 쪽도 상태 align_failed 로 나온다)")
    p.add_argument("--rotated", action="store_true", help="돌아서 들어와 세운 쪽만 (방향이 0 이 아닌 쪽 — tasks/0007 4.4)")

    p = sub.add_parser("eval", parents=[common], help="정답과 비교")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--answers", help="정답 JSON (synth 가 만드는 answers.json 형식)")
    g.add_argument("--inspection-csv", metavar="DIR", help="점검표 정답 CSV 폴더 (YYMMDD.csv)")
    g.add_argument("--meta", action="store_true",
                   help="쪽 메타: 기계가 읽은 값 대 사람·파일명의 값 — 키마다 정확도·자동 적재율·자동 적재 오류율, 배차가 바뀐 쪽, 자리")
    g.add_argument("--checks", action="store_true",
                   help="✓ 판정: 기계의 답 대 검수(review serve --queue checks)로 정한 행의 답 — 표, 정확도, 판정 불가, column_unused")
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
    t.add_argument("--rotate", type=int, choices=[0, 90, 180, 270], default=0,
                   help="돌아간 스캔이면 시계 방향으로 이만큼 돌려 세운 그림을 기준 이미지로 (tasks/0007 4.4). 없으면 들어온 방향 그대로 "
                        "— '바로 선 것'은 기준 이미지의 방향이다")
    t = tsub.add_parser("preview", parents=[common],
                        help="칸·필드의 테두리와 이름·종류·형식·역할·행 번호를 기준 이미지(또는 정합한 스캔) 위에 그린 PNG")
    t.add_argument("template_dir", help="템플릿 폴더 (<site>/templates/<양식>)")
    t.add_argument("--scan", help="이 스캔의 쪽을 정합해서 그 위에 그린다 (칸이 실제 글씨에 맞는지)")
    t.add_argument("--page", type=int, default=1, help="--scan 의 쪽 번호 (1부터)")
    t.add_argument("--out", help="출력 폴더 (기본 WORK_ROOT/template-preview). 저장소 안은 거절한다")
    t.add_argument("--print", dest="print_layer", action="store_true",
                   help="인쇄 층(print_image) 위에 그린다 — 인쇄 화소에 색. 값 자리가 인쇄에 덮이지 않았나 (tasks/0006)")
    t = tsub.add_parser("print-layer", parents=[common],
                        help="그 양식으로 분류된 쪽들에서 인쇄 층(손글씨가 빠진 빈 양식)을 만든다 → <템플릿 폴더>/print.png + 요약")
    t.add_argument("template_dir", help="템플릿 폴더 (<site>/templates/<양식>)")
    t.add_argument("--max-pages", type=int, default=40, help="쓸 쪽의 최대 수 — 날짜별로 고르게 (기본 40, 3장 미만이면 거절)")
    t.add_argument("--percentile", type=_number, default=75,
                   help="화소마다 밝기의 백분위 (기본 75). 판이 섞였을 수 있는 양식의 첫 층은 50 — 괘선을 잡는 데(add-region)만 쓰고, "
                        "값 유무에 쓰는 층은 판을 나눈 뒤 75 로 다시 만든다 (75 미만이면 요약이 경고한다)")
    t.add_argument("--out", help="출력 PNG 파일 (기본 <템플릿 폴더>/print.png). git 작업 트리 안은 거절한다")
    t.add_argument("--allow-in-repo", action="store_true", help="저장소 안에도 쓴다 (합성 사이트 팩만)")
    t = tsub.add_parser("variant", parents=[common],
                        help="같은 날 섞여 쓰이는 다른 인쇄 판의 템플릿: 열·행·필드는 그대로, 표마다 괘선만 새 스캔에서 다시 잡는다")
    t.add_argument("template_dir", help="기존 판의 템플릿 폴더 (<site>/templates/<양식>) — 고치지 않는다")
    t.add_argument("--scan", required=True, help="새 판의 깨끗한 쪽이 든 스캔 (이미지·PDF)")
    t.add_argument("--page", type=int, default=1, help="--scan 의 쪽 번호 (1부터)")
    t.add_argument("--name", required=True, help="새 판의 템플릿 이름 (예: <양식>_b)")
    t.add_argument("--out-dir", help="새 판의 폴더 (기본: 기존 판 옆의 <NAME>). git 작업 트리 안이거나 이미 있으면 거절한다")
    t = tsub.add_parser("add-region", parents=[common],
                        help="그 영역의 괘선을 잡아 표 하나의 뼈대를 template.yaml 의 regions 끝에 더한다 (인쇄 층이 있으면 그것에서)")
    t.add_argument("template_dir", help="템플릿 폴더 (<site>/templates/<양식>)")
    t.add_argument("--roi", required=True, help="표 영역 x0,y0,x1,y1 (템플릿 좌표 — 기준 이미지 픽셀). 표 둘레를 조금 넉넉히, "
                   "이웃 표까지의 간격보다는 좁게")
    t.add_argument("--name", required=True, help="표 이름 (영문·숫자·밑줄) — 같은 이름의 표가 있으면 거절한다")
    t.add_argument("--role", help="usage 핸들러의 표의 역할 meter | shifts | tally | activities — 그 역할이 요구하는 열의 자리표시로")
    t.add_argument("--header-rows", type=int, default=1, help="머리 행의 수 (기본 1)")
    t.add_argument("--allow-in-repo", action="store_true", help="저장소 안의 템플릿도 고친다 (합성 사이트 팩만)")
    t = tsub.add_parser("check", parents=[common],
                        help="템플릿의 오류를 전부: 읽기 오류, 겹치는 칸, 쪽 밖의 칸, 역할에 필요한 칸, 형식과 종류의 불일치 …")
    t.add_argument("template_dir", help="템플릿 폴더 (<site>/templates/<양식>)")

    p = sub.add_parser("synth", parents=[common], help="합성 사이트 팩 + 스캔 문서 + 정답 생성")
    p.add_argument("out", help="출력 폴더 (site/, scans/, truth.json, answers.json)")
    p.add_argument("--days", type=int, default=3)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--low-cells", action="store_true",
                   help="운반 양식 두 종을 실제처럼 낮은 칸·거친 숫자·X 표로 (숫자 인식기 시험용)")
    p.add_argument("--meta-fields", action="store_true",
                   help="일보의 차량번호(네 자리)·작성자를 사람마다 다른 획으로, 날짜 줄에 월·일 필드 (메타 필드 인식기 시험용)")
    p.add_argument("--mix-pages", action="store_true", help="--meta-fields 와 함께: 마지막 날의 묶음에 첫날의 일보 한 쪽을 섞는다")
    p.add_argument("--usage-logs", action="store_true",
                   help="장비 가동 일보 두 종(작업 표 + 계기 / 작업량 표 + 근무 시각 + 계기)을 날마다 묶음 끝에 붙인다")
    p.add_argument("--usage-only", action="store_true", help="가동 일보만 (점검표·운반 쪽 없이)")
    p.add_argument("--print-layers", action="store_true",
                   help="--usage-logs/--usage-only 와 함께: 가동 일보 두 종의 인쇄 층(print.png)을 합성 쪽에서 추정해 템플릿에 넣는다 "
                        "(print_image)")
    p.add_argument("--usage-variants", action="store_true",
                   help="--usage-logs/--usage-only 와 함께: 운행일보에 같은 날 섞여 쓰이는 판 B(표만 아래로 옮긴 판)를 더한다 — "
                        "두 판에 family·concurrent: true (tasks/0006)")
    p.add_argument("--rotate-pages", action="store_true",
                   help="스캔한 쪽마다 0·90·180·270° 중 하나로 돌려서 담는다 (B5 가로를 세로로 넣은 스캐너 — tasks/0007)")
    p.add_argument("--blank-backs", action="store_true",
                   help="쪽마다 빈 뒷면(흰 종이·티·가장자리 그림자·옅게 비친 앞면)을 붙인다 (양면 스캔 — tasks/0007)")

    p = sub.add_parser("review", parents=[common], help="검수 도구")
    rsub = p.add_subparsers(dest="review_command", required=True)
    r = rsub.add_parser("serve", parents=[common], help="로컬 검수 화면 (127.0.0.1)")
    r.add_argument("--queue", default="haul-numbers",
                   choices=["haul-numbers", "mismatch", "pending", "page-fields", "meta-check", "checks", "readings",
                            "usage-check"])
    r.add_argument("--audit", type=int, metavar="N",
                   help="page-fields·readings: 기계의 상태(잉크)와 상관없이 날짜별로 고르게 뽑은 쪽 N 개 (기계 값 없이) — "
                        "자동 적재된(빈 칸으로 본) 쪽의 오류를 잴 정답")
    r.add_argument("--n", type=int, help="표본 크기: haul-numbers 기본 1500, checks(점검표 행) 기본 300")
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
    r.add_argument("--include-illegible", action="store_true",
                   help="'읽을 수 없음'(illegible)도 내보낸다 — 숫자 인식기가 '거절'로 학습한다 (labels.jsonl 의 verdict)")
    r.add_argument("--meta", action="store_true",
                   help="표 밖 메타 필드(차량번호·작성자·날짜의 월·일)의 크롭 + 사람·파일명 값 → OUT/<split>/meta/<키>/. "
                        "여유 기본 8 px")
    r.add_argument("--meta-key", help="--meta: 이 키만 (vehicle_no, operator, date.month, date.day …)")

    p = sub.add_parser("recognizer", parents=[common], help="숫자 인식기: 학습·목록")
    nsub = p.add_subparsers(dest="recognizer_command", required=True)
    n = nsub.add_parser("train", parents=[common], help="크롭 + 합성 셀로 학습 → <site>/models/<이름>/ (torch 필요: .[train])")
    n.add_argument("--crops", help="review export-crops 로 내보낸 폴더 (test 줄이 있으면 거절). 없으면 합성 셀만으로")
    n.add_argument("--name", required=True, help="모델 이름 (영문·숫자·.-_). 같은 이름이 있으면 멈춘다")
    n.add_argument("--synthetic", type=int, help="합성 셀 수 (기본: 실제 셀이 있으면 4000, 없으면 8000)")
    n.add_argument("--seed", type=int, default=0)
    n.add_argument("--val-share", type=float, default=0.2, help="검증으로 떼는 train 날짜의 비율 (기본 0.2)")
    n.add_argument("--target-auto-error", type=float,
                   help="자동 적재 오류율의 목표 (검증 날짜, 기본: 숫자 칸 0.01, 메타 필드 0.02). 이 이하인 가장 낮은 임계값을 고른다")
    n.add_argument("--min-val-auto", type=int, default=100,
                   help="기준을 정하려면 그 임계값에서 자동 적재된 검증 칸이 이만큼은 있어야 한다 (기본 100). 모자라면 자동 적재 없음")
    n.add_argument("--steps", type=int, help="학습 스텝 (배치 64, 기본 2500 — CPU 4코어에서 약 1분 반. 분류기(choice)는 기본 600)")
    n.add_argument("--synthetic-geometry", metavar="WxH[,WxH…]",
                   help="합성 칸 크기(템플릿 px, doc_field bbox). 실제 셀이 없을 때만 쓴다 (있으면 실제 칸 크기). 기본 92x21")
    n.add_argument("--out", help="모델 폴더를 직접 지정 (기본: <site>/models/<이름>)")
    n.add_argument("--allow-in-repo", action="store_true", help="git 작업 트리 안에도 쓴다 (합성 셀만으로 만든 시험용 모델)")
    n.add_argument("--meta-key", metavar="KEY[,KEY…]",
                   help="표 밖 메타 필드의 모델: vehicle_no, operator, date.month, date.day … (export-crops --meta 의 폴더). "
                        "쉼표로 여럿 — 숫자 모델만")
    n.add_argument("--reader", choices=["digits", "choice"],
                   help="--meta-key: 읽는 법. 기본은 정답이 전부 숫자열이면 digits(읽고 목록에서 고른다), 아니면 choice(분류기)")
    n.add_argument("--cv", type=int, metavar="K",
                   help="--meta-key: train 날짜를 K 묶음으로 나눠 돌려 가며 읽은 것 전체로 온도·기준을 정한다 (정답이 적을 때)")
    n.add_argument("--min-examples", type=int, default=3, help="--reader choice: 종류가 되려면 필요한 예의 수 (기본 3)")
    n.add_argument("--extra-digits", metavar="DIR",
                   help="--meta-key (숫자 모델): 숫자 칸(운반 횟수)을 export-crops 로 내보낸 폴더 — 그 칸의 숫자를 학습에만 더한다 "
                        "(후보·기준에는 쓰지 않는다. test 줄이 있거나 규격이 다르면 거절)")
    n.add_argument("--synthetic-meta", type=int, metavar="DAYS",
                   help="--meta-key, --crops 없이: 합성 메타 필드 DAYS 일치로 학습 (시험용 모델, tools/synth_meta)")
    nsub.add_parser("list", parents=[common], help="사이트 팩의 모델과 카드 요약")
    n = nsub.add_parser("eval", parents=[common], help="크롭 폴더에서 바로 평가 (파이프라인을 돌리지 않는다)")
    n.add_argument("--crops", required=True, help="review export-crops 로 내보낸 폴더")
    n.add_argument("--model", required=True, help="모델 이름(<site>/models/<이름>) 또는 폴더 경로")
    n.add_argument("--split", choices=["val", "test", "train"], default="val",
                   help="val = 카드의 검증 규칙이 고르는 train 날짜 (기본), train = 그 나머지, test = test 로 내보낸 폴더 — 마지막에 한 번")
    n.add_argument("--errors", nargs="?", const="", metavar="DIR",
                   help="틀린 칸을 한 장에 모은 그림 (기본 WORK_ROOT/recognizer-errors). git 작업 트리 안이면 거절")
    return ap


def _number(text: str) -> int | float:
    """정수면 정수로 (요약에 75 로 찍히게), 아니면 실수."""
    v = float(text)
    return int(v) if v.is_integer() else v


def _settings(a: argparse.Namespace, **extra) -> Settings:
    return load_settings(a.config, site=a.site, archive_root=a.archive_root, work_root=a.work_root,
                         db_url=a.db_url, **extra)


def _emit(a: argparse.Namespace, data: dict, text: str) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=1) if a.json else text)


def _need_site(s: Settings):
    from .forms.sitepack import SitePack

    if s.site is None:
        raise SystemExit("사이트 팩이 지정되지 않았습니다: --site 또는 MINEDOCSCAN_SITE")
    try:
        return SitePack(s.site)
    except FileNotFoundError as e:
        raise SystemExit(str(e)) from e


# ── 명령 ───────────────────────────────────────────────────────────────────
def cmd_info(a) -> int:
    from .correct import REGISTRY as CORRECTORS
    from .handlers import REGISTRY as HANDLERS
    from .recognize import available as recognizers_available

    s = _settings(a)
    data = {
        "version": __version__,
        "settings": {"site": str(s.site) if s.site else None,
                     "archive_root": str(s.archive_root) if s.archive_root else None,
                     "work_root": str(s.work_root), "db_url": s.resolved_db_url, "dpi": s.dpi,
                     "recognizer": s.recognizer, "corrector": s.corrector, "auto_accept_conf": s.auto_accept_conf},
        "backends": {"recognizers": recognizers_available(), "correctors": sorted(CORRECTORS), "handlers": sorted(HANDLERS)},
        "site": None,
    }
    lines = [f"minedocscan {__version__}"] + [f"  {k}: {v}" for k, v in data["settings"].items()]
    lines.append("백엔드: " + ", ".join(f"{k}={v}" for k, v in data["backends"].items()))
    site = _need_site(s) if s.site and Path(s.site).is_dir() else None
    data["recognizer"] = _describe_recognizer(s, site)
    data["meta_readers"] = _describe_meta(s, site)
    rec = data["recognizer"]
    if "error" in rec:
        lines.append(f"인식기: 준비할 수 없습니다 — {rec['error']}")
    else:
        for kind, d in rec["by_kind"].items():
            m = (f" — 모델 {d['model']} ({d['path']}), 규격 {d['spec']}, 자동 적재 기준 "
                 f"{d['auto_accept_conf'] if d['auto_accept_conf'] is not None else '없음'} ({d['auto_accept_source']}"
                 + (f", 검증 오류율 95 % 상한 {d['auto_accept_upper95']:.1%}" if d.get("auto_accept_upper95") is not None else "")
                 + "), "
                 f"학습 셀 {d['train_cells']} + 합성 {d['synthetic_cells']}") if "model" in d else ""
            lines.append(f"인식기 [{kind}]: {d['backend']}{m}")
    mr = data["meta_readers"]
    if "error" in mr:
        lines.append(f"메타 필드: 준비할 수 없습니다 — {mr['error']}")
    for k, d in mr.get("by_key", {}).items():
        lines.append(f"메타 필드 [{k}]: 모델 {d['model']} ({d['reader']}, {d['path']}), 규격 {d['spec']}, 자동 적재 기준 "
                     f"{d['auto_accept_conf'] if d['auto_accept_conf'] is not None else '없음'}"
                     + (f" (검증 오류율 95 % 상한 {d['auto_accept_upper95']:.1%}, {d['method']})"
                        if d.get("auto_accept_upper95") is not None else f" ({d['method']})"))
    if site is not None:
        tpls = [{"name": t.name, "title": t.title, "handler": t.handler, "regions": len(t.regions),
                 "cells": len(t.cells()) + len(t.fields), "status": "cells" if t.has_cells else "classify_only",
                 "family": t.family, "valid_from": t.valid_from, "valid_to": t.valid_to, "concurrent": t.concurrent,
                 "print_image": t.spec.get("print_image"), "print_sha": _print_sha(t),
                 "print_used": t.uses_print_layer}          # 인쇄 층으로 재는 칸이 있는가 (role meter·shifts·tally — 4.3)
                for t in site.templates.values()]
        from .forms.equipment import master_keys

        n_master = len(master_keys(site.templates.values()))
        data["site"] = {"name": site.name, "templates": tpls, "labels": len(site.labels),
                        "equipment_aliases": len(site.equipment_aliases),
                        "equipment_aliases_sha": site.equipment_aliases_sha, "equipment_master": n_master}
        lines.append(f"사이트 팩: {site.name} — 템플릿 {len(tpls)}종, 페이지 라벨 {len(site.labels)}개, "
                     f"장비명 대응표 {len(site.equipment_aliases)}개 (해시 {site.equipment_aliases_sha}, 마스터 {n_master}대)")
        for t in tpls:
            valid = (f"  계열 {t['family']} {t['valid_from'] or '…'}~{t['valid_to'] or '…'}"
                     + (" (같은 날 섞여 쓰이는 판)" if t["concurrent"] else "") if t["family"] else "")
            printed = (f"  인쇄 층 {t['print_image']} ({t['print_sha'] or '읽을 수 없음'})"
                       + ("" if t["print_used"] else " — 인쇄 층으로 재는 칸(role meter·shifts·tally)이 없어 쓰지 않음")
                       if t["print_image"] else "")
            lines.append(f"  {t['name']:<24} handler={t['handler']:<11} 표 {t['regions']}개, 셀 {t['cells']}개"
                         + ("" if t["status"] == "cells" else "  (분류 전용 — 셀 정의 없음)") + valid + printed)
    else:
        lines.append("사이트 팩: 지정되지 않았거나 폴더가 없습니다")
    _emit(a, data, "\n".join(lines))
    return 0


def _print_sha(t) -> str | None:
    """info: 템플릿의 인쇄 층 해시 (print_image 가 없으면 None, 읽을 수 없으면 None — 글에는 "읽을 수 없음")."""
    try:
        return t.print_sha
    except (OSError, ValueError):
        return None


def _describe_recognizer(s: Settings, site) -> dict:
    """info: 칸 종류마다 어느 백엔드가 받는지, 숫자 모델이면 이름·규격·자동 적재 기준·학습 셀 수."""
    from .recognize import KINDS, build_recognizer

    try:
        rec = build_recognizer(s, site)
    except (KeyError, ValueError, FileNotFoundError) as e:
        return {"error": str(e)}

    def one(b) -> dict:
        d = {"backend": b.name}
        if callable(getattr(b, "describe", None)):
            d |= b.describe()
        return d

    backend_for = getattr(rec, "backend_for", lambda _k: rec)
    return {"by_kind": {k: one(backend_for(k)) for k in KINDS}}


def _describe_meta(s: Settings, site) -> dict:
    """info: 메타 필드 키마다 모델·읽는 법·기준(상한 포함). 값(이름·차량번호)은 내지 않는다."""
    from .recognize.meta.model import MetaModelError, build_meta_readers

    try:
        readers = build_meta_readers(s, site)
    except MetaModelError as e:
        return {"error": str(e)}
    return {"by_key": {k: m.describe() for k, m in sorted(readers.items())}}


def cmd_run(a) -> int:
    from .pipeline import Pipeline
    from .recognize import OracleRecognizer, build_recognizer, load_answers_json
    from .report import build_report, format_report, xcheck_by_date

    s = _settings(a, recognizer=a.recognizer, corrector=a.corrector)
    site = _need_site(s)
    paths = [_resolve(p, s) for p in a.paths] or ([s.archive_root] if s.archive_root else [])
    if not paths:
        raise SystemExit("처리할 경로가 없습니다: PATH 를 주거나 archive_root 를 지정하세요")
    if a.answers or a.inspection_csv:
        answers = load_answers_json(a.answers) if a.answers else {}
        if a.inspection_csv:
            from .evaluate.inspection_csv import load_answers

            answers |= load_answers(a.inspection_csv, site.templates[_only_inspection_template(site)])
        recognizer = OracleRecognizer(answers, site=site)       # 동시 판의 정답은 계열로 (tasks/0006 4.6)
    elif s.recognizer == "oracle":
        raise SystemExit("oracle 백엔드는 --answers 또는 --inspection-csv 가 필요합니다")
    else:
        try:
            recognizer = build_recognizer(s, site)
        except (KeyError, ValueError, FileNotFoundError) as e:
            raise SystemExit(f"인식 백엔드를 준비할 수 없습니다: {e}") from e
    from .recognize.meta.model import MetaModelError, build_meta_readers

    try:
        meta_readers = build_meta_readers(s, site)
    except MetaModelError as e:
        raise SystemExit(f"메타 필드 모델을 준비할 수 없습니다: {e}") from e
    if a.fresh and s.resolved_db_url.startswith("sqlite:///"):   # 인식기(모델)를 준비한 뒤에 지운다 — 모델이 없으면 DB 는 그대로
        Path(s.resolved_db_url[len("sqlite:///"):]).unlink(missing_ok=True)
    pipe = Pipeline(s, site=site, recognizer=recognizer, meta_readers=meta_readers)
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
    rep = build_report(pipe.con, pipe.site.variant_families())
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
    from .report import (
        build_report,
        by_month,
        format_by_month,
        format_report,
        format_stale_equipment_ids,
        stale_equipment_ids,
        xcheck_by_date,
        xcheck_usage_by_date,
    )
    from .store.db import open_db

    s = _settings(a)
    con = open_db(s.resolved_db_url)
    if a.by_month:
        rows = by_month(con, s.classify_min_margin)
        _emit(a, {"by_month": rows}, format_by_month(rows))
        return 0
    site, note = _load_site(s)
    rep = build_report(con, site.variant_families() if site is not None else None)
    by_date, usage_by_date = xcheck_by_date(con), xcheck_usage_by_date(con)
    text = format_report(rep, by_date)
    if usage_by_date:
        text += "\n날짜별 가동 일보 검산:\n" + "\n".join(
            f"  {d['work_date']}: " + ", ".join(f"{k} {v}" for k, v in d.items() if k != "work_date") for d in usage_by_date)
    data = {"report": rep, "xcheck_by_date": by_date, "xcheck_usage_by_date": usage_by_date}
    if site is not None and _has_usage_rows(con):           # report 의 dict 밖에 둔다 — regress 가 비교하지 않는다
        stale = stale_equipment_ids(con, site)              # 가동 기록이 없는 사이트(운반·점검표)는 키가 없다 — JSON 이 예전과 같다
        data["stale_equipment_ids"] = stale
        line = format_stale_equipment_ids(stale)
        text += f"\n{line}" if line else ""
    elif note:
        text += f"\n{note}"
    _emit(a, data, text)
    return 0


def _has_usage_rows(con) -> bool:
    """eq_usage_daily 나 prod_tally 에 행이 있나 — 낡은 장비 ID 를 셀 것이 있는 DB."""
    return any(con.execute(f"SELECT 1 FROM {t} LIMIT 1").fetchone() for t in ("eq_usage_daily", "prod_tally"))


def _load_site(s: Settings) -> tuple:
    """report 가 쓰는 사이트 팩: 대응표를 고친 뒤 낡은 장비 ID 의 수(report.stale_equipment_ids)와 동시 판의 계열
    (report.variant_summary). 사이트 팩이 없으면 (None, None), 읽을 수 없으면 (None, 한 줄 안내) — 그 검사만 건너뛰고
    동시 판은 판 이름으로 묶는다."""
    from .forms.sitepack import SitePack

    if s.site is None or not Path(s.site).is_dir():
        return None, None
    try:
        return SitePack(s.site), None
    except Exception as e:                                  # 망가진 팩(re.error·AttributeError …)이 report 전체를 막지 않게
        return None, f"(사이트 팩을 읽을 수 없어 장비 ID 검사를 건너뛰었습니다: {type(e).__name__})"   # 종류 이름만 (값 없이)


def cmd_pages(a) -> int:
    from .report import format_pages, list_pages
    from .store.db import open_db

    s = _settings(a)
    con = open_db(s.resolved_db_url)
    if a.meta_mismatch or a.meta_key:
        from .report import format_meta_mismatch, meta_mismatch_pages

        mm = meta_mismatch_pages(con, a.meta_key)
        _emit(a, {"meta_mismatch": mm}, format_meta_mismatch(mm) + f"\n대조가 어긋난 (쪽, 키) {len(mm)}개 — "
              "검수 화면: minedocscan review serve --queue meta-check")
        return 0
    rows = list_pages(con, status=a.status, template=a.template,
                      low_margin=s.classify_min_margin if a.low_margin else None, variants=a.variants,
                      rotated=a.rotated)
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
    if a.meta:
        return _eval_meta(a, con, kw["site"])
    if a.checks:
        from .evaluate.checks import evaluate_checks, format_checks

        site = kw["site"] or _need_site(s)
        r = evaluate_checks(con, site, split=a.split)
        _emit(a, {"checks": r}, format_checks(r))
        return 0
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
    ae = f["auto_error"]
    lines.append(f"  자동 적재 오류율(인식기가 자동 적재한 칸, 기계 값): {ae['wrong']}/{ae['auto']} = {ae['rate']} "
                 f"(95% {ae['ci95'][0]}–{ae['ci95'][1]}; 값 {ae['auto_value']}, 빈 칸 {ae['auto_empty']}; "
                 f"잉크 없음으로 확정 {ae['ink_auto']})")
    for k, v in f["by_field_kind"].items():
        e = v["auto_error"]
        lines.append(f"  {k}: n={v['n']} CER {v['cer']} 정확도 {v['field_accuracy']} (값 {v['accuracy_value']} / 빈 칸 "
                     f"{v['accuracy_empty']}) 자동 {v['auto_rate']} · 자동 적재 오류 {e['wrong']}/{e['auto']}")
    lines.append(f"값 유무 판단 (검수 {pr['n']}셀): 정밀도 {pr['precision']}, 재현율 {pr['recall']} "
                 f"(tp {pr['tp']}, fp {pr['fp']}, fn {pr['fn']}, tn {pr['tn']})")
    if "insp_daily" in data:
        d = data["insp_daily"]
        lines.append(f"점검 행 {d['rows']}개: 점검내역 CER {d['remark_corpus_cer']}, "
                     f"정확도 {d['remark_field_accuracy']}, 행 자동 적재율 {d['auto_rate']}")
    _emit(a, data, "\n".join(lines))
    return 0


def _eval_meta(a, con, site) -> int:
    from .evaluate.meta import evaluate_meta

    r = evaluate_meta(con, split=a.split, site=site)
    lines = [f"쪽 메타 (분할 {a.split}) — 정답은 사람·파일명의 값 (기계가 채운 값은 정답이 아니다)"]
    for k, d in r["keys"].items():
        e = d["auto_error"]
        lines.append(f"  {k}: 정답이 있는 쪽 {d['n']} — 기계 값 정확도 {d['accuracy']}, 자동 적재율 {d['auto_rate']}, "
                     f"자동 적재 오류 {e['wrong']}/{e['auto']} (95% {e['ci95'][0]}–{e['ci95'][1]}), 목록에 없는 값 {d['unlisted']}, "
                     f"배차가 바뀐 쪽 {d['changed']['n']}에서 정확도 {d['changed']['accuracy']} · 정답 없이 읽은 쪽 {d['read_without_truth']}")
    s = r["slots"]
    lines.append(f"일보의 자리: 사람 값으로 정한 {s['pages']}쪽 중 기계 값만으로 같은 자리 {s['same']} ({s['rate']}), "
                 f"기계 값만으로 자리가 정해진 쪽 {s['machine_resolved']}")
    _emit(a, {"meta": r}, "\n".join(lines))
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
    if res["new_keys"]:
        lines.append(f"기준에 없던 새 항목 {len(res['new_keys'])}개 (어긋남으로 치지 않는다 — --update 로 기준에 넣는다): "
                     + ", ".join(res["new_keys"][:12]) + (" …" if len(res["new_keys"]) > 12 else ""))
    if res["updated"]:
        lines.append(f"기준을 저장했습니다: {res['baseline']}")
    _emit(a, {k: v for k, v in res.items() if k != "report"} | {"report": res["report"]}, "\n".join(lines))
    return 0 if (res["ok"] or res["updated"]) else 1


def cmd_template(a) -> int:
    from .tools.mktemplate import init_template

    if a.template_command == "check":
        from .tools.tpltools import check_notes, check_template

        errs = check_template(a.template_dir)
        notes = check_notes(a.template_dir)                    # 오류가 아닌 참고 (쓰이지 않는 인쇄 층) — 종료 코드에 세지 않는다
        _emit(a, {"template": a.template_dir, "problems": errs, "notes": notes},
              ("\n".join(f"- {e}" for e in errs) + f"\n오류 {len(errs)}개" if errs else "오류 없음")
              + "".join(f"\n참고: {n}" for n in notes))
        return 1 if errs else 0
    if a.template_command == "preview":
        from .forms.template import TemplateError
        from .tools.tpltools import preview

        s = _settings(a)
        out = Path(a.out) if a.out else s.work_root / "template-preview"
        try:
            r = preview(a.template_dir, out, scan=a.scan, page=a.page, dpi=s.dpi, print_layer=a.print_layer)
        except TemplateError as e:                             # 템플릿 오류만 — 오류 목록은 template check 로
            raise SystemExit(f"{e}\n(오류를 전부 보려면: minedocscan template check {a.template_dir})") from e
        except (ValueError, OSError) as e:                     # 저장소 안이라 거절, 없는 쪽 … — 안내 없이 한 줄
            raise SystemExit(str(e)) from e
        al = r["aligned"]
        _emit(a, r, f"그렸습니다: {r['out']} — 테두리 {r['boxes']}개 (칸 + 필드)"
              + ("" if al is None else f"\n정합: {'통과' if al['ok'] else '실패'}, 인라이어 {al['inliers']}, "
                 f"괘선 오차 {al['grid_err']} px" + (f", 쪽을 시계 방향으로 {al['rotation']}° 세워서" if al.get("rotation") else ""))
              + "\n저장소에 넣지 마세요 — 실제 양식의 이름·차량번호가 보입니다.")
        return 0
    if a.template_command == "variant":
        from .tools.variant import VariantError, format_summary, make_variant

        s = _settings(a)
        try:
            r = make_variant(a.template_dir, a.scan, a.page, a.name, out_dir=a.out_dir, dpi=s.dpi, damaged=s.damaged_pdf)
        except VariantError as e:                               # 거절은 한 줄
            raise SystemExit(str(e).splitlines()[0]) from e
        _emit(a, r, format_summary(r))
        return 0
    if a.template_command == "print-layer":
        import yaml

        from .tools.printlayer import PrintLayerError, build, format_summary

        s = _settings(a)
        try:
            r = build(a.template_dir, s, max_pages=a.max_pages, percentile=a.percentile, out=a.out,
                      allow_in_repo=a.allow_in_repo)
        except (PrintLayerError, OSError, NotImplementedError, yaml.YAMLError) as e:   # 거절은 한 줄
            raise SystemExit(str(e).splitlines()[0] if str(e) else type(e).__name__) from e
        _emit(a, r, format_summary(r))
        return 0
    if a.template_command == "add-region":
        from .tools.mktemplate import AddRegionError, add_region, format_summary

        try:
            r = add_region(a.template_dir, _roi(a.roi), a.name, role=a.role, header_rows=a.header_rows,
                           allow_in_repo=a.allow_in_repo)
        except (AddRegionError, OSError) as e:                  # 거절은 한 줄
            raise SystemExit(str(e).splitlines()[0] if str(e) else type(e).__name__) from e
        _emit(a, r, format_summary(r))
        return 0
    s = _settings(a)
    if s.site is None:
        raise SystemExit("사이트 팩이 지정되지 않았습니다: --site 또는 MINEDOCSCAN_SITE")
    roi = _roi(a.roi) if a.roi else None
    path = init_template(a.image, a.name, Path(s.site) / "templates", roi=roi, header_rows=a.header_rows,
                         page=a.page, dpi=s.dpi, handler=a.handler, overwrite=a.overwrite, rotate=a.rotate)
    _emit(a, {"template": str(path)}, f"템플릿 뼈대를 만들었습니다: {path}\n열 이름·kind·행 키를 채우세요 (docs/SITE_PACK.md).")
    return 0


def _roi(text: str) -> tuple[int, int, int, int]:
    try:
        roi = tuple(int(v) for v in text.split(","))
    except ValueError:
        roi = ()
    if len(roi) != 4:
        raise SystemExit("--roi 는 x0,y0,x1,y1 네 정수입니다")
    return roi


def cmd_synth(a) -> int:
    from .tools.synth import generate

    if a.mix_pages and not a.meta_fields:
        raise SystemExit("--mix-pages 는 --meta-fields 와 같이 씁니다")
    if a.print_layers and not (a.usage_logs or a.usage_only):
        raise SystemExit("--print-layers 는 --usage-logs 또는 --usage-only 와 같이 씁니다")
    if a.usage_variants and not (a.usage_logs or a.usage_only):
        raise SystemExit("--usage-variants 는 --usage-logs 또는 --usage-only 와 같이 씁니다")
    r = generate(a.out, days=a.days, seed=a.seed, low_cells=a.low_cells, meta_fields=a.meta_fields, mix_pages=a.mix_pages,
                 usage_logs=a.usage_logs, usage_only=a.usage_only, print_layers=a.print_layers,
                 usage_variants=a.usage_variants, rotate_pages=a.rotate_pages, blank_backs=a.blank_backs)
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
                        {"n": a.n, "seed": a.seed, "empty_share": a.empty_share, "template": a.template, "kind": a.kind,
                         "audit": a.audit})
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
                 f"✓ 검수: 점검표 행 {st['checks']['rows']}개 (체크 칸 {st['checks']['fields']}개)",
                 "값의 형식별: " + kv(st["by_format"]),
                 "대기열별(끝남/모집단, 기본 설정): " + (", ".join(f"{k} {v['done']}/{v['total']}" for k, v in st["by_queue"].items()
                                                         if v["total"]) or "-"),
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
    if a.review_command == "export-crops" and (a.meta or a.meta_key):
        from .review.export import ExportError, export_meta_crops

        if a.kind:
            raise SystemExit("--meta 와 --kind 는 같이 쓰지 않습니다 (메타 필드는 키로 고른다: --meta-key)")
        s, site, con, _imported = _review_db(a)
        try:
            r = export_meta_crops(con, site, s, a.out, split=a.split, meta_key=a.meta_key, res=a.res, out_scale=a.scale,
                                  pad=a.pad, allow_in_repo=a.allow_in_repo, include_illegible=a.include_illegible)
        except (ExportError, ValueError) as e:
            raise SystemExit(str(e)) from e
        _emit(a, r, f"메타 필드 크롭 {r['written']}개를 썼습니다: {r['out']} — 분할별 {r['by_split']}, 키별 {r['by_key']}, "
                    f"정답의 출처별 {r['by_label_source']}, 해상도별 {r['by_source']}, 읽을 수 없음 제외 {r['skipped_illegible']}개\n"
                    "저장소에 넣지 마세요 — 이름·차량번호가 들어 있습니다.")
        return 0
    if a.review_command == "export-crops":
        from .review.export import ExportError, export_crops

        s, site, con, _imported = _review_db(a)
        try:
            r = export_crops(con, site, s, a.out, split=a.split, kind=a.kind, res=a.res, out_scale=a.scale, pad=a.pad,
                             allow_in_repo=a.allow_in_repo, include_illegible=a.include_illegible)
        except (ExportError, ValueError) as e:
            raise SystemExit(str(e)) from e
        ill = "읽을 수 없음 포함" if a.include_illegible else f"읽을 수 없음 제외 {r['skipped_illegible']}개"
        _emit(a, r, f"크롭 {r['written']}개를 썼습니다: {r['out']} — 분할별 {r['by_split']}, 해상도별 {r['by_source']}, "
                    f"{ill}\n저장소에 넣지 마세요 — 현장의 글씨가 들어 있습니다.")
        return 0
    raise SystemExit(f"알 수 없는 review 명령: {a.review_command}")


_MODEL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _geometry(text: str | None) -> list[tuple[int, int]] | None:
    if not text:
        return None
    out = []
    for g in text.split(","):
        try:
            w, h = (int(v) for v in g.lower().split("x"))
        except ValueError:
            raise SystemExit(f"--synthetic-geometry 는 WxH[,WxH…] (예: 92x21,112x22): {text}") from None
        if not (8 <= w <= 2000 and 8 <= h <= 1000):
            raise SystemExit(f"--synthetic-geometry 의 칸 크기가 범위 밖입니다: {g}")
        out.append((w, h))
    return out


def cmd_recognizer(a) -> int:
    from .recognize.digits.model import list_models, models_dir

    s = _settings(a)
    if a.recognizer_command == "list":
        site = _need_site(s)
        models = list_models(site.root)
        lines = [f"모델 ({models_dir(site.root)}): {len(models)}개"]
        for m in models:
            if m.get("reader"):                              # 메타 필드 모델
                lines.append(f"  {m['name']:<20} {m['created_at'] or '-':<21} {m['spec']:<34} 기준 {m['threshold']}  "
                             f"메타 {', '.join(m['keys'])} ({m['reader']}) · 학습 {m['train_cells']}쪽/{m['train_dates']}일 "
                             f"+ 합성 {m['synthetic_cells']} · 읽기({m['val_source']}) {m['val_cells']}번 정확도 {m['val_value_acc']}")
                continue
            lines.append(f"  {m['name']:<20} {m['created_at'] or '-':<21} {m['spec']:<34} 기준 {m['threshold']}  "
                         f"학습 {m['train_cells']}셀/{m['train_dates']}일 + 합성 {m['synthetic_cells']} · "
                         f"검증({m['val_source']}) {m['val_cells']}셀 값 {m['val_value_acc']} 빈 칸 {m['val_empty_acc']}"
                         + (f"  [{m['error']}]" if m.get("error") else ""))
        _emit(a, {"models": models}, "\n".join(lines))
        return 0
    if a.recognizer_command == "train" and (a.meta_key or a.synthetic_meta):
        return _recognizer_train_meta(a, s)
    if a.recognizer_command == "train":
        from .recognize.digits.train import TrainArgs, TrainError, train

        if not _MODEL_NAME.match(a.name):
            raise SystemExit(f"--name 은 영문·숫자·.-_ (64자 이하): {a.name}")
        if a.cv or a.reader or a.extra_digits:
            raise SystemExit("--cv, --reader, --extra-digits 는 메타 필드 모델(--meta-key)에서만 씁니다")
        site = _need_site(s) if (s.site or not a.out) else None
        out = Path(a.out) if a.out else models_dir(site.root) / a.name
        args = TrainArgs(name=a.name, steps=a.steps or 2500, synthetic=a.synthetic, seed=a.seed, val_share=a.val_share,
                         target_auto_error=0.01 if a.target_auto_error is None else a.target_auto_error,
                         min_val_auto=a.min_val_auto, geometry=_geometry(a.synthetic_geometry))
        trips_max = site.option("haul", "trips_max") if site else None
        try:
            card = train(a.crops, out, args, split_salt=site.split_salt if site else "synthetic",
                         trips_max=None if trips_max is None else int(trips_max), allow_in_repo=a.allow_in_repo)
        except TrainError as e:
            raise SystemExit(str(e)) from e
        v = card["validation"]
        aa = card["auto_accept"]
        thr = (f"{aa['threshold']} (검증에서 자동 적재 {aa['auto']}칸 중 오류 {aa['errors']}, 오류율 95 % 상한 {aa['upper95']:.1%})"
               if aa["met"] else f"없음 ({aa.get('reason') or '목표를 만족하는 임계값이 없다'} — 자동 적재하지 않는다)")
        _emit(a, {"model": str(out), "card": card},
              f"모델을 만들었습니다: {out}\n"
              f"  검증({v['source']}) {v['cells']}셀: 값 있는 칸 {v['score']['value']['accuracy']}, "
              f"빈 칸 {v['score']['empty']['accuracy']} · 온도 {card['temperature']} · 자동 적재 기준 {thr}\n"
              f"  설정: [recognize.by_kind] handwritten_number = \"digits\",  [recognize.digits] model = \"{a.name}\"")
        return 0
    if a.recognizer_command == "eval":
        return _recognizer_eval(a, s)
    raise SystemExit(f"알 수 없는 recognizer 명령: {a.recognizer_command}")


def _recognizer_train_meta(a, s: Settings) -> int:
    """메타 필드 모델 (tasks/0004 단계 3·4). 카드·출력에 값(이름·차량번호)을 적지 않는다."""
    import tempfile

    from .recognize.digits.model import models_dir
    from .recognize.digits.train import TrainError
    from .recognize.meta.model import template_values
    from .recognize.meta.train import MetaTrainArgs, train_meta

    if not _MODEL_NAME.match(a.name):
        raise SystemExit(f"--name 은 영문·숫자·.-_ (64자 이하): {a.name}")
    if not a.meta_key:
        raise SystemExit("--synthetic-meta 에는 --meta-key 가 필요합니다")
    if a.crops and a.synthetic_meta:
        raise SystemExit("--crops 와 --synthetic-meta 는 같이 쓰지 않습니다")
    if not a.crops and not a.synthetic_meta:
        raise SystemExit("메타 필드 모델은 --crops (review export-crops --meta 의 폴더) 또는 --synthetic-meta DAYS 로 학습합니다")
    keys = tuple(k.strip() for k in a.meta_key.split(",") if k.strip())
    site = _need_site(s) if (s.site or not a.out) else None
    out = Path(a.out) if a.out else models_dir(site.root) / a.name
    args = MetaTrainArgs(name=a.name, steps=a.steps,                     # None: 읽는 법마다 기본값 (train_meta)
                         synthetic=a.synthetic, seed=a.seed, val_share=a.val_share, min_val_auto=a.min_val_auto,
                         target_auto_error=0.02 if a.target_auto_error is None else a.target_auto_error, keys=keys,
                         reader=a.reader, cv=a.cv, min_examples=a.min_examples, extra_digits=a.extra_digits,
                         template_values={k: template_values(site, k) for k in keys} if site else {})
    try:
        with tempfile.TemporaryDirectory(prefix="minedocscan-synth-meta-") as tmp:
            crops = a.crops
            if a.synthetic_meta:
                from .tools.synth_meta import write_meta_crops

                try:
                    write_meta_crops(tmp, keys, a.synthetic_meta, seed=a.seed)
                except ValueError as e:                         # 합성 값이 없는 키
                    raise SystemExit(f"--synthetic-meta: {e}") from e
                crops = tmp
            card = train_meta(crops, out, args, split_salt=site.split_salt if site else "synthetic",
                              allow_in_repo=a.allow_in_repo)
    except TrainError as e:
        raise SystemExit(str(e)) from e
    aa, v, m = card["auto_accept"], card["validation"], card["meta"]
    thr = (f"{aa['threshold']} (읽기에서 자동 적재 {aa['auto']} 중 오류 {aa['errors']}, 오류율 95 % 상한 {aa['upper95']:.1%})"
           if aa["met"] else f"없음 ({aa.get('reason')} — 자동 적재하지 않는다)")
    sc = v["score"]
    _emit(a, {"model": str(out), "card": card},
          f"모델을 만들었습니다: {out} ({m['reader']}, 키 {', '.join(m['keys'])})\n"
          f"  읽기({v['source']}) {v['reads']}번: 정확도 {sc['accuracy']}, 목록에 있던 값 {sc['listed']['accuracy']}, "
          f"목록에 없는 값으로 답함 {sc['unlisted_answers']} · 온도 {card['temperature']} · 자동 적재 기준 {thr}\n"
          "  설정: [recognize.meta] " + ", ".join(f'"{k}" = "{a.name}"' for k in m["keys"]))
    return 0


def _recognizer_eval(a, s: Settings) -> int:
    from .recognize.digits.evaluate import EvalError, evaluate
    from .recognize.digits.model import resolve_model
    from .recognize.meta.model import is_meta_card

    site = _need_site(s) if s.site and Path(s.site).is_dir() else None
    try:
        model_dir = resolve_model(a.model, site.root if site else None)
    except FileNotFoundError as e:
        raise SystemExit(str(e)) from e
    if is_meta_card(model_dir):
        return _recognizer_eval_meta(a, s, site, model_dir)
    opts = (s.recognizer_options or {}).get("digits", {})
    thr = opts.get("auto_accept_conf")                       # 설정이 카드의 기준보다 먼저 (4.6)
    errors = None if a.errors is None else (Path(a.errors) if a.errors else Path(s.work_root) / "recognizer-errors")
    trips_max = site.option("haul", "trips_max") if site else None
    try:
        r = evaluate(a.crops, model_dir, split=a.split, threshold=None if thr is None else float(thr), errors=errors,
                     trips_max=None if trips_max is None else int(trips_max))
    except (EvalError, ValueError) as e:
        raise SystemExit(str(e)) from e
    r.pop("predictions")                                      # 칸마다의 답은 내놓지 않는다 (4.7)
    acc = r["accuracy"]
    at = r["at_threshold"]
    lines = [f"모델 {r['model']} · 분할 {r['split']} · {r['cells']}셀 ({r['dates']}일) · 규격 {r['spec']}",
             f"정확도: 전체 {acc['all']['accuracy']} ({acc['all']['correct']}/{acc['all']['n']}), "
             f"값 있는 칸 {acc['value']['accuracy']} ({acc['value']['correct']}/{acc['value']['n']}), "
             f"빈 칸 {acc['empty']['accuracy']} ({acc['empty']['correct']}/{acc['empty']['n']}), "
             f"읽을 수 없음 {acc['illegible']['accuracy']} ({acc['illegible']['correct']}/{acc['illegible']['n']})",
             "값별: " + ", ".join(f"{v['value'] or '빈칸'} {v['correct']}/{v['n']}" for v in r["by_value"]),
             "많이 틀린 쌍(정답→읽은 값): " + (", ".join(f"{c['truth'] or '빈칸'}→{c['read'] or '빈칸'} {c['n']}"
                                                   for c in r["confusions"]) or "-"),
             "신뢰도 구간별 정확도: " + ", ".join(f"[{b['bin'][0]},{b['bin'][1]}) {b['n']}셀 평균 {b['mean_conf']} 정확 {b['accuracy']}"
                                        for b in r["calibration"] if b["n"]),
             "임계값별 자동 적재 (적재율 · 오류 분자/분모 · 95% 구간):"]
    lines += [f"  {t['threshold']:<6} {t['auto_rate']:<7} {t['errors']}/{t['auto']} {t['error_ci95']}" for t in r["thresholds"]]
    lines.append(f"모델의 기준 {r['threshold'] if r['threshold'] is not None else '없음(자동 적재 없음)'} ({r['threshold_source']})"
                 + ("" if at is None else f": 자동 적재 {at['auto']}/{r['cells']} = {at['auto_rate']}, "
                                          f"오류 {at['errors']}/{at['auto']} {at['error_ci95']}")
                 + f" · 읽을 수 없음 칸 중 자동 적재될 것 {r['illegible']['auto']}/{r['illegible']['n']}")
    if r["errors_image"]:
        lines.append(f"틀린 칸 {r['errors']}개 모아 보기: {r['errors_image']} (현장 글씨 — 저장소·문서에 넣지 말 것)")
    _emit(a, r, "\n".join(lines))
    return 0


def _recognizer_eval_meta(a, s: Settings, site, model_dir: Path) -> int:
    from .recognize.digits.evaluate import EvalError
    from .recognize.meta.evaluate import evaluate_meta
    from .recognize.meta.model import MetaModelError

    errors = None if a.errors is None else (Path(a.errors) if a.errors else Path(s.work_root) / "recognizer-errors")
    try:
        r = evaluate_meta(a.crops, model_dir, split=a.split, site=site, errors=errors)
    except (EvalError, MetaModelError, ValueError) as e:
        raise SystemExit(str(e)) from e
    r.pop("predictions")                                      # 쪽마다의 답(값)은 내놓지 않는다 (4.7)
    sc, at = r["score"], r["at_threshold"]
    lines = [f"모델 {r['model']} ({r['reader']}, 키 {', '.join(r['keys'])}) · 분할 {r['split']} · {r['cells']}쪽 ({r['dates']}일) · "
             f"규격 {r['spec']} · 후보 {r['candidates']}" + (f"\n읽기: {r['source']}" if r.get("source") else ""),
             f"정확도 {sc['accuracy']} ({sc['correct']}/{sc['n']}), 정답이 목록에 있던 쪽 {sc['listed']['accuracy']} "
             f"({sc['listed']['correct']}/{sc['listed']['n']}), 목록에 없는 값으로 답함 {sc['unlisted_answers']}, 거절 {sc['rejects']}",
             f"정답이 목록 밖인 쪽 {sc['truth_unlisted']['n']}: 목록에 없는 값으로 답함 {sc['truth_unlisted']['answered_unlisted']}, "
             f"목록의 값으로 답함 {sc['truth_unlisted']['answered_value']}",
             "신뢰도 구간별 정확도: " + ", ".join(f"[{b['bin'][0]},{b['bin'][1]}) {b['n']}쪽 평균 {b['mean_conf']} 정확 {b['accuracy']}"
                                        for b in r["calibration"] if b["n"]),
             "임계값별 자동 적재 (적재율 · 오류 분자/분모 · 95% 구간):"]
    lines += [f"  {t['threshold']:<6} {t['auto_rate']:<7} {t['errors']}/{t['auto']} {t['error_ci95']}" for t in r["thresholds"]]
    lines.append(f"모델의 기준 {r['threshold'] if r['threshold'] is not None else '없음(자동 적재 없음)'}"
                 + ("" if at is None else f": 자동 적재 {at['auto']}/{r['cells']} = {at['auto_rate']}, "
                                          f"오류 {at['errors']}/{at['auto']} {at['error_ci95']}"))
    if r["errors_image"]:
        lines.append(f"틀린 쪽 {r['errors']}개 모아 보기: {r['errors_image']} (이름·차량번호 글씨 — 저장소·문서에 넣지 말 것)")
    _emit(a, r, "\n".join(lines))
    return 0


COMMANDS = {"info": cmd_info, "run": cmd_run, "report": cmd_report, "pages": cmd_pages, "eval": cmd_eval,
            "regress": cmd_regress, "template": cmd_template, "synth": cmd_synth, "review": cmd_review,
            "recognizer": cmd_recognizer}


def main(argv: list[str] | None = None) -> int:
    from .config import ConfigError
    from .forms.template import TemplateError
    from .store.db import SchemaVersionError

    args = build_parser().parse_args(argv)
    try:
        return COMMANDS[args.command](args)
    except (SchemaVersionError, ConfigError, TemplateError) as e:   # 안내문만 보이면 된다. 트레이스백은 필요 없다
        raise SystemExit(f"{_ERROR_TITLE.get(type(e).__name__, '오류')}: {e}") from e


_ERROR_TITLE = {"ConfigError": "설정 오류", "TemplateError": "템플릿 오류", "SchemaVersionError": "DB 오류"}


if __name__ == "__main__":
    raise SystemExit(main())

"""공용 픽스처. 합성 양식(개인정보 없음)을 한 번 만들고 null / oracle 두 백엔드로 한 번씩 돌려 둔다."""
from __future__ import annotations

from pathlib import Path

import pytest

from minedocscan.config import Settings
from minedocscan.forms.sitepack import SitePack
from minedocscan.pipeline import Pipeline
from minedocscan.recognize import OracleRecognizer, load_answers_json
from minedocscan.review.store import Review, export_answers, save
from minedocscan.tools.synth import T_INSP, T_LOG, T_MATRIX, generate


@pytest.fixture(scope="session")
def synth(tmp_path_factory):
    """3일치 합성 데이터: 날마다 다른 어려움이 들어 있다 (tools/synth.py)."""
    return generate(tmp_path_factory.mktemp("synth"), days=3, seed=0)


@pytest.fixture(scope="session")
def site(synth) -> SitePack:
    return SitePack(synth.site)


def run_pipeline(synth, work_root: Path, recognizer=None) -> Pipeline:
    settings = Settings(site=synth.site, archive_root=synth.scans, work_root=work_root)
    pipe = Pipeline(settings, recognizer=recognizer)
    pipe.run([synth.scans])
    return pipe


def run_day(root: Path, seed: int = 0, recognizer=None) -> tuple:
    """검수 테스트용 하루치: 합성 → null 실행. 검수 파일은 사이트 팩이 아니라 root 아래에 둔다 (세션 픽스처를 더럽히지 않게).
    돌려주는 값: (synth, settings, pipe)."""
    synth = generate(root / "data", days=1, seed=seed)
    settings = Settings(site=synth.site, archive_root=synth.scans, work_root=root / "work",
                        reviews=root / "검수" / "reviews.jsonl")
    pipe = Pipeline(settings, recognizer=recognizer)
    pipe.run([synth.scans])
    return synth, settings, pipe


@pytest.fixture(scope="session")
def null_run(synth, tmp_path_factory) -> Pipeline:
    """인식기 없이 돌린 결과 — 분류·정합·셀 추출·체크 판정·값 유무 교차검증까지."""
    return run_pipeline(synth, tmp_path_factory.mktemp("work_null"))


@pytest.fixture(scope="session")
def oracle_run(synth, tmp_path_factory) -> Pipeline:
    """정답을 돌려주는 인식기로 돌린 결과 — 인식기를 뺀 나머지가 맞으면 오류가 0 이어야 한다."""
    answers = load_answers_json(synth.answers_path)
    return run_pipeline(synth, tmp_path_factory.mktemp("work_oracle"), OracleRecognizer(answers))


def review_everything(con, site, settings, answers) -> int:
    """정답이 있는 세 표(점검내역, 일보 운반, 행렬 운반)의 수기 셀 전부를 정답대로 검수한다."""
    rows = con.execute(
        "SELECT f.field_id, d.source_name || '#' || p.page_no AS source, p.work_date, p.template_name, f.region, "
        "f.field_name, f.row_key FROM doc_field f JOIN doc_page p ON f.page_id = p.page_id "
        "JOIN doc_document d ON p.document_id = d.document_id WHERE f.kind LIKE 'handwritten%' "
        "AND ((p.template_name = ? AND f.region = 'main') OR (p.template_name = ? AND f.region = 'haul') "
        "OR (p.template_name = ? AND f.region = 'matrix'))", (T_INSP, T_LOG, T_MATRIX)).fetchall()
    for r in rows:
        tail = (r["template_name"], r["region"], r["field_name"], r["row_key"])
        text = answers.get((r["source"], *tail)) or answers.get((r["work_date"], *tail))
        rv = Review(r["field_id"], "value", text, "jp") if text else Review(r["field_id"], "empty", reviewer="jp")
        save(con, site, settings, rv)
    return len(rows)


@pytest.fixture(scope="session")
def reviewed_day(tmp_path_factory):
    """하루치를 null 로 돌리고 정답이 있는 표의 셀 전부를 정답대로 검수해 둔 상태 (+ answers 로 내보낸 파일)."""
    root = tmp_path_factory.mktemp("reviewed_day")
    synth, settings, pipe = run_day(root, seed=4)
    answers = load_answers_json(synth.answers_path)
    n = review_everything(pipe.con, pipe.site, settings, answers)
    out = root / "exported.json"
    assert export_answers(pipe.con, out) == n
    return {"synth": synth, "settings": settings, "null": pipe, "answers": answers, "exported": out, "root": root,
            "n_reviewed": n}


# ── 낮은 칸 합성 양식과 숫자 인식기 (tasks/0003 단계 3·5·6) ─────────────────────
FIXTURE_MODEL = Path(__file__).resolve().parent / "fixtures" / "digits-fixture"


@pytest.fixture(scope="session")
def low_synth(tmp_path_factory):
    """낮은 칸·거친 숫자·X 표의 합성 3일치 (한 번만 만든다). 날짜별 PDF 는 low_synth.scans 아래."""
    return generate(tmp_path_factory.mktemp("low_synth"), days=3, seed=0, low_cells=True)


def day_pdf(synth, i: int) -> Path:
    return sorted(synth.scans.glob("*.pdf"))[i]


def answers_of(answers: dict, pdfs: list[Path], synth) -> dict:
    """그 PDF 들의 정답만 (출처가 "<파일명>#<쪽>" 이거나 그 파일의 날짜인 것)."""
    stems = {p.stem for p in pdfs}
    dates = {d["date"] for d in synth.truth["days"] if any(d["date"] in st for st in stems)}
    return {k: v for k, v in answers.items() if k[0].split("#")[0] in stems or k[0] in dates}


@pytest.fixture(scope="session")
def digits_low3(low_synth, tmp_path_factory):
    """low_synth 3일치를 digits(시험용 모델) + null 로 돌린 것. 이 DB 에 쓰지 않는다 — 쓸 시험은 복사본(clone_db)에서."""
    root = tmp_path_factory.mktemp("digits_low3")
    pipe = Pipeline(digits_settings(low_synth, root / "work", reviews=root / "reviews.jsonl"))
    pipe.run([low_synth.scans])
    return {"root": root, "synth": low_synth, "answers": load_answers_json(low_synth.answers_path), "pipe": pipe}


def clone_db(con):
    """세션 픽스처의 DB 를 메모리로 복사한다 (검수를 넣어 볼 때)."""
    import sqlite3

    out = sqlite3.connect(":memory:", check_same_thread=False)          # 검수 서버 시험은 다른 스레드에서 쓴다
    con.backup(out)
    out.row_factory = sqlite3.Row
    return out


def digits_settings(synth, work_root: Path, model_dir: Path = FIXTURE_MODEL, reviews: Path | None = None, **kw) -> Settings:
    """숫자 칸은 digits(모델 폴더 경로로), 나머지는 null. 정합 이미지는 저장하지 않는다 (숫자 칸의 크롭은 원본에서 뜬다 —
    쪽마다 PNG 저장이 약 60 ms 라 시험 시간을 아낀다). 저장이 필요하면 save_aligned=True."""
    kw.setdefault("save_aligned", False)
    return Settings(site=synth.site, archive_root=synth.scans, work_root=work_root, reviews=reviews,
                    recognizer="null", recognizer_by_kind={"handwritten_number": "digits"},
                    recognizer_options={"digits": {"model": str(model_dir)}}, **kw)


def number_cells(con, answers) -> list[dict]:
    """운반 숫자 셀(일보 haul, 행렬 matrix)마다 기계 상태와 정답("" = 빈 칸 — X 표 칸은 정답에 없다)."""
    rows = con.execute(
        "SELECT f.*, d.source_name || '#' || p.page_no AS source, p.template_name, p.work_date FROM doc_field f "
        "JOIN doc_page p ON f.page_id = p.page_id JOIN doc_document d ON p.document_id = d.document_id "
        "WHERE f.kind = 'handwritten_number' AND f.region IN ('haul', 'matrix') ORDER BY f.field_id").fetchall()
    out = []
    for r in rows:
        tail = (r["template_name"], r["region"], r["field_name"], r["row_key"])
        truth = answers.get((r["source"], *tail)) or answers.get((r["work_date"], *tail)) or ""
        out.append(dict(r) | {"truth": truth})
    return out


def digits_metrics(cells: list[dict]) -> dict:
    """값 있는 칸의 정확도(기계 값), 자동 적재된 칸 중 틀린 것, 잉크로는 값 있음인데 정답이 빈 칸인 칸의 처리."""
    values = [c for c in cells if c["truth"]]
    auto = [c for c in cells if c["review_status"] == "auto" and c["backend"] == "digits"]
    wrong = [c for c in auto if (c["value_raw"] if c["has_value_raw"] else "") != c["truth"]]
    inked_empty = [c for c in cells if not c["truth"] and c["backend"] == "digits"]
    return {"values": len(values), "value_correct": sum(c["value_raw"] == c["truth"] for c in values),
            "auto": len(auto), "auto_wrong": len(wrong),
            "inked_empty": len(inked_empty),
            "inked_empty_auto": sum(c["has_value_raw"] == 0 and c["review_status"] == "auto" for c in inked_empty)}


# ── 메타 필드 합성 (tasks/0004) ─────────────────────────────────────────────
@pytest.fixture(scope="session")
def meta_synth(tmp_path_factory):
    """메타 필드가 사람마다 다른 획인 합성 4일치 (네 자리 차량번호, 월·일 필드, 차를 바꿔 탄 날·새 차·새 사람)."""
    return generate(tmp_path_factory.mktemp("meta_synth"), days=4, seed=3, meta_fields=True)


@pytest.fixture(scope="session")
def meta_null(meta_synth, tmp_path_factory):
    """meta_synth 를 라벨 그대로, 인식기 없이 돌린 것. 이 DB 에 쓰지 않는다 (쓸 시험은 clone_db 로)."""
    root = tmp_path_factory.mktemp("meta_null")
    settings = Settings(site=meta_synth.site, archive_root=meta_synth.scans, work_root=root / "work",
                        reviews=root / "reviews.jsonl")
    pipe = Pipeline(settings)
    pipe.run([meta_synth.scans])
    return {"root": root, "settings": settings, "pipe": pipe}


META_FIXTURES = Path(__file__).resolve().parent / "fixtures"


def meta_options(digits: Path = META_FIXTURES / "meta-digits", operator: Path = META_FIXTURES / "meta-operator") -> dict:
    """메타 필드 모델 두 개(기본: 시험용)를 네 키에 (설정 [recognize.meta] 와 같은 모양)."""
    d, o = str(digits), str(operator)
    return {"meta": {"vehicle_no": d, "date.month": d, "date.day": d, "operator": o}}


def meta_run(synth, root: Path, labels: dict | None = None, options: dict | None = None, inputs: list | None = None,
             **kw) -> dict:
    """사이트 팩을 복사해(라벨을 labels 로 바꿔) 메타 필드 모델로 돌린다. 정합 이미지는 저장하지 않는다. inputs: 돌릴 파일 (기본 전부)."""
    import json
    import shutil

    site = root / "site"
    shutil.copytree(synth.site, site)
    if labels is not None:
        (site / "labels" / "pages.json").write_text(json.dumps(labels, ensure_ascii=False), encoding="utf-8")
    kw.setdefault("save_aligned", False)
    settings = Settings(site=site, archive_root=synth.scans, work_root=root / "work", reviews=root / "reviews.jsonl",
                        recognizer_options=options or meta_options(), **kw)
    pipe = Pipeline(settings)
    pipe.run(inputs or [synth.scans])
    return {"root": root, "settings": settings, "pipe": pipe}


@pytest.fixture(scope="session")
def meta_truth(meta_synth) -> dict:
    import json

    return json.loads((meta_synth.site / "labels" / "pages.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def meta_nolabels(meta_synth, tmp_path_factory) -> dict:
    """meta_synth 를 라벨 없이 메타 필드 모델로 돌린 것. 이 DB 에 쓰지 않는다."""
    return meta_run(meta_synth, tmp_path_factory.mktemp("meta_nolabels"), labels={})


WRONG_LABELS = 2          # meta_mislabeled: 일보 쪽 앞의 둘은 차량번호 라벨이 틀렸다 ("4999")


@pytest.fixture(scope="session")
def meta_mislabeled(meta_synth, meta_truth, tmp_path_factory) -> dict:
    """meta_synth 의 첫날을 라벨과 함께(앞의 두 쪽은 차량번호 라벨을 일부러 틀리게) 메타 필드 모델로 돌린 것 — 기계 값은 대조에만.
    이 DB 에 쓰지 않는다 (쓸 시험은 clone_db 로). 돌려주는 값에 wrong: 라벨이 틀린 쪽의 "<파일명>#<쪽>"."""
    import json

    labels = json.loads(json.dumps(meta_truth))
    wrong = sorted(k for k in labels if "#" in k)[:WRONG_LABELS]
    for src in wrong:
        labels[src]["vehicle_no"] = "4999"
    first = sorted(meta_synth.scans.glob("*.pdf"))[0]                     # 첫날만 (틀린 라벨 둘이 그날의 쪽이다)
    assert all(src.startswith(first.stem + "#") for src in wrong)
    return meta_run(meta_synth, tmp_path_factory.mktemp("meta_mislabeled"), labels=labels, inputs=[first]) | {"wrong": wrong}


# ── 장비 가동 일보 (tasks/0005) ─────────────────────────────────────────────
@pytest.fixture(scope="session")
def usage_synth(tmp_path_factory):
    """합성 가동 일보 두 종만의 3일치 (점검표·운반 쪽 없이 — tools/synth_usage.py). 운행일보는 같은 날 섞여 쓰이는 두 판이다
    (usage_variants — 판 A synth_usage_log·판 B synth_usage_log_b, 같은 family·concurrent: true, tasks/0006 단계 4): 날마다 두 판이
    섞이고 정답의 template 은 그 쪽의 판. 모든 양식(판마다)에 인쇄 층이 켜져 있다 (print_layers — 합성 쪽에서 추정한 print.png +
    print_image, 단계 3). 인쇄 층을 끈 결과는 같은 쪽을 다시 펴서 함수 단위로 잰다 (test_print_presence.py) — 파이프라인을 한 번 더
    돌리지 않는다. 판 A 만 있을 때의 실패도 함수 단위로 (test_variants.py)."""
    return generate(tmp_path_factory.mktemp("usage_synth"), days=3, seed=0, usage_only=True, print_layers=True,
                    usage_variants=True)


@pytest.fixture(scope="session")
def usage_run(usage_synth, tmp_path_factory) -> dict:
    """usage_synth 를 정답 인식기(oracle)로 돌린 것 (인쇄 층을 켜고, 운행일보는 판마다 정합해 고른다): 정수 칸(작업량)·글자 칸은
    읽고, 소수·시각 칸(계기·근무 시각)은 읽지 않는다 (잉크가 있으면 검수 대기). oracle 에 사이트 팩을 주지 않는다 — 정답은 그 쪽의
    판 이름으로 찾으므로 판을 잘못 고르면 값이 빠진다. 이 DB 에 쓰지 않는다 (쓸 시험은 clone_db 와 다른 검수 파일로).
    align_calls: 양식(판)마다 정합(runner.align_to_template)을 부른 횟수 — 실행을 더 하지 않고 정합 횟수를 센다.
    classified: 이 실행의 분류 결과 (쪽 그림·후보·묶음 → ClassResult) — 같은 쪽을 다시 돌리는 시험이 분류를 되풀이하지 않게
    (replay_classify)."""
    from collections import Counter

    import minedocscan.pipeline.runner as runner

    root = tmp_path_factory.mktemp("usage_run")
    answers = load_answers_json(usage_synth.answers_path)
    settings = Settings(site=usage_synth.site, archive_root=usage_synth.scans, work_root=root / "work",
                        reviews=root / "reviews.jsonl", save_aligned=False)
    pipe = Pipeline(settings, recognizer=OracleRecognizer(answers))
    refs: list = []                                      # 정합한 기준 그림 (판이 하나뿐인 양식은 쪽마다 한 번 — tasks/0006 4.6)
    align = runner.align_to_template

    def counted(gray, ref_gray, *a, **kw):
        refs.append(ref_gray)
        return align(gray, ref_gray, *a, **kw)

    classified: dict = {}
    classify = pipe.classifier.classify

    def recorded(gray, *a, **kw):
        r = classified[_classify_key(gray, *a, **kw)] = classify(gray, *a, **kw)
        return r

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(runner, "align_to_template", counted)
        mp.setattr(pipe.classifier, "classify", recorded)
        pipe.run([usage_synth.scans])
    names = {id(t.reference): n for n, t in pipe.site.templates.items()}
    return {"root": root, "settings": settings, "pipe": pipe, "answers": answers,
            "align_calls": Counter(names[id(r)] for r in refs), "classified": classified}


def _classify_key(gray, min_inliers: int = 60, candidates=None, groups=None) -> tuple:
    import hashlib

    return (hashlib.sha256(gray.tobytes()).hexdigest(), gray.shape, min_inliers, tuple(candidates or ()), repr(groups))


def replay_classify(pipe, usage_run, monkeypatch) -> None:
    """pipe 의 분류를 usage_run 이 같은 쪽·같은 후보·같은 묶음으로 낸 결과로 바꾼다 (시간 — 분류가 쪽마다 정합보다 비싸다).
    분류는 검수와 상관없고 같은 입력이면 같은 결과다 (결정적). 기록에 없는 입력이면 시험이 실패한다 (조용히 다시 분류하지 않는다).
    다시 돌린 쪽의 정합·칸·핸들러·검수는 그대로 새로 한다 — 불변식(검수 파일 → 같은 DB)이 보는 것은 그쪽이다."""
    seen = usage_run["classified"]

    def replay(gray, *a, **kw):
        key = _classify_key(gray, *a, **kw)
        assert key in seen, "usage_run 이 분류하지 않은 쪽·후보"
        return seen[key]

    monkeypatch.setattr(pipe.classifier, "classify", replay)


def usage_fields(con) -> list:
    """가동 일보 쪽의 손으로 쓰는 칸·필드 전부: (field_id, 정답 키)."""
    from minedocscan.tools.synth_usage import T_LOADER, USAGE_LOGS

    return con.execute(
        "SELECT f.field_id, d.source_name || '#' || p.page_no AS source, p.template_name, f.region, f.field_name, f.row_key "
        "FROM doc_field f JOIN doc_page p ON f.page_id = p.page_id JOIN doc_document d ON p.document_id = d.document_id "
        "WHERE p.template_name IN (?, ?, ?) AND f.kind LIKE 'handwritten%' ORDER BY f.field_id", (*USAGE_LOGS, T_LOADER)).fetchall()


def review_usage(con, site, settings, answers, regions=None, skip=()) -> int:
    """가동 일보의 칸·필드를 정답대로 검수한다 (값이 있으면 value, 없으면 empty). regions: 이 표(와 "fields")만.
    skip: 건너뛸 field_id (이미 다른 길 — 예: readings 대기열 — 로 검수한 칸)."""
    n = 0
    for f in usage_fields(con):
        if (regions is not None and f["region"] not in regions) or f["field_id"] in skip:
            continue
        text = answers.get((f["source"], f["template_name"], f["region"], f["field_name"], f["row_key"] or ""))
        rv = Review(f["field_id"], "value", text, "jp") if text else Review(f["field_id"], "empty", reviewer="jp")
        save(con, site, settings, rv)
        n += 1
    return n


# ── 인쇄 층 (tasks/0006) ───────────────────────────────────────────────────
@pytest.fixture(scope="session")
def synth10(tmp_path_factory):
    """합성 가동 일보 10일치(seed 0, 판 B 없이 — 운행일보는 판 A 하나)와 그 쪽들로 추정한 인쇄 층 (synth --print-layers: 스캔한
    쪽을 스캔 효과의 기하 행렬로 템플릿 좌표에 되돌려 imaging/printlayer.estimate — template print-layer 와 같은 방법·차례·기본 설정
    (최대 40장, 백분위 75), 정합(ORB)과 파이프라인 없이). 단계 2 의 수용 기준 1 을 재는 층이고(test_printlayer.py — usage_synth 의
    3일치는 운행일보의 판마다 4–5쪽이라 같은 자리에 쓴 계기 값의 잔상이 남는다), 첫 이틀 묶음은 분류 전용 실행(usage_classify_only)의
    입력이다."""
    return generate(tmp_path_factory.mktemp("synth10"), days=10, seed=0, usage_only=True, print_layers=True)


@pytest.fixture(scope="session")
def usage_classify_only(synth10, tmp_path_factory) -> dict:
    """분류 전용 템플릿의 인쇄 층 (tasks/0006 4.2 — print-layer 가 쪽을 직접 정합하는 경로): synth10 의 첫 이틀 묶음(로우더 4쪽 —
    print-layer 는 3장 미만을 거절한다)을 두 양식의 칸 정의를 지운 사이트 팩으로 분류만 하고(classified_only — 호모그래피·정합 그림 없음),
    `template print-layer` 의 기본 설정으로 층을 만든다. 판이 섞이지 않은 쪽이라 층에 그 양식의 괘선이 다 남는다 — 판 A·B 가
    반씩 섞인 쪽으로 75 백분위 층을 만들면 두 판의 표 괘선이 다 빠진다 (4.2). 그래서 usage_synth(판이 섞였다)가 아니라 synth10 이다.
    운반 양식은 뺀다 (점검표는 장비 마스터라 둔다). 인쇄 층은 칸 정의가 있는 원래 템플릿 폴더(인쇄 층 없이)의 복사본에 쓴다.
    DB 는 print-layer 가 쓰지 않는다 — db_sha 는 만들기 전의 해시. 단계 6(add-region)도 이 층에서 괘선을 잡을 수 있다
    (test_printlayer.py 가 생성기의 괘선이 층에 남았는지 본다).
    {"settings", "db", "db_sha", "pdfs", "layers": {양식: {"dir", "summary"}}}"""
    import hashlib
    import shutil

    import yaml

    from minedocscan.tools.printlayer import build
    from minedocscan.tools.synth_usage import T_LOADER, T_USAGE

    root = tmp_path_factory.mktemp("usage_classify_only")
    site = root / "site"
    shutil.copytree(synth10.site, site, ignore=shutil.ignore_patterns("print.png"))
    for name in (T_LOG, T_MATRIX):
        shutil.rmtree(site / "templates" / name)

    for name in (T_USAGE, T_LOADER):
        shutil.copytree(site / "templates" / name, root / "templates" / name)
        for p, strip in ((root / "templates" / name / "template.yaml", False), (site / "templates" / name / "template.yaml", True)):
            spec = {k: v for k, v in yaml.safe_load(p.read_text(encoding="utf-8")).items() if k != "print_image"}
            if strip:
                spec["regions"], spec["fields"] = [], []
            p.write_text(yaml.safe_dump(spec, allow_unicode=True, sort_keys=False), encoding="utf-8")
    pdfs = sorted(synth10.scans.glob("*.pdf"))[:2]
    settings = Settings(site=site, archive_root=synth10.scans, work_root=root / "work", reviews=root / "reviews.jsonl",
                        save_aligned=False)
    pipe = Pipeline(settings)
    pipe.run(pdfs)
    pipe.con.close()
    db = root / "work" / "minedocscan.db"
    db_sha = hashlib.sha256(db.read_bytes()).hexdigest()
    layers = {name: {"dir": root / "templates" / name, "summary": build(root / "templates" / name, settings)}
              for name in (T_USAGE, T_LOADER)}
    return {"settings": settings, "db": db, "db_sha": db_sha, "pdfs": pdfs, "layers": layers}


@pytest.fixture(scope="session")
def usage_pages(usage_run) -> list[dict]:
    """usage_run 의 적재된 쪽마다 저장된 호모그래피로 다시 편 그림 (tools/printlayer.page_image — 파이프라인의 정합 그림과
    바이트까지 같다). 인쇄 층을 끈 결과·틀린 층은 이 그림에서 함수 단위로 다시 잰다 — 파이프라인을 한 번 더 돌리지 않는다 (6절).
    [{page_id, document_id, page_no, source_name, source, tpl, aligned}] (쪽 순서)."""
    from minedocscan.tools.printlayer import page_image

    con, site, settings = usage_run["pipe"].con, usage_run["pipe"].site, usage_run["settings"]
    out = []
    for r in con.execute("SELECT p.*, d.source_path, d.source_rel, d.source_name FROM doc_page p JOIN doc_document d "
                         "ON p.document_id = d.document_id WHERE p.status = 'loaded' ORDER BY p.page_id").fetchall():
        tpl = site.templates[r["template_name"]]
        img, how = page_image(r, tpl, settings, tpl.reference.shape)
        assert how == "rewarped", how
        out.append({"page_id": r["page_id"], "document_id": r["document_id"], "page_no": r["page_no"],
                    "source_name": r["source_name"], "source": f"{r['source_name']}#{r['page_no']}", "tpl": tpl,
                    "aligned": img})
    return out


def reload_usage(con, usage_run, pages: list[dict], mask_of) -> None:
    """usage_run 의 쪽들을 usage 핸들러로만 다시 적재한다 (분류·정합 없이 — 다시 편 그림에서 칸을 잰다). mask_of(쪽) → 인쇄 마스크
    | None. 파이프라인과 같은 순서다: observe_cells(그림, 템플릿, 마스크) → 핸들러의 load (값 유무는 handlers/usage.presence) →
    마무리의 검산. 인쇄 층을 끈 DB 를 파이프라인 한 번 더 없이 만든다.
    con 은 usage_run 의 DB 를 복사한 것이어도 된다: 다시 적재할 쪽의 핸들러 행(doc_field·eq_usage_daily·prod_tally)과 검산
    (xcheck_usage — 마무리가 전부 다시 만든다)을 먼저 지운다 — 다시 적재가 빠뜨린 행이 복사본의 값으로 남아 "같다"가 되지 않게.
    메타 필드의 기계 행(파이프라인이 핸들러보다 먼저 쓴다)은 이 합성에 없다 (인식기 oracle, 메타 모델 없음)."""
    from minedocscan.handlers import PageContext, get_handler
    from minedocscan.imaging.cells import observe_cells
    from minedocscan.pagemeta import page_meta_of

    pipe, settings = usage_run["pipe"], usage_run["settings"]
    ids = [pg["page_id"] for pg in pages]
    q = ", ".join("?" * len(ids))
    for t in ("doc_field", "eq_usage_daily", "prod_tally"):
        con.execute(f"DELETE FROM {t} WHERE page_id IN ({q})", ids)
    con.execute("DELETE FROM xcheck_usage")
    h = get_handler("usage")
    for pg in pages:
        m = mask_of(pg)
        obs = observe_cells(pg["aligned"], pg["tpl"], m)
        h.load(PageContext(con, settings, pipe.site, pg["tpl"], pg["document_id"], pg["page_id"], pg["page_no"],
                           pg["source_name"], page_meta_of(con, pg["page_id"]), pg["aligned"], obs, pipe.recognizer,
                           pipe.corrector, print_mask=m))
    h.finalize(con, pipe.site, settings)
    for t in ("doc_field", "eq_usage_daily"):                       # 쪽마다 다시 쓴 행이 있다
        assert {r[0] for r in con.execute(f"SELECT DISTINCT page_id FROM {t} WHERE page_id IN ({q})", ids)} == set(ids), t

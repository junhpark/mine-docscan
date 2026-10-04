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

    out = sqlite3.connect(":memory:")
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


def meta_options() -> dict:
    """시험용 메타 필드 모델 두 개를 네 키에 (설정 [recognize.meta] 와 같은 모양)."""
    d, o = str(META_FIXTURES / "meta-digits"), str(META_FIXTURES / "meta-operator")
    return {"meta": {"vehicle_no": d, "date.month": d, "date.day": d, "operator": o}}


def meta_run(synth, root: Path, labels: dict | None = None, **kw) -> dict:
    """사이트 팩을 복사해(라벨을 labels 로 바꿔) 메타 필드 모델로 돌린다. 정합 이미지는 저장하지 않는다."""
    import json
    import shutil

    site = root / "site"
    shutil.copytree(synth.site, site)
    if labels is not None:
        (site / "labels" / "pages.json").write_text(json.dumps(labels, ensure_ascii=False), encoding="utf-8")
    kw.setdefault("save_aligned", False)
    settings = Settings(site=site, archive_root=synth.scans, work_root=root / "work", reviews=root / "reviews.jsonl",
                        recognizer_options=meta_options(), **kw)
    pipe = Pipeline(settings)
    pipe.run([synth.scans])
    return {"root": root, "settings": settings, "pipe": pipe}


@pytest.fixture(scope="session")
def meta_truth(meta_synth) -> dict:
    import json

    return json.loads((meta_synth.site / "labels" / "pages.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def meta_nolabels(meta_synth, tmp_path_factory) -> dict:
    """meta_synth 를 라벨 없이 메타 필드 모델로 돌린 것. 이 DB 에 쓰지 않는다."""
    return meta_run(meta_synth, tmp_path_factory.mktemp("meta_nolabels"), labels={})


@pytest.fixture(scope="session")
def meta_labeled(meta_synth, tmp_path_factory) -> dict:
    """meta_synth 를 라벨 그대로 메타 필드 모델로 돌린 것 (기계 값은 대조에만). 이 DB 에 쓰지 않는다."""
    return meta_run(meta_synth, tmp_path_factory.mktemp("meta_labeled"))

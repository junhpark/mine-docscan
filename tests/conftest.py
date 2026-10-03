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

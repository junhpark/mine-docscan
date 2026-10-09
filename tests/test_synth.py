import json

import numpy as np
import pytest

from minedocscan.tools.synth import SLOTS, UG_ROWS, _day_truth, expected_xcheck, generate


def test_same_seed_same_truth(tmp_path):
    a = generate(tmp_path / "a", days=1, seed=3)
    b = generate(tmp_path / "b", days=1, seed=3)
    assert a.truth == b.truth
    assert json.loads(a.answers_path.read_text(encoding="utf-8")) == json.loads(b.answers_path.read_text(encoding="utf-8"))
    assert (a.site / "templates" / "synth_inspection" / "reference.png").read_bytes() == \
           (b.site / "templates" / "synth_inspection" / "reference.png").read_bytes()
    assert generate(tmp_path / "c", days=1, seed=4).truth["days"] != a.truth["days"]


def test_expected_xcheck_counts_every_cell():
    days = [_day_truth(d, f"2030-01-{7 + d:02d}", np.random.default_rng(0)) for d in range(3)]
    for with_trips in (False, True):
        c = expected_xcheck(days, with_trips)
        assert sum(c.values()) == 3 * len(SLOTS) * len(UG_ROWS)
    has_only, with_trips = expected_xcheck(days, False), expected_xcheck(days, True)
    n_missing = sum(x["type"] == "missing_in_matrix" for d in days for x in d["discrepancies"])
    n_count = sum(x["type"] == "different_count" for d in days for x in d["discrepancies"])
    assert has_only["mismatch"] == n_missing
    assert with_trips["mismatch"] == n_missing + n_count


def test_same_seed_gives_identical_pdf_bytes(tmp_path):
    """문서 ID 는 파일 해시다. 같은 seed 면 바이트까지 같아야 테스트의 문서 ID 가 실행마다 바뀌지 않는다."""
    import hashlib

    a = generate(tmp_path / "a", days=1, seed=5)
    b = generate(tmp_path / "b", days=1, seed=5)
    pa, pb = sorted(a.scans.glob("*.pdf")), sorted(b.scans.glob("*.pdf"))
    assert [p.name for p in pa] == [p.name for p in pb]
    assert [hashlib.sha256(p.read_bytes()).hexdigest() for p in pa] == [hashlib.sha256(p.read_bytes()).hexdigest() for p in pb]


@pytest.mark.slow
@pytest.mark.parametrize("opts", [{"usage_logs": True}, {"usage_only": True, "print_layers": True, "usage_variants": True},
                                  {"low_cells": True}, {"meta_fields": True, "mix_pages": True}, {"rotate_pages": True},
                                  {"blank_backs": True}, {"rescans": True}, {"display_names": True}],
                         ids=lambda o: "+".join(o))
def test_same_seed_gives_identical_pdf_bytes_for_every_option(tmp_path, opts):
    """선택마다 같은 seed 면 PDF 의 바이트가 같다 (tasks/0009 4.3 가 — 합성 PDF 를 직접 쓴다: /ID·만든 시각 없이)."""
    import hashlib

    days = 2 if opts.get("mix_pages") else 1                        # mix_pages 는 마지막 날의 묶음에 앞날의 쪽을 섞는다
    a = generate(tmp_path / "a", days=days, seed=5, **opts)
    b = generate(tmp_path / "b", days=days, seed=5, **opts)
    pa, pb = sorted(a.scans.rglob("*.pdf")), sorted(b.scans.rglob("*.pdf"))
    assert pa and [p.relative_to(a.scans) for p in pa] == [p.relative_to(b.scans) for p in pb]
    assert [hashlib.sha256(p.read_bytes()).hexdigest() for p in pa] == [hashlib.sha256(p.read_bytes()).hexdigest() for p in pb]

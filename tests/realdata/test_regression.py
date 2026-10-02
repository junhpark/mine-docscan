"""실데이터 회귀 테스트 — 저장소에는 데이터가 없으므로 환경변수가 있을 때만 돈다.

  MINEDOCSCAN_SITE          실제 사이트 팩 (expected/regression.json 포함)
  MINEDOCSCAN_ARCHIVE_ROOT  스캔 원본 폴더

  pytest -m realdata

기준 수치는 사이트 팩 안에 있다. 수치가 달라지면 좋아진 것인지 망가진 것인지 확인한 뒤
`minedocscan regress --update` 로 기준을 갱신한다 (docs/DATA.md).
"""
import os

import pytest

from minedocscan.config import load_settings
from minedocscan.evaluate.regression import baseline_path, run_regression
from minedocscan.forms.sitepack import SitePack

pytestmark = pytest.mark.realdata


@pytest.fixture(scope="module")
def settings():
    if not (os.environ.get("MINEDOCSCAN_SITE") and os.environ.get("MINEDOCSCAN_ARCHIVE_ROOT")):
        pytest.skip("실데이터 없음: MINEDOCSCAN_SITE, MINEDOCSCAN_ARCHIVE_ROOT 를 지정하면 실행됩니다")
    return load_settings()


def test_matches_site_baseline(settings):
    site = SitePack(settings.site)
    if not baseline_path(site).exists():
        pytest.skip(f"기준 파일 없음: {baseline_path(site)}")
    res = run_regression(settings, site)
    assert res["ok"], "기준과 다른 항목:\n" + "\n".join(f"  {k}: 기준 {e} → 지금 {a}" for k, e, a in res["diffs"])

"""실데이터 회귀 검사: 사이트 팩에 저장한 기준 수치와 지금 코드의 결과를 비교한다.

기준 파일은 사이트 팩의 expected/regression.json 이다 (저장소 밖 — 실제 문서에 종속된 수치이므로).

  {"inputs": ["폴더 또는 파일 (archive_root 기준 상대경로)", …],
   "recognizer": "null",
   "report": { …build_report() 결과… }}

검수 파일은 읽어 들이지 않는다. 회귀는 코드(기계)의 수치를 보는 것이고, 검수가 쌓일수록 pending 과 trips 가
달라지면 코드 변경 없이도 기준과 어긋나기 때문이다. 검수값과 비교하는 평가는 eval --target raw 로 따로 한다.

수치가 달라졌다면 둘 중 하나다: 고쳐서 좋아졌거나(기준을 갱신), 망가뜨렸거나(코드를 고친다).
어느 쪽인지는 사람이 본다. 기준 갱신은 `minedocscan regress --update`.
"""
from __future__ import annotations

import json
import tempfile
from dataclasses import replace
from pathlib import Path

from ..config import Settings
from ..forms.sitepack import SitePack
from ..pipeline import Pipeline
from ..recognize import get_recognizer
from ..report import build_report


def baseline_path(site: SitePack) -> Path:
    return site.root / "expected" / "regression.json"


def flatten(d: dict, prefix: str = "") -> dict:
    out = {}
    for k, v in d.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(flatten(v, key + "."))
        else:
            out[key] = v
    return out


def diff_reports(expected: dict, actual: dict, float_tol: float = 0.01) -> list[tuple[str, object, object]]:
    e, a = flatten(expected), flatten(actual)
    diffs = []
    for k in sorted(set(e) | set(a)):
        ev, av = e.get(k), a.get(k)
        if isinstance(ev, float) or isinstance(av, float):
            if ev is None or av is None or abs(ev - av) > float_tol:
                diffs.append((k, ev, av))
        elif ev != av:
            diffs.append((k, ev, av))
    return diffs


def run_regression(settings: Settings, site: SitePack | None = None, update: bool = False,
                   inputs: list[str] | None = None) -> dict:
    site = site or SitePack(settings.site)
    path = baseline_path(site)
    spec = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    inputs = inputs or spec.get("inputs")
    if not inputs:
        raise ValueError(f"기준 파일이 없거나 inputs 가 비었습니다: {path} (처음에는 --update --inputs … 로 만듭니다)")
    if settings.archive_root is None:
        raise ValueError("archive_root 가 필요합니다 (MINEDOCSCAN_ARCHIVE_ROOT)")
    missing = [i for i in inputs if not (settings.archive_root / i).exists()]
    if missing:
        raise FileNotFoundError(f"archive_root 에 입력이 없습니다: {missing}")
    recognizer = spec.get("recognizer", "null")
    with tempfile.TemporaryDirectory(prefix="minedocscan-regress-") as tmp:
        s = replace(settings, work_root=Path(tmp), db_url=None, save_aligned=False)
        pipe = Pipeline(s, site=site, recognizer=get_recognizer(recognizer), load_reviews=False)
        pipe.run([settings.archive_root / i for i in inputs])
        actual = build_report(pipe.con)
        pipe.con.close()
    diffs = diff_reports(spec.get("report", {}), actual) if spec.get("report") else []
    if update:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"inputs": inputs, "recognizer": recognizer, "report": actual},
                                   ensure_ascii=False, indent=1), encoding="utf-8")
    return {"ok": not diffs, "diffs": diffs, "report": actual, "baseline": str(path), "updated": update,
            "had_baseline": bool(spec.get("report"))}

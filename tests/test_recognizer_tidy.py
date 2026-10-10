"""tasks/0005 단계 1 — 손질: 끊긴 학습의 임시 폴더, [recognize.meta] 의 모델 아닌 항목, --extra-digits 폴더의 규칙 (torch 없이)."""
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from minedocscan.cli import main
from minedocscan.config import ConfigError, Settings
from minedocscan.imaging.cropspec import CropSpec
from minedocscan.imaging.io import imwrite
from minedocscan.recognize.digits.model import clean_orphan_staging, list_models, staging_dir
from minedocscan.recognize.digits.train import TrainError
from minedocscan.recognize.meta.model import build_meta_readers
from minedocscan.recognize.meta.train import META_SPEC, MetaTrainArgs, read_extra_digits, train_meta
from minedocscan.tools import synth_meta
from minedocscan.tools.synth_cells import CellParams, make_cells

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def dead_pid() -> int:
    """끝난 프로세스의 pid (곧바로 다른 프로세스가 받을 일은 거의 없다)."""
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def write_digit_crops(root: Path, n: int, seed: int = 0, spec: CropSpec = META_SPEC, split: str = "train") -> int:
    """`review export-crops --kind handwritten_number` 와 같은 모양의 합성 숫자 칸 폴더. 돌려주는 값: 숫자열이 정답인 칸 수."""
    out = root / split / "handwritten_number"
    out.mkdir(parents=True, exist_ok=True)
    cells = make_cells(n, seed, CellParams(spec=spec))
    lines = []
    for i, c in enumerate(cells):
        name = f"c{seed}_{i}.png"
        imwrite(out / name, c.image)
        lines.append({"file": f"{split}/handwritten_number/{name}", "text": c.text, "verdict": "value" if c.text else "empty",
                      "split": split, "work_date": f"2030-02-{1 + i % 20:02d}", "kind": "handwritten_number",
                      "field_id": f"p{seed}:haul:trips_day:{i}", "spec": spec.to_dict(), "inked": True,
                      "bbox": [0, 0, 92, 21]})
    with open(root / split / "labels.jsonl", "a", encoding="utf-8") as f:
        for line in lines:
            f.write(json.dumps(line) + "\n")
    return sum(c.text.isdigit() for c in cells)


def test_orphan_staging_folders_are_not_models_and_are_cleaned(tmp_path, capsys):
    """끊긴 학습의 `.<이름>.tmp-<pid>` 는 recognizer list 에 나오지 않고, 다음 학습이 임시 폴더를 만들 때 치운다.
    다른 창에서 학습 중인(프로세스가 살아 있는) 임시 폴더는 두고, 같은 이름으로 다시 학습할 자리가 생긴다."""
    site = tmp_path / "site"
    models = site / "models"
    shutil.copytree(FIXTURES / "meta-digits", models / "veh-v1")
    dead = models / f".veh-v2.tmp-{dead_pid()}"
    dead.mkdir()
    (dead / "card.json").write_text("{", encoding="utf-8")                 # 반쯤 쓴 카드
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        live = models / f".other.tmp-{sleeper.pid}"
        live.mkdir()
        assert [m["name"] for m in list_models(site)] == ["veh-v1"]
        capsys.readouterr()
        assert main(["recognizer", "list", "--site", str(site)]) == 0
        out = capsys.readouterr().out
        assert ".tmp-" not in out and "veh-v2" not in out and "veh-v1" in out and "1개" in out
        tmp = staging_dir(models / "veh-v2")                                 # 같은 이름의 새 학습
        assert tmp.is_dir() and not dead.exists() and live.exists()
        assert not (models / "veh-v2").exists()                              # 이름 자리는 비어 있다 — 다 만든 뒤 바꾼다
        assert clean_orphan_staging(models) == []                            # 지금 프로세스와 살아 있는 프로세스의 것은 두었다
    finally:
        sleeper.kill()
        sleeper.wait()
    assert clean_orphan_staging(models) == [live.name] and tmp.exists()


def test_meta_table_takes_only_model_names(tmp_path, meta_synth):
    """[recognize.meta] auto_accept_conf = 0.9 는 조용히 무시되던 것 → 설정 오류 한 줄 (메타 모델의 기준은 카드에서만)."""
    with pytest.raises(ConfigError, match="auto_accept_conf"):
        build_meta_readers(Settings(recognizer_options={"meta": {"auto_accept_conf": 0.9}}), None)
    with pytest.raises(ConfigError, match="operator"):
        build_meta_readers(Settings(recognizer_options={"meta": {"operator": 3}}), None)
    cfg = tmp_path / "c.toml"
    cfg.write_text(f'[recognize.meta]\nvehicle_no = {json.dumps(str(FIXTURES / "meta-digits"))}\nauto_accept_conf = 0.9\n',
                   encoding="utf-8")                                    # json 의 글자열 = TOML 의 기본 글자열 (윈도우 경로의 역슬래시)
    for cmd in (["info"], ["run", str(meta_synth.scans), "--work-root", str(tmp_path / "w")]):
        with pytest.raises(SystemExit) as e:
            main([*cmd, "--config", str(cfg), "--site", str(meta_synth.site)])
        msg = str(e.value)
        assert msg.startswith("설정 오류: [recognize.meta] auto_accept_conf") and "\n" not in msg


def test_extra_digits_folder_rules(tmp_path):
    """--extra-digits: 숫자열이 정답인 칸만 더한다 (빈 칸은 뺀다). test 줄이 섞이거나 규격이 메타 크롭과 다르면 거절.
    분류기(작성자)에는 쓰지 않는다. 이 검사들은 torch 없이 학습을 시작하기 전에 끝난다."""
    n = write_digit_crops(tmp_path / "ok", 40, seed=1)
    samples, info = read_extra_digits(tmp_path / "ok", META_SPEC)
    assert len(samples) == n == info["cells"] and 0 < n < 40 and info["skipped"] == 40 - n
    assert all(s.text.isdigit() for s in samples) and info["dates"] > 1
    write_digit_crops(tmp_path / "with-test", 10, seed=2)
    write_digit_crops(tmp_path / "with-test", 5, seed=3, split="test")
    with pytest.raises(TrainError, match="test"):
        read_extra_digits(tmp_path / "with-test", META_SPEC)
    write_digit_crops(tmp_path / "other-spec", 10, seed=4, spec=CropSpec("source", 1.5, None))
    with pytest.raises(TrainError, match="--pad 8"):
        read_extra_digits(tmp_path / "other-spec", META_SPEC)
    synth_meta.write_meta_crops(tmp_path / "op", ("operator",), 3, seed=0)
    with pytest.raises(TrainError, match="extra-digits"):
        train_meta(tmp_path / "op", tmp_path / "m", MetaTrainArgs(name="op", keys=("operator",), extra_digits=str(tmp_path / "ok")))
    with pytest.raises(SystemExit, match="extra-digits"):
        main(["recognizer", "train", "--name", "x", "--extra-digits", str(tmp_path / "ok"), "--out", str(tmp_path / "x")])

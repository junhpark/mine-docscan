from pathlib import Path

from minedocscan.config import load_settings


def test_defaults(tmp_path, monkeypatch):
    for k in ("MINEDOCSCAN_CONFIG", "MINEDOCSCAN_ARCHIVE_ROOT", "MINEDOCSCAN_WORK_ROOT", "MINEDOCSCAN_SITE",
              "MINEDOCSCAN_DB_URL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.chdir(tmp_path)
    s = load_settings()
    assert s.recognizer == "null" and s.corrector == "none" and s.dpi == 200
    assert s.resolved_db_url == "sqlite:///work/minedocscan.db"


def test_file_then_env_then_override(tmp_path, monkeypatch):
    cfg = tmp_path / "c.toml"
    cfg.write_text('[paths]\nsite = "from_file"\nwork_root = "w_file"\n[pipeline]\nauto_accept_conf = 0.8\n'
                   '[recognize]\nbackend = "oracle"\n', encoding="utf-8")
    monkeypatch.delenv("MINEDOCSCAN_SITE", raising=False)
    monkeypatch.setenv("MINEDOCSCAN_WORK_ROOT", "w_env")
    s = load_settings(cfg)
    assert s.site == Path("from_file") and s.work_root == Path("w_env")
    assert s.auto_accept_conf == 0.8 and s.recognizer == "oracle"
    s = load_settings(cfg, work_root="w_arg", site=None)
    assert s.work_root == Path("w_arg") and s.site == Path("from_file")     # None 은 덮어쓰지 않는다

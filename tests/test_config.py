from pathlib import Path

import pytest

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


def test_damaged_pdf_policy(tmp_path, monkeypatch):
    monkeypatch.delenv("MINEDOCSCAN_DAMAGED_PDF", raising=False)
    cfg = tmp_path / "c.toml"
    cfg.write_text('[pipeline]\ndamaged_pdf = "warn"\n', encoding="utf-8")
    assert load_settings(tmp_path / "none.toml").damaged_pdf == "fail"
    assert load_settings(cfg).damaged_pdf == "warn"
    monkeypatch.setenv("MINEDOCSCAN_DAMAGED_PDF", "fail")
    assert load_settings(cfg).damaged_pdf == "fail"                   # 환경변수가 이긴다
    monkeypatch.setenv("MINEDOCSCAN_DAMAGED_PDF", "maybe")
    import pytest

    with pytest.raises(ValueError):
        load_settings(cfg)


def test_bad_values_are_one_line_config_errors(tmp_path, monkeypatch):
    """틀린 설정 값은 ConfigError 한 줄 — 어느 항목이 왜 틀렸는지 (tasks/0004 단계 1)."""
    import pytest

    from minedocscan.config import ConfigError

    monkeypatch.delenv("MINEDOCSCAN_DAMAGED_PDF", raising=False)
    cases = {
        '[pipeline]\ndpi = "two hundred"\n': r"\[pipeline\] dpi",
        "[pipeline]\nauto_accept_conf = 1.5\n": r"auto_accept_conf",
        "[pipeline]\nauto_accept_conf = true\n": r"auto_accept_conf",
        "[pipeline]\nclassify_min_margin = -1\n": r"classify_min_margin",
        '[pipeline]\nsave_aligned = "yes"\n': r"save_aligned",
        "[review]\nsource_dpi = 30\n": r"source_dpi",
        '[pipeline]\ndamaged_pdf = "ignore"\n': r"damaged_pdf",
        "[recognize]\nby_kind = 3\n": r"by_kind",
        "[pipeline]\ndup_min_sim = 1.2\n": r"dup_min_sim",
        "[intake]\npoll_seconds = 0\n": r"\[intake\] poll_seconds",
        '[intake]\nsettle_seconds = "soon"\n': r"settle_seconds",
        "intake = 3\n": r"\[intake\]",
        "pipeline = 3\n": r"\[pipeline\]",
        "[pipeline\ndpi = 200\n": r"설정 파일을 읽을 수 없습니다",
    }
    for i, (text, pat) in enumerate(cases.items()):
        cfg = tmp_path / f"c{i}.toml"
        cfg.write_text(text, encoding="utf-8")
        with pytest.raises(ConfigError, match=pat) as ei:
            load_settings(cfg)
        assert "\n" not in str(ei.value).strip() or "TOML" in str(ei.value) or "읽을 수 없" in str(ei.value)


def test_cli_reports_config_errors_without_traceback(tmp_path, monkeypatch, capsys):
    """어느 명령이든 틀린 설정 값이면 트레이스백 없이 한 줄로 끝나고 종료 코드가 0 이 아니다."""
    import pytest

    from minedocscan.cli import main

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MINEDOCSCAN_DAMAGED_PDF", "maybe")
    for argv in (["info"], ["report", "--work-root", str(tmp_path / "w")], ["run", str(tmp_path)],
                 ["review", "stats", "--site", str(tmp_path)], ["recognizer", "list", "--site", str(tmp_path)]):
        with pytest.raises(SystemExit) as ei:
            main(argv)
        msg = str(ei.value.code)
        assert msg.startswith("설정 오류:") and "MINEDOCSCAN_DAMAGED_PDF" in msg and "Traceback" not in msg, argv
    monkeypatch.delenv("MINEDOCSCAN_DAMAGED_PDF")
    cfg = tmp_path / "bad.toml"
    cfg.write_text("[pipeline]\ndpi = 0\n", encoding="utf-8")
    with pytest.raises(SystemExit, match=r"설정 오류: \[pipeline\] dpi"):
        main(["info", "--config", str(cfg)])
    # 사이트 팩의 site.toml 이 깨졌을 때도
    site = tmp_path / "site"
    site.mkdir()
    (site / "site.toml").write_text("[site\n", encoding="utf-8")
    with pytest.raises(SystemExit, match=r"설정 오류: site\.toml"):
        main(["review", "stats", "--site", str(site), "--work-root", str(tmp_path / "w2")])


def test_publish_secrets_are_refused_in_any_case_without_echoing_them(tmp_path):
    """[publish] 의 URL·비밀번호 키는 대소문자·앞뒤 공백과 상관없이, conninfo 도 거절한다 — 글에 비밀값을 찍지 않는다 (tasks/0009 4.1 사)."""
    from minedocscan.config import ConfigError

    secret = "s3cr3t-p4ss"
    cfg = tmp_path / "c.toml"
    for key in ("URL", "Url", "Password", "PASSWORD", "conninfo", "ConnInfo", '" dsn"', '"DSN "', '" password "'):
        cfg.write_text(f'[publish]\n{key} = "postgresql://writer:{secret}@db.example.invalid:5432/site"\n', encoding="utf-8")
        with pytest.raises(ConfigError, match="MINEDOCSCAN_PUBLISH_URL") as ei:
            load_settings(cfg)
        assert secret not in str(ei.value) and "writer" not in str(ei.value), key
    cfg.write_text('[publish]\nschema = "site_a"\nretry_seconds = 10\n', encoding="utf-8")   # 그 밖의 키는 그대로
    assert load_settings(cfg).publish_schema == "site_a"


def test_excel_dir_must_be_text(tmp_path, monkeypatch):
    """[export] excel_dir 가 글자가 아니면 ConfigError 한 줄 (Path(5) 의 TypeError 가 아니라)."""
    from minedocscan.config import ConfigError

    monkeypatch.delenv("MINEDOCSCAN_EXCEL_DIR", raising=False)
    cfg = tmp_path / "c.toml"
    for bad in ("5", "true", '["a", "b"]', "1.5"):
        cfg.write_text(f"[export]\nexcel_dir = {bad}\n", encoding="utf-8")
        with pytest.raises(ConfigError, match=r"\[export\] excel_dir"):
            load_settings(cfg)
    cfg.write_text('[export]\nexcel_dir = "엑셀"\n', encoding="utf-8")
    assert load_settings(cfg).excel_dir == Path("엑셀")


NON_FINITE = [("[intake]\npoll_seconds = nan\n", r"\[intake\] poll_seconds"),
              ("[intake]\nsettle_seconds = inf\n", r"\[intake\] settle_seconds"),
              ("[publish]\nsweep_minutes = inf\n", r"\[publish\] sweep_minutes"),
              ("[publish]\nlock_timeout_s = nan\n", r"\[publish\] lock_timeout_s"),
              ("[publish]\nretry_seconds = inf\n", r"\[publish\] retry_seconds"),
              ("[export]\nsweep_minutes = -inf\n", r"\[export\] sweep_minutes"),
              ("[pipeline]\nauto_accept_conf = nan\n", r"\[pipeline\] auto_accept_conf"),
              ("[pipeline]\nclassify_min_margin = inf\n", r"\[pipeline\] classify_min_margin"),
              ("[pipeline]\ndpi = nan\n", r"\[pipeline\] dpi"),
              ("[pipeline]\ndpi = inf\n", r"\[pipeline\] dpi"),                       # 정수 설정 — int(inf) 는 OverflowError
              ("[review]\nsource_dpi = -inf\n", r"\[review\] source_dpi")]


def _non_finite_id(case) -> str:
    return case[0].replace("\n", " ").strip()


@pytest.mark.parametrize("text, pat", NON_FINITE, ids=[_non_finite_id(c) for c in NON_FINITE])
def test_non_finite_numbers_are_config_errors(tmp_path, monkeypatch, text, pat):
    """숫자 설정의 nan·inf 는 ConfigError 한 줄 — nan 은 범위 검사를 지나가고, 정수 설정의 inf 는 OverflowError 였다 (tasks/0009 4.1 사)."""
    from minedocscan.config import ConfigError

    monkeypatch.delenv("MINEDOCSCAN_DAMAGED_PDF", raising=False)
    cfg = tmp_path / "c.toml"
    cfg.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError, match=pat) as ei:
        load_settings(cfg)
    assert "\n" not in str(ei.value)


def test_an_empty_config_variable_is_unset_and_a_folder_is_a_config_error(tmp_path, monkeypatch):
    """빈 MINEDOCSCAN_CONFIG 는 없는 것과 같다 (지금 폴더를 열다 트레이스백으로 죽었다 — 설명서의 검토). 폴더를 주면 한 줄의 설정 오류."""
    from minedocscan.config import ConfigError

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MINEDOCSCAN_SITE", raising=False)
    monkeypatch.setenv("MINEDOCSCAN_CONFIG", "")
    assert load_settings().site is None
    monkeypatch.setenv("MINEDOCSCAN_CONFIG", str(tmp_path))
    with pytest.raises(ConfigError, match="폴더"):
        load_settings()

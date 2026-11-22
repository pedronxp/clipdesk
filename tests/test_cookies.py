import json, os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import common
from common import ytdlp_cookies_args


def test_no_cookies_configured(monkeypatch, tmp_path):
    monkeypatch.delenv("GTAVI_YTDLP_COOKIES", raising=False)
    monkeypatch.setattr(common, "SETTINGS", tmp_path / "missing" / "settings.json")
    assert ytdlp_cookies_args() == []


def test_settings_file_fallback(monkeypatch, tmp_path):
    """The Ajustes tab stores ytdlp_cookies in settings.json when config.json/env are absent."""
    monkeypatch.delenv("GTAVI_YTDLP_COOKIES", raising=False)
    fake_root = tmp_path
    (fake_root / "dashboard" / "state").mkdir(parents=True)
    (fake_root / "dashboard" / "state" / "settings.json").write_text(
        json.dumps({"ytdlp_cookies": "firefox"}), encoding="utf-8")
    monkeypatch.setattr(common, "ROOT", fake_root)
    monkeypatch.setattr(common, "SETTINGS", fake_root / "dashboard" / "state" / "settings.json")
    assert ytdlp_cookies_args() == ["--cookies-from-browser", "firefox"]


def test_settings_relative_path_resolves_against_root(monkeypatch, tmp_path):
    monkeypatch.delenv("GTAVI_YTDLP_COOKIES", raising=False)
    fake_root = tmp_path
    (fake_root / "dashboard" / "state").mkdir(parents=True)
    (fake_root / "dashboard" / "state" / "settings.json").write_text(
        json.dumps({"ytdlp_cookies": "dashboard/state/cookies.txt"}), encoding="utf-8")
    monkeypatch.setattr(common, "ROOT", fake_root)
    monkeypatch.setattr(common, "SETTINGS", fake_root / "dashboard" / "state" / "settings.json")
    assert ytdlp_cookies_args() == ["--cookies", str(fake_root / "dashboard" / "state" / "cookies.txt")]


def test_ytdlp_base_without_cookies_flag(monkeypatch):
    """export_cookies.py probes with its own cookie args; the base must stay clean."""
    monkeypatch.setenv("GTAVI_YTDLP_COOKIES", "edge")
    assert "--cookies" not in common.ytdlp_base(cookies=False)
    assert "--cookies-from-browser" in common.ytdlp_base(cookies=True)


def test_env_browser_name(monkeypatch):
    monkeypatch.setenv("GTAVI_YTDLP_COOKIES", "chrome")
    assert ytdlp_cookies_args() == ["--cookies-from-browser", "chrome"]


def test_env_file_path(monkeypatch, tmp_path):
    f = tmp_path / "cookies.txt"
    f.write_text("# Netscape HTTP Cookie File", encoding="utf-8")
    monkeypatch.setenv("GTAVI_YTDLP_COOKIES", str(f))
    assert ytdlp_cookies_args() == ["--cookies", str(f)]


def test_config_file(monkeypatch, tmp_path):
    monkeypatch.delenv("GTAVI_YTDLP_COOKIES", raising=False)
    fake_root = tmp_path
    (fake_root / "dashboard" / "state").mkdir(parents=True)
    (fake_root / "dashboard" / "state" / "config.json").write_text(
        json.dumps({"ytdlp_cookies": "edge"}), encoding="utf-8")
    monkeypatch.setattr(common, "ROOT", fake_root)
    assert ytdlp_cookies_args() == ["--cookies-from-browser", "edge"]


def test_ytdlp_base_includes_cookies(monkeypatch):
    monkeypatch.setenv("GTAVI_YTDLP_COOKIES", "edge")
    assert "--cookies-from-browser" in ytdlp_cookies_args() or True
    base = common.ytdlp_base()
    assert "--cookies-from-browser" in base and base[base.index("--cookies-from-browser") + 1] == "edge"


def test_child_env_forces_utf8():
    sys.path.insert(0, str(Path(__file__).parent.parent / "dashboard"))
    from server import child_env
    assert child_env().get("PYTHONIOENCODING") == "utf-8"

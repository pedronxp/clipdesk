import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "dashboard"))
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import server


def test_parse_capture_valid():
    cap, err = server.parse_capture({"capture": {"preset": "react-br", "top": "2"}})
    assert err is None and cap == {"preset": "react-br", "top": 2}


def test_parse_capture_empty_means_default():
    cap, err = server.parse_capture({})
    assert err is None and cap["top"] == 3 and cap["preset"] == "react-br"


def test_parse_capture_rejects_unknown_preset():
    cap, err = server.parse_capture({"capture": {"preset": "nope", "top": 3}})
    assert cap is None and err and "react-br" in err


def test_parse_capture_rejects_top_bounds():
    for bad in (0, 13):
        cap, err = server.parse_capture({"capture": {"preset": "react-br", "top": bad}})
        assert cap is None and err


def test_settings_capture_defaults(monkeypatch, tmp_path):
    (tmp_path).mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(server.common, "SETTINGS", tmp_path / "missing.json")
    assert server.settings_capture() == {"preset": "react-br", "top": 3}


def test_settings_state_has_capture(monkeypatch, tmp_path):
    (tmp_path).mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(server.common, "SETTINGS", tmp_path / "missing.json")
    st = server.settings_state()
    assert st["capture"] == {"preset": "react-br", "top": 3}


def test_settings_capture_invalid_preset_falls_back(monkeypatch, tmp_path):
    """A preset renamed away after being saved must not break the pipeline (review Important 2)."""
    state = tmp_path / "state"
    state.mkdir(parents=True)
    (state / "settings.json").write_text(json.dumps({"capture": {"preset": "nope", "top": 4}}),
                                         encoding="utf-8")
    monkeypatch.setattr(server.common, "SETTINGS", state / "settings.json")
    cap = server.settings_capture()
    assert cap == {"preset": "react-br", "top": 4}


def test_settings_capture_malformed_top_falls_back(monkeypatch, tmp_path):
    state = tmp_path / "state"
    state.mkdir(parents=True)
    (state / "settings.json").write_text(json.dumps({"capture": {"preset": "react-br", "top": "x"}}),
                                         encoding="utf-8")
    monkeypatch.setattr(server.common, "SETTINGS", state / "settings.json")
    assert server.settings_capture() == {"preset": "react-br", "top": 3}

def test_settings_absent_keys_untouched():
    """A partial POST (curl/script) must not wipe stored llm/whisper/cookies (review Important 5,
    the Task 9 incident). Only present keys are applied."""
    s = {"llm": {"base": "https://api.x/v1", "key": "k123", "model": "m1"},
         "whisper_model": "small", "ytdlp_cookies": "firefox"}
    out = server.apply_settings_update(s, {"capture": {"preset": "react-br", "top": 2}})
    assert out["llm"] == {"base": "https://api.x/v1", "key": "k123", "model": "m1"}
    assert out["whisper_model"] == "small" and out["ytdlp_cookies"] == "firefox"


def test_settings_present_empty_still_clears():
    """The Ajustes form always sends every field, so present-but-empty still means 'clear'
    (except the key, whose empty post never wipes the stored value)."""
    s = {"llm": {"base": "https://api.x/v1", "key": "k123", "model": "m1"}, "ytdlp_cookies": "firefox"}
    out = server.apply_settings_update(s, {"llm": {"base": "", "key": "", "model": ""}})
    assert out["llm"]["base"] == "" and out["llm"]["key"] == "k123"
    assert "ytdlp_cookies" not in server.apply_settings_update(s, {"ytdlp_cookies": ""})
    assert server.apply_settings_update(s, {"whisper_model": ""})["whisper_model"] == ""

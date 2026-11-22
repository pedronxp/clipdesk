"""Quick-clips ao vivo: contracts of the per-spike engine spawn (spec 2026-11-21).
Covers the analyze_creation command builders, the blur fallback and the preset→options
mapping used by live.py's spawn_quick_clip."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import analyze_creation as ac
import live


# ---------- moments command builder ----------

def test_moments_cmd_without_durations_keeps_default_window():
    cmd = ac.build_moments_cmd("src.mp4", 1, 2)
    assert cmd[2] == "src.mp4"
    assert cmd[-2:] == ["--max-minutes", "2"]
    assert "--durations" not in cmd


def test_moments_cmd_passes_valid_durations():
    cmd = ac.build_moments_cmd("src.mp4", 1, 2, "30")
    assert cmd[-2:] == ["--durations", "30"]
    assert ac.build_moments_cmd("src.mp4", 1, 2, "15,30")[-1] == "15,30"
    # whitespace around a valid tier is stripped, not dropped
    assert ac.build_moments_cmd("src.mp4", 1, 2, " 30 ")[-1] == "30"


def test_moments_cmd_silently_drops_malformed_durations():
    for bad in ("abc", "30;x", "-5", "1.5", "30,", ",30", "", None):
        assert "--durations" not in ac.build_moments_cmd("src.mp4", 1, 2, bad), bad


# ---------- blur fallback ----------

def test_should_retry_blur_only_for_webcam_layouts():
    assert ac.should_retry_blur("react") is True
    assert ac.should_retry_blur("facecam") is True
    assert ac.should_retry_blur("blur") is False
    assert ac.should_retry_blur("fill") is False


def test_blur_retry_cmd_swaps_layout_without_touching_the_original():
    cut = [ac.PY, "cut.py", "job", "--pick", "1", "--layout", "react", "--captions", "word"]
    retried = ac.blur_retry_cmd(cut)
    assert retried[cut.index("--layout") + 1] == "blur"
    assert cut[cut.index("--layout") + 1] == "react"  # original command untouched
    assert retried[:-1] == cut[:-1] or retried != cut  # a copy, never the same list


# ---------- preset → options ----------

def test_react_br_yields_react_sentence_pt():
    opts = live.quick_clip_options_for("react-br")
    assert opts["mode"] == "auto" and opts["rerank"] is True
    assert opts["layout"] == "react"
    assert opts["captions"] == "sentence" and opts["lang"] == "pt"
    assert opts["durations"] == "30" and opts["orientation"] == "vertical"


def test_original_preset_keeps_native_word_captions():
    opts = live.quick_clip_options_for("original")
    assert opts["layout"] == "react"
    assert opts["captions"] == "word"  # lang "" → no translation path


def test_blur_multi_preset_uses_blur_layout():
    opts = live.quick_clip_options_for("blur-multi")
    assert opts["layout"] == "blur" and opts["captions"] == "sentence"


def test_unknown_preset_in_settings_degrades_to_default(monkeypatch):
    monkeypatch.setattr(live, "load_settings", lambda: {"capture": {"preset": "preset-que-nao-existe"}})
    opts = live.quick_clip_options()
    assert opts["layout"] == "react" and opts["lang"] == "pt"


def test_settings_without_capture_key_uses_default_preset(monkeypatch):
    monkeypatch.setattr(live, "load_settings", lambda: {})
    opts = live.quick_clip_options()
    assert opts["captions"] == "sentence" and opts["lang"] == "pt"


# ---------- off-switch ----------

def test_quick_clips_enabled_by_default(monkeypatch):
    monkeypatch.setenv("GTAVI_QUICK_CLIPS", "")
    assert live.quick_clips_enabled() is True
    monkeypatch.delenv("GTAVI_QUICK_CLIPS", raising=False)
    assert live.quick_clips_enabled() is True


def test_quick_clips_disabled_by_env(monkeypatch):
    monkeypatch.setenv("GTAVI_QUICK_CLIPS", "0")
    assert live.quick_clips_enabled() is False

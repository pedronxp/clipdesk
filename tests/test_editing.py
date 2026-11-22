import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).parent.parent / "dashboard"))

from common import load_presets
from cut import _cache_bucket, zoom_filter
from monitor import blur_cut_cmd, draft_title
from pack import H, TOP, draw_band_filter, title_band


def test_presets_ship_the_three_editing_modes():
    p = load_presets()
    assert p["default"] == "react-br"
    assert p["presets"]["react-br"]["lang"] == "pt"
    assert p["presets"]["react-br"]["zoom"] == "face"
    assert p["presets"]["original"]["lang"] == ""
    assert "pt" in p["presets"]["blur-multi"]["langs"]


def test_translation_cache_is_isolated_per_language():
    cache = {"Hola.": "Ola."}  # legacy flat cache = PT-BR
    assert _cache_bucket(cache, "pt") is cache
    en = _cache_bucket(cache, "en")
    assert en is not cache and en == {}
    assert _cache_bucket(cache, "en") is en


def test_zoom_filter_has_slow_punch_in_and_panel_size():
    f = zoom_filter(1.18, 10, 1080, 730)
    assert "zoompan" in f and "s=1080x730" in f and "min(zoom+" in f
    assert "1.1800" in f


def test_draft_title_masks_and_stays_short():
    t = draft_title("the nigger word and fuck this, then a very long tail " + "x" * 200, 2)
    assert "nigger" not in t and "fuck" not in t and len(t) <= 86


def test_title_band_wraps_to_three_lines():
    bh, lines = title_band("Uma frase bem comprida para o titulo do clipe inteiro")
    assert len(lines) == 3 and bh == 3 * 78 + 44  # LINE_H/BAND_PAD: same band math as cut.py's TitleBox


def test_thumb_drawtext_keeps_every_line_inside_the_band():
    title = "Uma frase bem comprida para o titulo do clipe inteiro"
    bh, lines = title_band(title)
    f = draw_band_filter(title)
    assert f.count("drawtext=") == len(lines)
    y0 = min(max(0, TOP - bh // 2), max(0, H - bh))  # same clamp draw_band_filter applies
    ys = [int(p.split(":y=")[1].split(",")[0]) for p in f.split("drawtext=")[1:]]
    assert ys and all(y0 <= y <= y0 + bh for y in ys)


def test_blur_fallback_drops_the_cam_zoom():
    cmd = ["cut.py", "job", "--pick", "1", "--zoom", "face", "--lang", "pt"]
    out = blur_cut_cmd(cmd, {"zoom": "face"})
    assert out[-2:] == ["--layout", "blur"]
    assert out[out.index("--zoom") + 1] == "game"
    assert out[out.index("--lang") + 1] == "pt"

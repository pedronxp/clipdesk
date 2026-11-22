import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "dashboard"))
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import server


def test_note_survives_parse(monkeypatch, tmp_path):
    """The Kick blocked note travels from streams.json to the panel payload (bug: Kick)."""
    monkeypatch.setattr(server, "ROOT", tmp_path)
    (tmp_path / "streams.json").write_text(json.dumps({
        "query": "gta 6", "checked": "2026-11-21 21:00",
        "sections": [{"platform": "Kick", "note": "A Kick bloqueia acesso automatizado",
                      "rows": []},
                     {"platform": "YouTube", "note": "", "rows": [{"channel": "x"}]}]}),
        encoding="utf-8")
    s = server.parse_streams_md()
    kick = next(sec for sec in s["sections"] if sec["platform"] == "Kick")
    assert kick["note"] == "A Kick bloqueia acesso automatizado"
    yt = next(sec for sec in s["sections"] if sec["platform"] == "YouTube")
    assert yt["note"] == ""


def test_pt_sort_brazil_first():
    """PT-BR streamers first, then viewers (operador: foca nas PT-BR)."""
    import streams
    rows = [{"channel": "big-gringo", "viewers": 9000, "pt": False},
            {"channel": "br-pequeno", "viewers": 10, "pt": True},
            {"channel": "gringo-medio", "viewers": 500, "pt": False}]
    out = streams.pt_sort(rows)
    assert [r["channel"] for r in out] == ["br-pequeno", "big-gringo", "gringo-medio"]


def test_pt_flag_markers_and_language():
    import streams
    assert streams.pt_flag({"title": "ZOEI O CARRO NO GTA 6 (PT-BR)", "language": None}) is True
    assert streams.pt_flag({"title": "Insane GTA 6 gameplay", "language": "pt"}) is True
    assert streams.pt_flag({"title": "Insane GTA 6 gameplay", "language": "en"}) is False


def test_pt_flag_twitch_uppercase():
    """Twitch GQL returns uppercase codes ("PT") — the flag must catch them (found live)."""
    import streams
    assert streams.pt_flag({"title": "qualquer coisa", "language": "PT"}) is True
    assert streams.pt_flag({"title": "qualquer coisa", "language": "RU"}) is False


def test_pt_sort_survives_the_cut():
    """A PT streamer beyond the top viewers must NOT be cut out before the ranking
    (order bug found live: cut happened before pt_sort)."""
    import streams
    wide = [{"channel": f"gringo{i}", "viewers": 5000 - i, "pt": False} for i in range(60)]
    wide.append({"channel": "thedarkness", "viewers": 1278, "pt": True})
    out = streams.pt_sort(wide)[:20]
    assert out[0]["channel"] == "thedarkness"

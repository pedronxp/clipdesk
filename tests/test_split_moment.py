import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "dashboard"))
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import server


def _job(tmp_path, name="job-s"):
    jd = tmp_path / "jobs" / name
    jd.mkdir(parents=True)
    (jd / "candidates.json").write_text(json.dumps({"candidates": [
        {"n": 1, "start": 10.0, "end": 50.0, "score": 5.0, "text": "trecho", "why": ["chat spike"],
         "llm": {"total_score": 70, "hook_title": "olha"}}]}), encoding="utf-8")
    return jd


def _redirect(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "JOBS", tmp_path / "jobs")
    monkeypatch.setattr(server.common, "JOBS", tmp_path / "jobs")
    monkeypatch.setattr(server, "ROOT", tmp_path)


def test_split_at_midpoint_preserved(tmp_path, monkeypatch):
    """The existing retitle 'dividir ao meio' behavior stays exactly as it was."""
    _redirect(tmp_path, monkeypatch)
    jd = _job(tmp_path)
    r = server.split_moment(jd, 1, at=30.0)
    assert r["ok"] and r["new_n"] == 2
    cands = json.loads((jd / "candidates.json").read_text(encoding="utf-8"))["candidates"]
    assert [c["start"] for c in cands] == [10.0, 30.0] and [c["end"] for c in cands] == [30.0, 50.0]
    assert cands[1]["why"] == ["divisão manual"]


def test_bounds_create_variant(tmp_path, monkeypatch):
    """The player's 'salvar como novo momento' clones the moment with operator-adjusted
    limits (spec D3/G5) — the original moment stays untouched."""
    _redirect(tmp_path, monkeypatch)
    jd = _job(tmp_path)
    r = server.split_moment(jd, 1, start=15.0, end=35.0)
    assert r["ok"] and r["new_n"] == 2
    cands = json.loads((jd / "candidates.json").read_text(encoding="utf-8"))["candidates"]
    assert len(cands) == 2
    assert cands[0]["start"] == 10.0 and cands[0]["end"] == 50.0  # original intact
    assert cands[1]["start"] == 15.0 and cands[1]["end"] == 35.0
    assert cands[1]["why"] == ["ajuste manual no player"]


def test_bounds_outside_moment_rejected(tmp_path, monkeypatch):
    _redirect(tmp_path, monkeypatch)
    jd = _job(tmp_path)
    try:
        server.split_moment(jd, 1, start=45.0, end=90.0)  # start past the moment's end
        raise AssertionError("expected ValueError")
    except ValueError as e:
        assert "limites" in str(e) or "momento" in str(e)


def test_at_and_bounds_rejected_together(tmp_path, monkeypatch):
    _redirect(tmp_path, monkeypatch)
    jd = _job(tmp_path)
    try:
        server.split_moment(jd, 1, at=30.0, start=15.0, end=35.0)
        raise AssertionError("expected ValueError")
    except ValueError:
        pass

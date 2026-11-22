import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import spike_feedback


def _patch(tmp_path, monkeypatch):
    monkeypatch.setattr(spike_feedback, "FEEDBACK_PATH", tmp_path / "spike_feedback.json")


def test_record_dedup_and_cap(tmp_path, monkeypatch):
    """Same job+utc+offset keeps one row, with the LAST verdict written."""
    _patch(tmp_path, monkeypatch)
    spike_feedback.record("job-a", "2026-11-20 14:00:00", "12:05", "ok")
    rows = spike_feedback.record("job-a", "2026-11-20 14:00:00", "12:05", "fp")
    assert len(rows) == 1
    assert rows[0]["verdict"] == "fp"
    assert spike_feedback.load()[0]["verdict"] == "fp"


def test_record_rejects_bad_verdict(tmp_path, monkeypatch):
    _patch(tmp_path, monkeypatch)
    try:
        spike_feedback.record("job-a", "2026-11-20 14:00:00", "12:05", "x")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_context_both_polarities(tmp_path, monkeypatch):
    """fp examples ask the LLM to sieve lookalikes; ok examples ask to keep them."""
    _patch(tmp_path, monkeypatch)
    assert spike_feedback.context() is None  # no file yet
    spike_feedback.record("job-a", "2026-11-20 14:00:00", "12:05", "fp")
    spike_feedback.record("job-b", "2026-11-20 15:10:00", "3:44", "ok")
    spike_feedback.record("job-b", "2026-11-20 15:22:00", "7:01", "ok")
    ctx = spike_feedback.context()
    low = (ctx or "").lower()
    assert ctx and "falso positivo" in low and "confirmados" in low
    assert "14:00" in ctx and "15:10" in ctx

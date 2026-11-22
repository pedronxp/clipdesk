import json, sys, urllib.error
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import rank_llm


REPLY = json.dumps({
    "candidates": [{"id": "c1", "hook_score": 20, "engagement_score": 15,
                    "value_score": 10, "shareability_score": 12,
                    "hook_type": "afirmação", "hook_title": "olha o que apareceu",
                    "reasoning": "ok"}],
    "summary": "ok"})


def _make_job(tmp_path):
    (tmp_path / "candidates.json").write_text(json.dumps({
        "candidates": [{"n": 1, "start": 10, "end": 40, "score": 5.0,
                        "text": "olha isso", "why": ["audio spike"]}],
        "info": {"channel": "debug", "url": "https://twitch.tv/debug"}}), encoding="utf-8")


def test_retry_sleeps_and_recovers_after_transient_llm_failure(monkeypatch, tmp_path):
    """A transient endpoint failure must sleep and retry, not crash with NameError."""
    _make_job(tmp_path)
    monkeypatch.setattr(sys, "argv", ["rank_llm.py", str(tmp_path)])
    calls = []

    def flaky_call_llm(cfg, user, timeout=120):
        calls.append(1)
        if len(calls) == 1:
            raise urllib.error.URLError("transient network failure")
        return REPLY

    monkeypatch.setattr(rank_llm, "call_llm", flaky_call_llm)
    sleeps = []
    monkeypatch.setattr(rank_llm.time, "sleep", lambda s: sleeps.append(s))

    rc = rank_llm.main()

    assert rc == 0
    assert len(calls) == 2          # first attempt failed, second succeeded
    assert sleeps == [2]            # retried once after the documented 2 s backoff
    data = json.loads((tmp_path / "candidates.json").read_text(encoding="utf-8"))
    assert data["candidates"][0]["llm"]["total_score"] == 57  # plain sum recomputed locally
    assert data["llm_summary"]["summary"] == "ok"

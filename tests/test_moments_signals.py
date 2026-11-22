"""Signal-level guards for the moments engine (chunked audio, speech-aware baseline, PT-BR hype)
and the rank_llm window-refinement contract (adj_start/adj_end, tolerant JSON)."""
import json, sys, wave
from pathlib import Path
import numpy as np
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import moments
import rank_llm


def _write_wav(path, samples, sr=16000):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(samples.astype(np.int16).tobytes())


def test_audio_rms_chunked_matches_whole_array(tmp_path):
    """The 10-minute chunk reader must equal the old whole-array math (same trailing-partial-second drop)."""
    rng = np.random.default_rng(7)
    x = (rng.standard_normal(16000 * 602 + 400) * 3000).astype(np.float64)  # 602.025 s
    _write_wav(tmp_path / "audio.wav", x)
    _, db = moments.audio_rms(tmp_path / "video.mp4", tmp_path)  # wav exists: ffmpeg is skipped
    xf = x.astype(np.int16).astype(np.float32) / 32768  # what the wav actually holds
    n = len(xf) // 16000
    rms = np.sqrt((xf[: n * 16000].reshape(n, 16000) ** 2).mean(1) + 1e-9)
    assert len(db) == 602
    assert np.allclose(db, 20 * np.log10(rms), atol=1e-6)


def test_loudness_spikes_ignore_silence_baseline():
    """Speech at the same level before and after a long quiet stretch must score alike — the old
    30 s median collapsed through the silence and turned the next sentence into a fake scream."""
    db = np.full(900, -20.0)
    db[300:600] = -60.0  # quiet stretch
    db[850] = 0.0        # one real spike
    ex = moments.loudness_spikes(db)
    assert abs(ex[610]) < 1.0    # right after the silence: no false spike
    assert abs(ex[299]) < 1.0    # right before it either
    assert ex[850] > 15.0        # the real spike stands out


def test_dampen_edges_greetings_and_goodbyes():
    score = np.ones(1200)
    moments.dampen_edges(score, 1200)
    assert score[0] == 0.3 and score[-1] == 0.3
    assert score[300] == 1.0 and score[900] == 1.0  # the middle is untouched


def test_dampen_edges_short_video_untouched():
    score = np.ones(300)
    assert np.all(moments.dampen_edges(score.copy(), 300) == 1.0)


def test_hype_matches_pt_br_reactions():
    for w in ("CARALHO", "porrada", "kkkk", "hahaha", "nossa", "putz", "caramba", "eita",
              "vish", "caraca", "rapaz", "socorro", "puta", "pqp"):
        assert moments.HYPE.search(w), w
    # no false positives on ordinary words
    for w in ("trabalho", "nossas", "computador", "manobrar"):
        assert not moments.HYPE.search(w), w


# ---------------------------------------------------------------- piecewise transcription

class _FakeInfo:
    language, language_probability, duration = "en", 0.99, 0


class _FakeWord:
    def __init__(self, w, s, e):
        self.word, self.start, self.end = w, s, e


class _FakeSeg:
    def __init__(self, words, end):
        self.words, self.end = words, end


def test_transcribe_pieces_cover_audio_once_and_resume(tmp_path, monkeypatch):
    """1250 s -> 3 pieces. The stubbed model emits one word per second of the audio it is handed
    (overlap included); correct clipping means every second of the live comes back exactly once.
    A second run with transcript.json gone must resume from the piece files without re-transcribing."""
    import types
    _write_wav(tmp_path / "audio.wav", np.zeros(16000 * 1250))  # 20:50 of silence
    total = 1250.0
    seen = []

    class _FakeModel:
        def __init__(self, *a, **k):
            pass

        def transcribe(self, pcm, language=None, **kw):
            k = len(seen)
            seen.append(k)
            a = max(0.0, k * moments.PIECE - moments.OVERLAP)
            b = min(total, (k + 1) * moments.PIECE + moments.OVERLAP)
            # real whisper returns piece-relative timestamps; the stub must too
            segs = [_FakeSeg([_FakeWord(f"w{int(t)}", float(t) - a, t - a + 0.9) for t in range(int(a), int(b))], b - a)]
            return segs, _FakeInfo()

    monkeypatch.setattr(sys, "modules", {**sys.modules, "faster_whisper": types.SimpleNamespace(WhisperModel=_FakeModel)})
    tr = moments.transcribe(tmp_path / "audio.wav", tmp_path)
    assert [w["w"] for w in tr["words"]] == [f"w{i}" for i in range(int(total))]  # every second, once
    assert tr["language"] == "en"
    assert len(seen) == 3  # 1250 s -> 3 pieces

    (tmp_path / "transcript.json").unlink()
    seen.clear()
    tr2 = moments.transcribe(tmp_path / "audio.wav", tmp_path)
    assert tr2["words"] == tr["words"]
    assert seen == []  # resumed purely from the transcribe/NNNN.json piece files


# ---------------------------------------------------------------- rank_llm refinements

def test_to_secs():
    assert rank_llm.to_secs("2:05") == 125.0
    assert rank_llm.to_secs("1:02:03") == 3723.0
    assert rank_llm.to_secs(88) == 88.0
    assert rank_llm.to_secs("0:19") == 19.0
    assert rank_llm.to_secs(None) is None
    assert rank_llm.to_secs("bananas") is None


def test_refine_window_clamps_and_rejects():
    c = {"start": 100.0, "end": 160.0}
    assert rank_llm.refine_window(c, 95.0, 150.0) == (95.0, 150.0)      # trim the dead tail
    assert rank_llm.refine_window(c, None, 130.0) == (100.0, 130.0)     # one edge only
    assert rank_llm.refine_window(c, None, None) is None                # no proposal
    assert rank_llm.refine_window(c, 118.0, 112.0) is None              # inverted
    assert rank_llm.refine_window(c, 100.0, 110.0) is None              # too short
    assert rank_llm.refine_window(c, 400.0, 500.0) is None              # far outside the slack


def test_parse_reply_tolerates_fences_comments_and_trailing_commas():
    rows, summary = rank_llm.parse_reply(
        '```json\n{"candidates": [{"id": "c1", "hook_score": 20,}], // one candidate\n'
        '"summary": "ok"}\n```')
    assert rows[0]["id"] == "c1"
    assert summary == "ok"


def test_prompt_includes_timestamped_transcript_once_per_moment():
    cands = [
        {"n": 1, "_cid": "c1", "start": 60, "end": 120, "peak": 90, "score": 5.0, "text": "a", "why": []},
        {"n": 2, "_cid": "c2", "start": 60, "end": 90, "peak": 90, "score": 4.0, "text": "a", "why": []},
    ]
    words = [{"w": "olha", "s": 80, "e": 80.4}, {"w": "isso.", "s": 81, "e": 81.8}]
    prompt = rank_llm.build_user_prompt(cands, {"title": "t"}, "", None, words=words)
    assert "[1:20]" in prompt and "olha isso." in prompt
    assert prompt.count("[1:20]") == 1          # tier takes share one transcript
    assert "same moment as another take" in prompt


def test_window_refinement_rewrites_start_end(tmp_path, monkeypatch):
    (tmp_path / "candidates.json").write_text(json.dumps({
        "candidates": [{"n": 1, "start": 10, "end": 70, "score": 5.0, "text": "oi", "why": []}],
        "info": {}}), encoding="utf-8")
    (tmp_path / "transcript.json").write_text(json.dumps({"language": "pt", "words": [
        {"w": "oi", "s": 8, "e": 8.4}, {"w": "gente", "s": 11, "e": 11.5},
        {"w": "olha", "s": 20, "e": 20.5}, {"w": "isso.", "s": 20.6, "e": 21.2}]}), encoding="utf-8")
    reply = json.dumps({"candidates": [{"id": "c1", "hook_score": 20, "engagement_score": 15,
                                        "value_score": 10, "shareability_score": 12,
                                        "hook_type": "pergunta", "hook_title": "olha isso",
                                        "reasoning": "ok", "adj_start": "0:19", "adj_end": "1:05"}],
                        "summary": "ok"})
    seen = {}
    monkeypatch.setattr(rank_llm, "call_llm", lambda cfg, user, timeout=120: seen.update(user=user) or reply)
    monkeypatch.setattr(rank_llm.common, "llm_config", lambda: {"base": "x", "key": "k", "model": "m"})
    monkeypatch.setattr(rank_llm.performance, "context", lambda: "")
    monkeypatch.setattr(rank_llm.spike_feedback, "context", lambda: None)
    monkeypatch.setattr(sys, "argv", ["rank_llm.py", str(tmp_path)])
    assert rank_llm.main() == 0
    data = json.loads((tmp_path / "candidates.json").read_text(encoding="utf-8"))
    c = data["candidates"][0]
    assert c["start"] == 19.0 and c["end"] == 65.0          # adjusted window
    assert c["signal_start"] == 10 and c["signal_end"] == 70  # original kept
    assert data["llm_summary"]["windows_refined"] == 1
    assert "[0:08] oi" in seen["user"] and "[0:20] olha isso." in seen["user"]  # transcript reached the model

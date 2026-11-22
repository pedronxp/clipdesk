import json, sys
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "dashboard"))
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import server


CAND = {"n": 1, "start": 42.0, "end": 76.0, "dur": 34.0, "score": 5.0, "text": "olha o carro"}


def _job(tmp_path, source="live", clip=False, thumb=False, name="job-m"):
    jd = tmp_path / "jobs" / name
    jd.mkdir(parents=True, exist_ok=True)
    (jd / "candidates.json").write_text(json.dumps({"candidates": [CAND]}), encoding="utf-8")
    if source == "live":
        (jd / "live").mkdir(exist_ok=True)
        (jd / "live" / "source_cfr.mp4").write_bytes(b"x")
    elif source == "root":
        (jd / "source.mp4").write_bytes(b"x")
    if clip:
        (jd / "edits").mkdir(exist_ok=True)
        (jd / "edits" / "clip_1.mp4").write_bytes(b"x")
    if thumb:
        (jd / "edits").mkdir(exist_ok=True)
        (jd / "edits" / "thumb_1.jpg").write_bytes(b"x")
    return jd


def _redirect(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "JOBS", tmp_path / "jobs")
    monkeypatch.setattr(server.common, "JOBS", tmp_path / "jobs")
    monkeypatch.setattr(server, "LOGS", tmp_path / "logs")


def test_thumb_from_source_frame(tmp_path, monkeypatch):
    """Real frame extraction: thumbs/moment_<n>_s<start>.jpg (spec D9 file naming)."""
    jd = _job(tmp_path)
    calls = []

    def fake_run(cmd, *a, **k):
        calls.append(cmd)
        Path(cmd[-1]).write_bytes(b"jpg")  # real ffmpeg creates the file
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(server, "subprocess", SimpleNamespace(run=fake_run))
    out, kind = server.moment_thumb(jd, CAND)
    assert out and out.exists() and out.name == "moment_1_s42.jpg" and kind == "source"
    assert len(calls) == 1


def test_thumb_cache_hits_file(tmp_path, monkeypatch):
    """Second call serves the cached file — no second ffmpeg (Review Focus 2)."""
    jd = _job(tmp_path)
    n = {"c": 0}

    def fake_run(cmd, *a, **k):
        n["c"] += 1
        Path(cmd[-1]).write_bytes(b"jpg")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(server, "subprocess", SimpleNamespace(run=fake_run))
    server.moment_thumb(jd, CAND)
    server.moment_thumb(jd, CAND)
    assert n["c"] == 1


def test_thumb_in_progress_returns_fast(tmp_path, monkeypatch):
    """A nested request while one extraction is running must NOT spawn a second ffmpeg."""
    jd = _job(tmp_path)
    n = {"c": 0}

    def slow_run(cmd, *a, **k):
        n["c"] += 1
        server.moment_thumb(jd, CAND)  # re-entrant request mid-extraction
        Path(cmd[-1]).write_bytes(b"jpg")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(server, "subprocess", SimpleNamespace(run=slow_run))
    server.moment_thumb(jd, CAND)
    assert n["c"] == 1


def test_fallback_clip_thumb(tmp_path, monkeypatch):
    """No source but a cut exists: serve the cut's thumb (D10, labeled at the card)."""
    jd = _job(tmp_path, source=None, clip=True)

    def fake_ensure(mp4, target):
        target.write_bytes(b"x")  # py3.13: write_bytes returns the byte count — never truthy-test it
        return target

    monkeypatch.setattr(server, "ensure_thumb", fake_ensure)
    out, kind = server.moment_thumb(jd, CAND)
    assert out and out.name == "thumb_1.jpg" and kind == "clip"


def test_none_when_nothing(tmp_path, monkeypatch):
    """No source and no cut: None — the card goes gray with the time (D10)."""
    jd = _job(tmp_path, source=None)
    assert server.moment_thumb(jd, CAND) is None


def test_thumb_kind_without_ffmpeg(tmp_path, monkeypatch):
    """The payload needs the chain decision without ever extracting (pre-flight ruling)."""
    assert server.thumb_kind_for(_job(tmp_path, name="k1"), 1) == "source"
    assert server.thumb_kind_for(_job(tmp_path, source=None, clip=True, name="k2"), 1) == "clip"
    assert server.thumb_kind_for(_job(tmp_path, source=None, name="k3"), 1) is None


def test_reanalysis_clears_thumbs(tmp_path, monkeypatch):
    """New analysis of the same source wipes thumbs — old frames never come back (D9)."""
    jd = _job(tmp_path, source="root")
    thumbs = jd / "thumbs"
    thumbs.mkdir()
    (thumbs / "moment_1_s999.jpg").write_bytes(b"old")
    _redirect(tmp_path, monkeypatch)
    monkeypatch.setattr(server, "_spawn_detached", lambda cmd, fh, env: SimpleNamespace(pid=0))
    monkeypatch.setattr(server, "creation_run", lambda jd: None)
    monkeypatch.setattr(server.common, "job_dir", lambda src: jd)
    server.start_creation_analysis(str(jd / "source.mp4"), "src", 6, 90, {})
    assert not thumbs.exists()


def test_moment_cand_missing_candidates_json(tmp_path, monkeypatch):
    """A live capture job has no candidates.json — the endpoint must 404, not crash
    (found in the smoke test with real data)."""
    jd = _job(tmp_path, source="live", name="no-cands")
    jd / "candidates.json"
    if (jd / "candidates.json").exists():
        (jd / "candidates.json").unlink()
    monkeypatch.setattr(server, "JOBS", tmp_path / "jobs")
    assert server._moment_cand(jd, 1) is None
    assert server._moment_cand(jd, 99) is None


def test_source_resolved_via_candidates_reference(tmp_path, monkeypatch):
    """D0/file-analysis jobs keep their source in ANOTHER job dir (candidates.json['source'])
    — the thumb chain must resolve it, not claim 'fonte removida' (review I3)."""
    real = tmp_path / "jobs" / "job-live" / "live"
    real.mkdir(parents=True)
    (real / "source_cfr.mp4").write_bytes(b"x")
    jd = _job(tmp_path, source=None, name="job-moments")
    data = json.loads((jd / "candidates.json").read_text(encoding="utf-8"))
    data["source"] = str(real / "source_cfr.mp4")
    (jd / "candidates.json").write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(server, "JOBS", tmp_path / "jobs")
    assert server.thumb_kind_for(jd, 1) == "source"
    calls = []

    def fake_run(cmd, *a, **k):
        calls.append(cmd)
        Path(cmd[-1]).write_bytes(b"jpg")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(server, "subprocess", SimpleNamespace(run=fake_run))
    out, kind = server.moment_thumb(jd, CAND)
    src_arg = calls[0][calls[0].index("-i") + 1]  # the video ffmpeg actually read
    assert kind == "source" and out.exists()
    assert src_arg == str(real / "source_cfr.mp4")

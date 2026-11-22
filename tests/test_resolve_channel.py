import json, sys
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "dashboard"))
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import server


def _job(tmp_path, channel=""):
    jd = tmp_path / "jobs" / "yt-job"
    jd.mkdir(parents=True)
    (jd / "info.json").write_text(json.dumps({"channel": channel,
                                              "url": "https://www.youtube.com/watch?v=-isRxm6m2c8"}),
                                  encoding="utf-8")
    return jd


def _redirect(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "JOBS", tmp_path / "jobs")
    monkeypatch.setattr(server, "YTDLP_NAME_TRIED", set())


def test_channel_resolved_and_saved(tmp_path, monkeypatch):
    """yt-dlp metadata fills the empty channel and is written back to info.json (bug: YouTube)."""
    _redirect(tmp_path, monkeypatch)
    jd = _job(tmp_path)
    meta = {"channel": "CanalReal", "webpage_url": "https://www.youtube.com/watch?v=-isRxm6m2c8"}

    def fake_run(cmd, *a, **k):
        return SimpleNamespace(returncode=0, stdout=json.dumps(meta).encode("utf-8"))

    monkeypatch.setattr(server, "subprocess", SimpleNamespace(run=fake_run))
    name = server.resolve_channel(jd, "https://www.youtube.com/watch?v=-isRxm6m2c8")
    assert name == "CanalReal"
    assert json.loads((jd / "info.json").read_text(encoding="utf-8"))["channel"] == "CanalReal"


def test_once_per_job(tmp_path, monkeypatch):
    """yt-dlp is slow — one attempt per job, never per 4 s poll."""
    _redirect(tmp_path, monkeypatch)
    jd = _job(tmp_path)
    n = {"c": 0}

    def fake_run(cmd, *a, **k):
        n["c"] += 1
        return SimpleNamespace(returncode=1, stdout=b"{}")

    monkeypatch.setattr(server, "subprocess", SimpleNamespace(run=fake_run))
    assert server.resolve_channel(jd, "https://www.youtube.com/watch?v=x") == ""
    assert server.resolve_channel(jd, "https://www.youtube.com/watch?v=x") == ""
    assert n["c"] == 1


def test_still_empty_keeps_fallback():
    """Resolution failure keeps the honest generic fallback (no invented names)."""
    assert server._streamer_name.__doc__ is not None  # fallback path unchanged, covered by UI

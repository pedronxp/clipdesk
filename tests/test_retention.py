import json, os, sys, time
from datetime import datetime, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "dashboard"))
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import server


def _capture(tmp_path, name, *, processed=True, ended_h=25.0, live_json=True):
    jd = tmp_path / "jobs" / name
    (jd / "live").mkdir(parents=True)
    (jd / "live" / "seg_000.ts").write_bytes(b"x")
    (jd / "live" / "spike_1.mp4").write_bytes(b"x")
    if processed:
        mon = {"preset": "react-br", "clips": [{"n": 1}]}
        if not live_json:
            mon["error"] = "Error opening output files: Invalid argument"
            mon["failed_ts"] = datetime.fromtimestamp(time.time() - ended_h * 3600, timezone.utc).isoformat(timespec="seconds")
        (jd / "monitor.json").write_text(json.dumps(mon), encoding="utf-8")
    if live_json:
        old = time.time() - ended_h * 3600
        (jd / "live" / "live.json").write_text(json.dumps({"ended_ts": old}), encoding="utf-8")
    return jd


def _redirect(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "JOBS", tmp_path / "jobs")
    monkeypatch.setattr(server.common, "JOBS", tmp_path / "jobs")
    monkeypatch.setattr(server, "PIPELINE_QUEUE", tmp_path / "state" / "pipeline_queue.json")
    monkeypatch.setattr(server.voids, "STATE", tmp_path / "state")
    monkeypatch.setattr(server.voids, "DISCARDED", tmp_path / "state" / "discarded.json")
    monkeypatch.setattr(server, "load_watchlist", lambda: [])
    monkeypatch.setattr(server.voids, "prune_voids", lambda *a, **k: [])
    return tmp_path / "jobs"


def test_processed_capture_deleted_after_24h(tmp_path, monkeypatch):
    """Operator decision 2026-11-21: processed capture is deleted WHOLE after 24h."""
    jobs = _redirect(tmp_path, monkeypatch)
    jd = _capture(tmp_path, "job-done", ended_h=25.0)
    server.retention_sweep()
    assert not jd.exists()
    disc = json.loads((tmp_path / "state" / "discarded.json").read_text(encoding="utf-8"))
    assert disc[0]["reason"] == "retenção 24h" and disc[0]["job"] == "job-done"


def test_recent_capture_kept(tmp_path, monkeypatch):
    _redirect(tmp_path, monkeypatch)
    jd = _capture(tmp_path, "job-fresh", ended_h=2.0)
    server.retention_sweep()
    assert jd.exists()


def test_unprocessed_capture_kept(tmp_path, monkeypatch):
    """No monitor.json: the watcher/pipeline owns it — never swept by retention."""
    _redirect(tmp_path, monkeypatch)
    jd = _capture(tmp_path, "job-pending", processed=False)
    server.retention_sweep()
    assert jd.exists()


def test_failed_pipeline_also_swept(tmp_path, monkeypatch):
    """monitor.json {"error"} is still a finished pipeline: the operator wants those gone too."""
    _redirect(tmp_path, monkeypatch)
    jd = _capture(tmp_path, "job-err", live_json=False)
    server.retention_sweep()
    assert not jd.exists()


def test_truncated_monitor_json_does_not_crash_the_pruner(tmp_path, monkeypatch):
    """A 0-byte monitor.json (crashed write) used to raise JSONDecodeError on every pruner
    tick, so retention and the watchlist cleanup never ran."""
    _redirect(tmp_path, monkeypatch)
    jd = _capture(tmp_path, "job-corrupt", ended_h=25.0)
    (jd / "monitor.json").write_text("", encoding="utf-8")
    server.void_pruner_tick()
    assert not jd.exists()


def test_running_or_queued_capture_kept(tmp_path, monkeypatch):
    _redirect(tmp_path, monkeypatch)
    jd = _capture(tmp_path, "job-queued", ended_h=48.0)
    monkeypatch.setattr(server, "pipeline_for", lambda jid: {"state": "queued", "position": 0})
    server.retention_sweep()
    assert jd.exists()

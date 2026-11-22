import json, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import common
import post_capture


def _live_job(tmp_path, n_segs=2):
    """A fake ended capture: jobs/<id>/live/ with raw segments, no monitor.json."""
    jobs = tmp_path / "jobs"
    live_jd = jobs / "job-test"
    ld = live_jd / "live"
    ld.mkdir(parents=True)
    for i in range(n_segs):
        (ld / f"seg_{i:03d}.ts").write_bytes(b"x")
    (live_jd / "info.json").write_text(json.dumps({"channel": "debug", "url": "https://twitch.tv/x"}),
                                        encoding="utf-8")
    return live_jd


def _redirect(monkeypatch, tmp_path):
    monkeypatch.setattr(common, "JOBS", tmp_path / "jobs")  # job_dir() reads common.JOBS
    monkeypatch.setattr(post_capture, "JOBS", tmp_path / "jobs")
    monkeypatch.setattr(post_capture, "rebuild_cfr", lambda segs, src: None)  # no ffmpeg in tests
    monkeypatch.setattr(post_capture, "ffprobe", lambda f: {"format": {"duration": "10.0"}})
    discarded = []
    monkeypatch.setattr(post_capture.voids, "discard",
                        lambda folder, reason="": discarded.append(str(folder)) or {"job": folder.name, "mb": 0})
    return discarded


def test_write_monitor_json_error_schema(tmp_path):
    live_jd = tmp_path / "job-x"
    (live_jd / "live").mkdir(parents=True)
    post_capture.write_monitor_json(live_jd, {"preset": "react-br", "error": "boom", "failed_ts": "now"})
    back = common.load_json(live_jd / "monitor.json")
    assert back["error"] == "boom" and back["preset"] == "react-br"


def test_run_no_segments_discards(tmp_path, monkeypatch):
    """No seg_*.ts at all: a true void — discard, no monitor.json, return 1."""
    live_jd = _live_job(tmp_path, n_segs=0)
    _redirect(monkeypatch, tmp_path)
    rc = post_capture.run(live_jd)
    assert rc == 1
    assert not (live_jd / "monitor.json").exists()


def test_run_failure_writes_error_monitor(tmp_path, monkeypatch):
    """Analysis failure keeps the capture on disk with monitor.json {"error": ...} (spec D0)."""
    live_jd = _live_job(tmp_path)
    _redirect(monkeypatch, tmp_path)
    monkeypatch.setattr(post_capture, "run_step", lambda cmd, title: 1)  # every step fails
    rc = post_capture.run(live_jd, top=3)
    assert rc == 1
    meta = common.load_json(live_jd / "monitor.json")
    assert meta.get("error") and meta.get("failed_ts")
    assert (live_jd / "live" / "seg_000.ts").exists()  # material never deleted on failure


def test_draft_title_short_text():
    assert post_capture.draft_title("abc", 3) == "Momento 3 de GTA 6"

def test_run_skips_while_recording(tmp_path, monkeypatch):
    """A resumed capture must never be discarded under a live recorder (review Important 4):
    run() exits early — no discard, no monitor.json — and the watcher re-queues after the end."""
    live_jd = _live_job(tmp_path)
    discarded = _redirect(monkeypatch, tmp_path)
    monkeypatch.setattr(post_capture.voids, "recording_running", lambda jd: True)
    assert post_capture.run(live_jd) == 1
    assert discarded == []
    assert not (live_jd / "monitor.json").exists()


def test_already_processed_fresh_monitor(tmp_path):
    live = tmp_path / "j" / "live"
    live.mkdir(parents=True)
    (live / "seg_000.ts").write_bytes(b"x")
    mon = tmp_path / "j" / "monitor.json"
    time.sleep(0.02)
    mon.write_text(json.dumps({"clips": [{"n": 1}]}), encoding="utf-8")
    assert post_capture.already_processed(tmp_path / "j") is True


def test_already_processed_error_record_rerunnable(tmp_path):
    live = tmp_path / "j" / "live"
    live.mkdir(parents=True)
    (live / "seg_000.ts").write_bytes(b"x")
    (tmp_path / "j" / "monitor.json").write_text(json.dumps({"error": "x"}), encoding="utf-8")
    assert post_capture.already_processed(tmp_path / "j") is False


def test_already_processed_no_monitor(tmp_path):
    live = tmp_path / "j" / "live"
    live.mkdir(parents=True)
    (live / "seg_000.ts").write_bytes(b"x")
    assert post_capture.already_processed(tmp_path / "j") is False

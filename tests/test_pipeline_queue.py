import json, sys, time
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "dashboard"))
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import server


def _fake_job(tmp_path, name="job-a", n_segs=2, monitor_json=False):
    jd = tmp_path / "jobs" / name
    ld = jd / "live"
    ld.mkdir(parents=True, exist_ok=True)
    for i in range(n_segs):
        f = ld / f"seg_{i:03d}.ts"
        f.write_bytes(b"x")
    if monitor_json:
        (jd / "monitor.json").write_text(json.dumps({"clips": [{"n": 1}]}), encoding="utf-8")
    return jd


def _redirect(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "PIPELINE_QUEUE", tmp_path / "pipeline_queue.json")
    monkeypatch.setattr(server, "JOBS", tmp_path / "jobs")
    monkeypatch.setattr(server, "LOGS", tmp_path / "logs")
    monkeypatch.setattr(server, "watch_running", lambda row, jd=None: False)
    return tmp_path / "logs"


def _row(jid="job-a"):
    return {"id": jid, "url": "https://twitch.tv/x", "pid": 0, "minutes": 60, "started": None}


def test_enqueue_dedup(tmp_path, monkeypatch):
    _redirect(tmp_path, monkeypatch)
    assert server.enqueue_pipeline("job-a", 3, "react-br") is True
    assert server.enqueue_pipeline("job-a", 3, "react-br") is False  # already queued
    rows = server.pipeline_rows()
    assert len(rows) == 1 and rows[0]["state"] == "queued"


def test_pipeline_for_position(tmp_path, monkeypatch):
    _redirect(tmp_path, monkeypatch)
    server.enqueue_pipeline("job-a", 3, "react-br")
    server.enqueue_pipeline("job-b", 3, "react-br")
    assert server.pipeline_for("job-a") == {"state": "queued", "position": 0}
    assert server.pipeline_for("job-b") == {"state": "queued", "position": 1}
    assert server.pipeline_for("job-z") is None


def test_should_enqueue_partial_capture(tmp_path, monkeypatch):
    """Capture with segs, no monitor.json, not running → enqueue (Review Focus 1)."""
    _redirect(tmp_path, monkeypatch)
    jd = _fake_job(tmp_path)
    assert server.should_enqueue(_row(), time.time()) is True


def test_should_enqueue_old_capture(tmp_path, monkeypatch):
    _redirect(tmp_path, monkeypatch)
    jd = _fake_job(tmp_path)
    old = time.time() - 3 * 86400
    for f in jd.rglob("*"):
        import os
        os.utime(f, (old, old))
    assert server.should_enqueue(_row(), time.time()) is False  # 48h window


def test_should_enqueue_with_monitor_json(tmp_path, monkeypatch):
    _redirect(tmp_path, monkeypatch)
    _fake_job(tmp_path, monitor_json=True)
    assert server.should_enqueue(_row(), time.time()) is False


def test_worker_spawns_and_advances(tmp_path, monkeypatch):
    _redirect(tmp_path, monkeypatch)
    _fake_job(tmp_path, name="job-a")
    spawned = []
    monkeypatch.setattr(server, "_spawn_detached",
                        lambda cmd, fh, env: spawned.append(cmd) or SimpleNamespace(pid=4321))
    monkeypatch.setattr(server, "pid_alive", lambda pid: pid == 4321)
    server.enqueue_pipeline("job-a", 3, "react-br")
    server.pipeline_worker_tick()
    rows = server.pipeline_rows()
    assert rows[0]["state"] == "running" and rows[0]["pid"] == 4321
    assert any("post_capture.py" in str(x) and "job-a" in [str(y) for y in x] for x in spawned)
    # next tick with a dead pid: head removed, queue empty
    monkeypatch.setattr(server, "pid_alive", lambda pid: False)
    server.pipeline_worker_tick()
    assert server.pipeline_rows() == []


def test_pruner_protects_pipeline_jobs(tmp_path, monkeypatch):
    """Queued/running pipeline jobs are never handed to prune_voids (Review Focus 2)."""
    _redirect(tmp_path, monkeypatch)
    server.enqueue_pipeline("job-a", 3, "react-br")
    captured = {}
    monkeypatch.setattr(server.voids, "prune_voids",
                        lambda jobs_root, grace_minutes=0, running_ids=(), dry_run=False:
                        captured.update(running=set(running_ids)) or [])
    monkeypatch.setattr(server, "load_watchlist", lambda: [])
    server.void_pruner_tick()
    assert "job-a" in captured["running"]


def test_pipeline_queue_no_lost_update(tmp_path, monkeypatch):
    """Watcher enqueues racing the worker's read-modify-write must never lose a row
    (review Important 5: the RMW on pipeline_queue.json needs a lock)."""
    import threading
    _redirect(tmp_path, monkeypatch)
    _fake_job(tmp_path, name="job-a")
    monkeypatch.setattr(server, "_spawn_detached",
                        lambda cmd, fh, env: SimpleNamespace(pid=4321))
    monkeypatch.setattr(server, "pid_alive", lambda pid: pid == 4321)
    server.enqueue_pipeline("job-a", 3, "react-br")
    errs = []

    def watcher_writes():
        try:
            for i in range(60):
                server.enqueue_pipeline(f"w{i}", 3, "react-br")
        except Exception as e:
            errs.append(e)

    t = threading.Thread(target=watcher_writes)
    t.start()
    for _ in range(60):
        server.pipeline_worker_tick()
    t.join()
    assert not errs
    rows = server.pipeline_rows()
    jobs = [r["job"] for r in rows]
    assert len(jobs) == len(set(jobs)), f"duplicate rows: {[j for j in jobs if jobs.count(j) > 1]}"
    assert len(rows) == 61, f"lost rows: {len(rows)}/61"

def test_should_enqueue_new_session_after_monitor(tmp_path, monkeypatch):
    """A re-capture on the same URL must be analyzed even though monitor.json exists from the
    previous session (review Important 1): any segment newer than the analysis = new session."""
    _redirect(tmp_path, monkeypatch)
    jd = _fake_job(tmp_path, monitor_json=True)
    time.sleep(0.02)
    (jd / "live" / "seg_999.ts").write_bytes(b"x")  # new session recorded after the analysis
    assert server.should_enqueue(_row()) is True


def test_worker_head_done_by_monitor_sentinel(tmp_path, monkeypatch):
    """A reused PID can look alive forever; the terminal monitor.json (post_capture writes it
    on success AND failure) is the completion sentinel (review Important 2)."""
    _redirect(tmp_path, monkeypatch)
    jd = _fake_job(tmp_path)
    monkeypatch.setattr(server, "pid_alive", lambda pid: True)
    monkeypatch.setattr(server, "_spawn_detached", lambda cmd, fh, env: SimpleNamespace(pid=4242))
    server.enqueue_pipeline("job-a", 3, "react-br")
    server.pipeline_worker_tick()  # spawns the head
    import os as _os
    mj = jd / "monitor.json"
    mj.write_text(json.dumps({"clips": []}), encoding="utf-8")
    ts = time.time() + 1
    _os.utime(mj, (ts, ts))  # certainly newer than queued_ts
    server.pipeline_worker_tick()
    assert server.pipeline_rows() == []


def test_worker_head_stale_running_released(tmp_path, monkeypatch):
    """A hung run (pid alive, no monitor.json, over the ceiling) must not wedge the queue."""
    _redirect(tmp_path, monkeypatch)
    monkeypatch.setattr(server, "pid_alive", lambda pid: True)
    monkeypatch.setattr(server, "_spawn_detached", lambda cmd, fh, env: SimpleNamespace(pid=4242))
    server.enqueue_pipeline("job-a", 3, "react-br")
    server.pipeline_worker_tick()
    rows = server.pipeline_rows()
    old = server.datetime.fromtimestamp(time.time() - server.PIPELINE_STALE_S - 60, server.timezone.utc)
    server.save_pipeline_rows([{**rows[0], "started_ts": old.isoformat()}])
    server.pipeline_worker_tick()
    assert server.pipeline_rows() == []


def test_start_watch_cancels_queue_rows(tmp_path, monkeypatch):
    """Resuming a capture cancels its queued post-capture: the pipeline must never run under
    a live recorder, and live.py deletes old segments on start (review Important 4)."""
    _redirect(tmp_path, monkeypatch)
    monkeypatch.setattr(server, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(server, "WATCHLIST", tmp_path / "state" / "watchlist.json")
    monkeypatch.setattr(server, "_spawn_detached", lambda cmd, fh, env: SimpleNamespace(pid=7))
    jid = server.job_id_for("https://twitch.tv/x")
    server.enqueue_pipeline(jid, 3, "react-br")
    server.start_watch("https://twitch.tv/x", 60)
    assert all(r.get("job") != jid for r in server.pipeline_rows())


def test_reconcile_on_read_releases_dead_head(tmp_path, monkeypatch):
    """A wedged worker must not leave a phantom "processando" card: the read path itself
    releases a running head whose pid is dead (Review Focus — stale running row)."""
    _redirect(tmp_path, monkeypatch)
    server.enqueue_pipeline("job-a", 3, "react-br")
    rows = server.pipeline_rows()
    rows[0].update({"state": "running", "pid": 0})
    server.save_pipeline_rows(rows)
    monkeypatch.setattr(server, "pid_alive", lambda pid: False)
    assert server.pipeline_for("job-a") is None          # leitura já reconcilia
    assert server.pipeline_rows() == []                  # head liberado do arquivo


def test_reconcile_keeps_live_running_head(tmp_path, monkeypatch):
    _redirect(tmp_path, monkeypatch)
    server.enqueue_pipeline("job-a", 3, "react-br")
    rows = server.pipeline_rows()
    rows[0].update({"state": "running", "pid": 123456})
    server.save_pipeline_rows(rows)
    monkeypatch.setattr(server, "pid_alive", lambda pid: True)
    assert server.pipeline_for("job-a") == {"state": "running", "position": 0}
    assert len(server.pipeline_rows()) == 1

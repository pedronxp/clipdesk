"""Void live jobs: captures that ended without producing any video.

Every live capture owns a job folder (jobs/<id>/live/). When the capture ends and
nothing usable came out of it — no final cut (edits/clip_*.mp4) and no raw take
(live/spike_*.mp4) — the folder is a *void*: it only keeps the disk busy. Voids are
deleted instead of being kept around.

A capture that is still recording is never void, even with no output yet, and the
deletion never touches a job that produced a video.
"""
import json
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

from common import pid_alive

STATE = Path(__file__).resolve().parent.parent / "dashboard" / "state"
DISCARDED = STATE / "discarded.json"
RECORDER = "recorder.json"
KEEP_DISCARDED = 60
STALE_SECONDS = 120  # the recorder writes a segment every ~60 s; silence beyond this means it is dead


def live_dir(job_d):
    return Path(job_d) / "live"


def last_activity(job_d):
    """Newest file mtime under live/ (0.0 when the folder is missing)."""
    newest = 0.0
    ld = live_dir(job_d)
    if ld.is_dir():
        for f in ld.glob("*"):
            try:
                newest = max(newest, f.stat().st_mtime)
            except OSError:
                continue
    return newest


def is_live_job(job_d):
    """True for a job folder that belongs to a live capture."""
    return live_dir(job_d).is_dir()


def job_outputs(job_d):
    """Videos that prove the job produced something: final cuts, raw takes and downloaded
    sources (moments.py/analyze_creation leave source videos at the job root — a job that
    still holds one is not a void, or the pruner would destroy a source that took minutes
    to download while the user waits)."""
    job_d = Path(job_d)
    edits = job_d / "edits"
    clips = sorted(edits.glob("clip_*.mp4")) if edits.is_dir() else []
    ld = live_dir(job_d)
    spikes = sorted(ld.glob("spike_*.mp4")) if ld.is_dir() else []
    sources = sorted(job_d.glob("*.mp4")) + sorted(job_d.glob("*.mkv")) + sorted(job_d.glob("*.webm"))
    return {"clips": clips, "spikes": spikes, "sources": sources}


def analysis_running(job_d):
    """True while an analyze_creation.py process (creation.json pid) owns this job."""
    try:
        data = json.loads((Path(job_d) / "creation.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return pid_alive(data.get("pid"))


def recorder_info(job_d):
    """Metadata written by live.py when it owns the capture (pid + start time)."""
    f = live_dir(job_d) / RECORDER
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def recording_running(job_d):
    """True while the recorder that owns this job folder is alive and writing.

    Windows reuses PIDs, so a dead capture can look alive forever if a new process took
    its PID. The PID check is therefore confirmed by recent activity: a healthy capture
    lands a new segment (or at least touches live/) well inside STALE_SECONDS."""
    pid = recorder_info(job_d).get("pid")
    if not pid_alive(pid):
        return False
    return time.time() - last_activity(job_d) < STALE_SECONDS


def ended_at(job_d):
    """When the capture stopped (live.json "ended", else the last file written)."""
    job_d = Path(job_d)
    ended = 0.0
    try:
        meta = json.loads((live_dir(job_d) / "live.json").read_text(encoding="utf-8"))
        ended = float(meta.get("ended_ts") or 0)
    except (OSError, ValueError, TypeError):
        ended = 0.0
    if not ended:
        newest = 0.0
        for f in live_dir(job_d).glob("*"):
            try:
                newest = max(newest, f.stat().st_mtime)
            except OSError:
                continue
        ended = newest
    return ended


def is_empty(job_d):
    """True when the folder holds no video at all (no final cut, no raw take, no source)."""
    out = job_outputs(job_d)
    return not out["clips"] and not out["spikes"] and not out["sources"]


def source_referenced(job_d, jobs_root=None):
    """True when another job's candidates.json points at files inside this folder.
    Deleting such a folder would break re-cutting the moments it fed — its chat and
    spike history are the only way back to them — so it is never treated as a void."""
    job_d = Path(job_d).resolve()
    root = Path(jobs_root) if jobs_root else job_d.parent
    try:
        for cands in Path(root).glob("*/candidates.json"):
            if cands.parent.resolve() == job_d:
                continue
            try:
                src = str(json.loads(cands.read_text(encoding="utf-8")).get("source") or "")
            except (OSError, ValueError):
                continue
            if src and Path(src).resolve().is_relative_to(job_d):
                return True
    except OSError:
        pass
    return False


def is_void(job_d, running=None, jobs_root=None):
    """True when the capture is over and produced no video at all."""
    job_d = Path(job_d)
    if not is_live_job(job_d):
        return False
    if (job_d / "monitor.json").is_file():
        return False  # monitor.py ran the full pipeline: its clips live in the moments job
    if running is None:
        running = recording_running(job_d)
    if running or analysis_running(job_d):
        return False
    if source_referenced(job_d, jobs_root):
        return False
    return is_empty(job_d)


def folder_stats(path):
    """(bytes, file count) of a folder tree."""
    total = count = 0
    for f in Path(path).rglob("*"):
        if f.is_file():
            try:
                total += f.stat().st_size
                count += 1
            except OSError:
                continue
    return total, count


def job_identity(job_d):
    """URL, channel and title of the capture, for the discard log."""
    job_d = Path(job_d)
    info = {}
    for name in ("info.json", "live/live.json"):
        try:
            data = json.loads((job_d / name).read_text(encoding="utf-8"))
            if isinstance(data, dict):
                info.update({k: v for k, v in data.items() if v})
        except (OSError, ValueError):
            continue
    return {"url": str(info.get("url") or info.get("webpage_url") or ""),
            "channel": str(info.get("channel") or info.get("uploader") or ""),
            "title": str(info.get("title") or "")}


def discarded_rows():
    """History of the void jobs already deleted."""
    try:
        rows = json.loads(DISCARDED.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return rows if isinstance(rows, list) else []


def record_discarded(row):
    STATE.mkdir(parents=True, exist_ok=True)
    rows = [r for r in discarded_rows() if r.get("job") != row.get("job")]
    rows.insert(0, row)
    DISCARDED.write_text(json.dumps(rows[:KEEP_DISCARDED], ensure_ascii=False, indent=1), encoding="utf-8")


def discard(job_d, reason="live encerrada sem cortes"):
    """Delete one void job folder and log what was removed.

    Refuses folders that hold a video, and returns None when the folder could not be
    fully removed (a file is still locked on Windows) so the caller can retry later.
    """
    job_d = Path(job_d)
    if not is_empty(job_d):
        return None
    total, count = folder_stats(job_d)
    row = {"job": job_d.name, "mb": round(total / 1e6, 1), "files": count,
           "at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
           "reason": reason}
    row.update(job_identity(job_d))
    shutil.rmtree(job_d, ignore_errors=True)
    if job_d.exists():  # a media player may still hold a segment; give it one moment
        time.sleep(2)
        shutil.rmtree(job_d, ignore_errors=True)
        if job_d.exists():
            return None
    record_discarded(row)
    return row


def void_jobs(jobs_root, grace_minutes=0, running_ids=()):
    """Void captures found under jobs_root (finished, no video, past the grace period)."""
    root = Path(jobs_root)
    if not root.is_dir():
        return []
    import time
    now = time.time()
    out = []
    for jd in sorted(root.iterdir()):
        if not jd.is_dir() or not is_live_job(jd) or jd.name in set(running_ids):
            continue
        if not is_void(jd, jobs_root=root):
            continue
        if grace_minutes and now - ended_at(jd) < grace_minutes * 60:
            continue
        total, count = folder_stats(jd)
        row = {"id": jd.name, "mb": round(total / 1e6, 1), "files": count}
        row.update(job_identity(jd))
        out.append(row)
    return out


def prune_voids(jobs_root, grace_minutes=0, running_ids=(), dry_run=False):
    """Delete every void capture under jobs_root. Returns the discarded rows."""
    rows = []
    for row in void_jobs(jobs_root, grace_minutes=grace_minutes, running_ids=running_ids):
        if dry_run:
            rows.append(row)
            continue
        discarded = discard(Path(jobs_root) / row["id"])
        if discarded:
            rows.append(discarded)
    return rows

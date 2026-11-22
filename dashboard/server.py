"""Local web dashboard for gtavi-claudecode.

Reads the system's own files (jobs/, streams.md) and drives its scripts:
  - Cortes:        every jobs/<id>/edits/clip_N.mp4 with hook, layout, duration and preview frames
  - Lives:         which live is being monitored (live.py processes), status, recorded size, spikes, chat
  - Views:         live viewer counts per monitored stream (Twitch GQL / yt-dlp), cached 90 s
  - Criação:       pipeline per job: source -> candidates -> cuts, with scores and reasons
  +Lives:          start monitoring a new URL (spawns scripts/live.py) and refresh the streams list

usage: python dashboard/server.py [--port 8787] [--no-open]
Only binds 127.0.0.1. No new dependencies.
"""
import argparse, ctypes, hashlib, json, math, os, re, shutil, subprocess, sys, threading, time, urllib.error, urllib.request, webbrowser
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs, unquote, urlencode

ROOT = Path(__file__).resolve().parent.parent
JOBS = ROOT / "jobs"
VENV_PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
PYTHON = str(VENV_PYTHON) if sys.platform == "win32" and VENV_PYTHON.exists() else sys.executable
STATIC = Path(__file__).resolve().parent / "static"
STATE_DIR = Path(__file__).resolve().parent / "state"
SERVER_LOCK = STATE_DIR / "server.lock"
LOGS = Path(__file__).resolve().parent / "logs"
WATCHLIST = STATE_DIR / "watchlist.json"
WATCH_LOCK = threading.RLock()
PIPELINE_QUEUE = STATE_DIR / "pipeline_queue.json"  # sequential post-capture jobs (spec D0)
PIPELINE_MAX_AGE_H = 48  # a capture that ended more than this ago is never auto-analyzed
VIEWER_TTL = 90

sys.path.insert(0, str(ROOT / "scripts"))
import common  # noqa: E402
import performance  # noqa: E402
import spike_feedback  # noqa: E402
import voids  # noqa: E402
from youtube_publish import YouTubePublisher  # noqa: E402
try:
    import twitch as tw  # noqa: E402
except Exception:
    tw = None

MIME = {".mp4": "video/mp4", ".ts": "video/mp2t", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".png": "image/png", ".json": "application/json", ".md": "text/markdown",
        ".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml"}
VIEWERS = {}  # url -> {"viewers": int|None, "checked": iso, "platform": str}
THUMB_LOCK = threading.Lock()
THUMB_TRIED = set()
VOID_GRACE_MIN = int(os.environ.get("GTAVI_VOID_GRACE_MIN") or 15)  # a capture that just ended is kept a few minutes before being discarded
VOID_LOCK = threading.Lock()
YOUTUBE = YouTubePublisher(STATE_DIR, JOBS)


def ensure_thumb(mp4, target):
    """Extract a preview frame for a clip that was cut before pack.py made a thumbnail."""
    if target.exists():
        return target
    key = str(target)
    with THUMB_LOCK:
        if key in THUMB_TRIED:
            return None
        THUMB_TRIED.add(key)
    for seek in ("1", "0"):
        try:
            r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", seek, "-i", str(mp4),
                                "-frames:v", "1", "-vf", "scale=540:-2", "-q:v", "3", str(target)],
                               capture_output=True, timeout=60, env=child_env())
        except Exception:
            continue
        if r.returncode == 0 and target.exists():
            return target
        target.unlink(missing_ok=True)
    return None


# ---------- thumbs por momento (spec D9/D10) ----------

MOMENT_THUMB_LOCK = threading.RLock()
MOMENT_THUMB_INFLIGHT = set()  # frames being extracted right now (the 4 s poll must not double-spawn ffmpeg)


def _moment_source_video(jd):
    """The video the moments were analyzed from: in-job (live capture or download) or the
    candidates.json["source"] reference — D0 and file-analysis keep the real video in
    ANOTHER job dir, and claiming "fonte removida" for those would be a lie (review I3)."""
    jd = Path(jd)
    src = jd / "live" / "source_cfr.mp4"
    if src.exists():
        return src
    for ext in ("*.mp4", "*.mkv", "*.webm"):
        for f in sorted(jd.glob(ext)):
            if re.fullmatch(r"clip_\d+(?:_[A-Za-z0-9]{2,8})?", f.stem):
                continue  # a rendered cut, not the source (review M7)
            return f
    try:
        ref = str((common.load_json(jd / "candidates.json") or {}).get("source") or "")
        if ref:
            rp = Path(ref)
            if rp.is_file() and rp.resolve().is_relative_to(JOBS.resolve()):
                return rp
    except (OSError, ValueError):
        pass
    return None


def thumb_kind_for(jd, n):
    """The D10 chain decision WITHOUT extracting — the card payload is lazy."""
    jd = Path(jd)
    if _moment_source_video(jd):
        return "source"
    if (jd / "edits" / f"clip_{n}.mp4").exists() or (jd / "edits" / f"thumb_{n}.jpg").exists():
        return "clip"
    return None


def _moment_cand(jd, n):
    """The candidate with this n, or None (missing/garbled candidates.json is just a 404)."""
    try:
        cands = common.load_json(Path(jd) / "candidates.json").get("candidates") or []
    except (OSError, ValueError):
        return None
    return next((c for c in cands if int(c.get("n") or 0) == int(n)), None)


def moment_thumb(jd, cand):
    """(Path, kind) for one moment's image, or None. Lazy: ffmpeg only on the first request;
    the file name carries the moment's start so re-analyses/splits never serve a stale frame."""
    jd = Path(jd)
    n = int(cand.get("n") or 0)
    start = float(cand.get("start") or 0)
    kind = thumb_kind_for(jd, n)
    if kind == "clip":
        t = jd / "edits" / f"thumb_{n}.jpg"
        if t.exists():
            return t, "clip"
        mp4 = jd / "edits" / f"clip_{n}.mp4"
        made = ensure_thumb(mp4, t)
        return (made, "clip") if made else None
    if kind != "source":
        return None
    thumbs = jd / "thumbs"
    thumbs.mkdir(parents=True, exist_ok=True)
    target = thumbs / f"moment_{n}_s{int(start)}.jpg"
    if target.exists():
        return target, "source"
    key = str(target)
    with MOMENT_THUMB_LOCK:
        if key in MOMENT_THUMB_INFLIGHT:
            return None  # another request is already extracting this very frame
        MOMENT_THUMB_INFLIGHT.add(key)
        try:
            src = _moment_source_video(jd)
            for seek in (start + 2, start, 0):
                try:
                    r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{max(0.0, seek):.2f}",
                                        "-i", str(src), "-frames:v", "1", "-vf", "scale=540:-2", "-q:v", "3",
                                        str(target)], capture_output=True, timeout=60, env=child_env())
                except Exception:
                    continue
                if r.returncode == 0 and target.exists():
                    return target, "source"
                target.unlink(missing_ok=True)
            return None
        finally:
            MOMENT_THUMB_INFLIGHT.discard(key)

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes
    _k32 = ctypes.windll.kernel32
    _k32.OpenProcess.restype = wintypes.HANDLE
    _k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]


def job_id_for(url):
    key = hashlib.sha1(url.encode()).hexdigest()[:8]
    stem = re.sub(r"[^a-z0-9]+", "-", url.split("v=")[-1].split("/")[-1].lower())[:24].strip("-")
    return f"{stem or 'job'}-{key}"


def pid_alive(pid):
    if not pid:
        return False
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if sys.platform == "win32":
        h = _k32.OpenProcess(0x1F0FFF, False, pid)  # PROCESS_ALL_ACCESS; the limited variant can be denied
        if not h:
            return False
        ec = wintypes.DWORD()
        ok = _k32.GetExitCodeProcess(h, ctypes.byref(ec))
        _k32.CloseHandle(h)
        return bool(ok) and ec.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def acquire_server_lock():
    """Claim this project's state directory before starting background workers."""
    SERVER_LOCK.parent.mkdir(parents=True, exist_ok=True)
    with SERVER_LOCK.with_suffix(".guard").open("a+b") as guard:
        try:
            if sys.platform == "win32":
                import msvcrt
                guard.seek(0)
                msvcrt.locking(guard.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, PermissionError):
            return False
        try:
            owner = common.load_json(SERVER_LOCK)
        except (OSError, ValueError):
            owner = {}
        if isinstance(owner, dict) and owner.get("pid") and pid_alive(owner["pid"]):
            return False
        tmp = SERVER_LOCK.with_suffix(".tmp")
        common.save_json(tmp, {"pid": os.getpid(), "ts": time.time()})
        tmp.replace(SERVER_LOCK)
        return True


def load_watchlist():
    with WATCH_LOCK:
        try:
            rows = common.load_json(WATCHLIST)
        except (OSError, ValueError):
            return []
    return [row for row in rows if isinstance(row, dict) and row.get("id") and "url" in row] if isinstance(rows, list) else []


def save_watchlist(rows):
    STATE_DIR.mkdir(exist_ok=True)
    with WATCH_LOCK:
        tmp = WATCHLIST.with_suffix(".tmp")
        common.save_json(tmp, rows)
        tmp.replace(WATCHLIST)


def child_env():
    """PATH with the usual tool install dirs appended, so live.py/streams.py find ffmpeg/yt-dlp
    even when the dashboard was started from a shell without them."""
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"  # spike/chat prints carry emoji; cp1252 logs crash the scripts
    if sys.platform == "win32":
        extra = [str(Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Links"),
                 r"C:\ffmpeg\bin"]
        env["PATH"] = env.get("PATH", "") + os.pathsep + os.pathsep.join(p for p in extra if p)
    return env


def log_path(job_id):
    return LOGS / f"watch_{job_id}.log"


def log_tail(job_id, n=14, *, creation=False, comp=False):
    path = (LOGS / f"compile_{job_id}.log" if comp else
            LOGS / f"create_{job_id}.log" if creation else log_path(job_id))
    try:
        with path.open("rb") as fh:
            end = fh.seek(0, os.SEEK_END)
            start = end
            chunks = []
            breaks = 0
            while start and breaks <= n:
                offset = max(0, start - 8192)
                fh.seek(offset)
                chunk = fh.read(start - offset)
                chunks.append(chunk)
                breaks += chunk.count(b"\n") + chunk.count(b"\r") - chunk.count(b"\r\n")
                start = offset
        lines = b"".join(reversed(chunks)).decode("utf-8", errors="replace").splitlines()
        return lines[-n:]
    except OSError:
        return []


def _detached_kwargs():
    """Spawn flags that SURVIVE the dashboard being closed or restarted.

    On Windows, CREATE_NEW_PROCESS_GROUP alone still ties the child to this console: closing
    the dashboard (or a task-tree kill) takes the recorder down with it, killing hours of
    capture mid-way. DETACHED_PROCESS + CREATE_BREAKAWAY_FROM_JOB cut the tie; when a job
    object refuses the breakaway, fall back to the plain group (old behavior)."""
    if sys.platform != "win32":
        return {"start_new_session": True}
    return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
            | subprocess.CREATE_BREAKAWAY_FROM_JOB}


def _spawn_detached(cmd, fh, env):
    kwargs = _detached_kwargs()
    try:
        return subprocess.Popen(cmd, cwd=str(ROOT), stdout=fh, stderr=subprocess.STDOUT, env=env, **kwargs)
    except OSError:  # breakaway denied by the job object: retry with the conservative flags
        if sys.platform != "win32":
            raise
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        return subprocess.Popen(cmd, cwd=str(ROOT), stdout=fh, stderr=subprocess.STDOUT, env=env, **kwargs)


def start_watch(url, minutes):
    with WATCH_LOCK, PIPELINE_LOCK:
        rows = load_watchlist()
        jid = job_id_for(url)
        if any(r["id"] == jid and watch_running(r) for r in rows):
            raise RuntimeError("esta live já está sendo monitorada")
        pipeline_reconcile()
        rows_q = pipeline_rows()
        if any(r.get("job") == jid and r.get("state") == "running" for r in rows_q):
            raise RuntimeError("aguarde a pós-captura terminar antes de retomar esta live")
        LOGS.mkdir(exist_ok=True)
        with open(log_path(jid), "a", encoding="utf-8") as fh:
            proc = _spawn_detached([PYTHON, str(ROOT / "scripts" / "live.py"), url,
                                    "--minutes", str(minutes)], fh, child_env())
        if any(r.get("job") == jid for r in rows_q):
            save_pipeline_rows([r for r in rows_q if r.get("job") != jid])
        rows = [r for r in rows if r["id"] != jid]
        rows.append({"id": jid, "url": url, "pid": proc.pid, "minutes": minutes,
                     "started": datetime.now(timezone.utc).isoformat(timespec="seconds")})
        save_watchlist(rows)
        return rows[-1]


def stop_watch(jid):
    with WATCH_LOCK:
        rows = load_watchlist()
        row = next((r for r in rows if r["id"] == jid), None)
        if not row:
            raise KeyError("monitor não encontrado")
        pid = row.get("pid")
        if watch_running(row):
            if sys.platform == "win32":
                result = subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True)
                if result.returncode and pid_alive(pid):
                    raise RuntimeError("não foi possível parar a gravação")
            else:
                try:
                    os.killpg(pid, 9)
                except ProcessLookupError:
                    pass
        row["pid"] = 0
        save_watchlist(rows)
        return row


def settings_capture():
    """Capture defaults for the automatic post-capture pipeline (spec D0). A preset renamed
    or removed after being saved falls back to the default — otherwise the watcher would
    respawn a dying pipeline forever (review Important 2)."""
    c = (common.load_settings().get("capture") or {})
    preset = str(c.get("preset") or "").strip()
    if preset and preset != "default":
        try:
            common.preset_values(preset)
        except (SystemExit, Exception):
            preset = "react-br"
    preset = preset or "react-br"
    raw = c.get("top")
    try:
        top = 3 if raw in (None, "") else int(raw)
    except (TypeError, ValueError):
        top = 3
    if not 1 <= top <= 12:
        top = 3
    return {"preset": preset, "top": top}


def parse_capture(body):
    """Capture defaults from the Ajustes form; (None, msg) on anything invalid."""
    c = body.get("capture")
    if not isinstance(c, dict):
        c = {}
    preset = str(c.get("preset") or "").strip()
    if preset and preset != "default":
        try:
            common.preset_values(preset)  # sys.exit()s with the available list on unknown keys
        except SystemExit as exc:
            return None, str(exc)
        except Exception as exc:
            return None, str(exc)
    raw = c.get("top")
    try:
        top = 3 if raw in (None, "") else int(raw)  # `0 or 3` would swallow an explicit 0
    except (TypeError, ValueError):
        return None, "top deve ser um número"
    if not 1 <= top <= 12:
        return None, "top deve ficar entre 1 e 12"
    return {"preset": preset or "react-br", "top": top}, None


# ---------- fila sequencial de pós-captura (spec D0) ----------

PIPELINE_LOCK = threading.RLock()  # watcher and worker both read-modify-write the queue file

def pipeline_rows():
    with PIPELINE_LOCK:
        try:
            rows = json.loads(PIPELINE_QUEUE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
    return rows if isinstance(rows, list) else []


def save_pipeline_rows(rows):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with PIPELINE_LOCK:
        tmp = PIPELINE_QUEUE.with_suffix(".tmp")
        common.save_json(tmp, rows)
        tmp.replace(PIPELINE_QUEUE)


def enqueue_pipeline(job, top, preset):
    """Queue one post-capture pipeline; False when the job is already queued or running."""
    with PIPELINE_LOCK:
        rows = pipeline_rows()
        if any(r.get("job") == job for r in rows):
            return False
        rows.append({"job": job, "top": int(top), "preset": str(preset), "state": "queued",
                     "pid": 0, "queued_ts": datetime.now(timezone.utc).isoformat(timespec="seconds")})
        save_pipeline_rows(rows)
        return True


def pipeline_job_ids():
    pipeline_reconcile()
    return {r.get("job") for r in pipeline_rows() if r.get("state") in ("queued", "running")}


def pipeline_for(job):
    """{"state", "position"} for the card's processando state, or None when not queued."""
    pipeline_reconcile()
    rows = pipeline_rows()
    running_pos = 0
    queued = 0
    for r in rows:
        if r.get("job") == job and r.get("state") == "running":
            return {"state": "running", "position": 0}
        if r.get("state") == "queued":
            if r.get("job") == job:
                return {"state": "queued", "position": queued}
            queued += 1
        else:
            running_pos += 1
    return None


def should_enqueue(row, now=None):
    """A watchlist capture worth auto-analyzing: stopped, has raw segments, not analyzed
    in this session, not already queued, and ended within PIPELINE_MAX_AGE_H (spec D0).
    monitor.json from a previous session does not block: live.py deletes old segments on
    start, so any segment newer than the analysis means a fresh, unanalyzed session."""
    now = time.time() if now is None else now
    if watch_running(row):
        return False
    jd = JOBS / row["id"]
    ld = jd / "live"
    if not ld.is_dir():
        return False
    segs = [s for s in ld.glob("seg_*.ts") if s.is_file()]
    if not segs:
        return False
    monitor = jd / "monitor.json"
    if monitor.is_file():  # a newer segment than the analysis = a new session to analyze
        try:
            if monitor.stat().st_mtime >= max(s.stat().st_mtime for s in segs):
                return False
        except OSError:
            return False
    if any(r.get("job") == row["id"] for r in pipeline_rows()):
        return False
    ended = voids.ended_at(jd)
    return bool(ended) and (now - ended) < PIPELINE_MAX_AGE_H * 3600


PIPELINE_STALE_S = 6 * 3600  # a run over this ceiling is released even with a "live" pid (pid reuse)


def pipeline_head_finished(head):
    """pid dead, or this run already wrote its terminal monitor.json (post_capture writes it
    on success AND failure — a reused PID can no longer wedge the queue head), or the run
    overstayed a generous ceiling (transcription of a full capture stays well inside it)."""
    try:
        qts = datetime.fromisoformat(head.get("started_ts") or head.get("queued_ts") or "").timestamp()
    except ValueError:
        qts = 0.0
    if qts:
        try:
            if (JOBS / head["job"] / "monitor.json").stat().st_mtime >= qts:
                return True
        except OSError:
            pass
        if time.time() - qts > PIPELINE_STALE_S:
            return True
    return False


def pipeline_reconcile():
    """Release the head when its run is provably over. Called from the read path too, so a
    wedged worker thread can never leave a phantom "processando" card on screen."""
    with PIPELINE_LOCK:
        rows = pipeline_rows()
        if rows and rows[0].get("state") == "running":
            head = rows[0]
            if not pid_alive(head.get("pid")) or pipeline_head_finished(head):
                print(f"pós-captura concluída: {head['job']}", flush=True)
                save_pipeline_rows(rows[1:])


def pipeline_worker_tick():
    pipeline_reconcile()
    with PIPELINE_LOCK:
        rows = pipeline_rows()
        if not rows:
            return
        head = rows[0]
        if head.get("state") == "running":
            return  # reconcile released it if the run is over
        cmd = [PYTHON, str(ROOT / "scripts" / "post_capture.py"), head["job"],
               "--top", str(head.get("top") or 3), "--preset", str(head.get("preset") or "")]
        LOGS.mkdir(exist_ok=True)
        fh = open(log_path(head["job"]), "a", encoding="utf-8")  # same log monitor_info already tails
        try:
            proc = _spawn_detached(cmd, fh, child_env())
        finally:
            fh.close()
        head.update({"state": "running", "pid": proc.pid,
                     "started_ts": datetime.now(timezone.utc).isoformat(timespec="seconds")})
        save_pipeline_rows(rows)
        print(f"pós-captura iniciada: {head['job']} (pid {proc.pid})", flush=True)


def pipeline_worker():
    time.sleep(5)  # after the watcher's first pass, before the pruner needs protection
    while True:
        try:
            pipeline_worker_tick()
        except Exception as e:
            print(f"pipeline worker: {e}", flush=True)
        time.sleep(5)


def capture_watcher_tick():
    cap = settings_capture()
    with WATCH_LOCK:
        for row in load_watchlist():
            if should_enqueue(row) and enqueue_pipeline(row["id"], cap["top"], cap["preset"]):
                print(f"pós-captura enfileirada: {row['id']} (preset {cap['preset']}, top {cap['top']})", flush=True)


def capture_watcher():
    time.sleep(3)  # first pass BEFORE the pruner's first sweep (+5 s) — Review Focus 2
    while True:
        try:
            capture_watcher_tick()
        except Exception as e:
            print(f"capture watcher: {e}", flush=True)
        time.sleep(10)


RETENTION_H = int(os.environ.get("GTAVI_RETENTION_H") or 24)  # processed captures are deleted whole after this (operator decision 2026-11-21)


def retention_sweep(now=None):
    """Full delete of processed live captures after the retention window (spec 2026-11-21):
    monitor.json present + ended ≥ RETENTION_H hours + not recording and not queued/pipelining.
    Clips stay safe in their moments jobs; re-cutting those shows 'fonte não encontrada'."""
    now = time.time() if now is None else now
    if not JOBS.is_dir():
        return
    running_ids = {r["id"] for r in load_watchlist() if watch_running(r)}
    for jd in JOBS.iterdir():
        if not jd.is_dir() or not (jd / "live").is_dir():
            continue
        mon_path = jd / "monitor.json"
        if not mon_path.is_file():
            continue  # never processed: the watcher/pipeline owns it, voids own the empties
        meta = common.load_json(mon_path) or {}
        ended = _iso_ts(meta.get("failed_ts")) or voids.ended_at(jd)
        if not ended or (now - ended) < RETENTION_H * 3600:
            continue
        if jd.name in running_ids or pipeline_for(jd.name):
            continue
        row = voids.job_identity(jd)
        mb = round(sum(f.stat().st_size for f in jd.rglob("*") if f.is_file()) / 1e6, 1)
        shutil.rmtree(jd, ignore_errors=True)
        if not jd.exists():
            row.update({"job": jd.name, "mb": mb, "reason": "retenção 24h",
                        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds")})
            voids.record_discarded(row)
            print(f"retenção: captura processada apagada {jd.name} ({mb} MB)", flush=True)


def void_pruner_tick():
    with WATCH_LOCK:
        rows = load_watchlist()
        running = {r["id"] for r in rows if watch_running(r)} | pipeline_job_ids()
        with VOID_LOCK:
            freed = voids.prune_voids(JOBS, grace_minutes=VOID_GRACE_MIN, running_ids=running)
        for row in freed:
            print(f"void descartada: {row['job']} ({row['mb']} MB liberados)", flush=True)
        retention_sweep()
        alive = [r for r in rows if watch_running(r) or (JOBS / r["id"]).is_dir()]
        if len(alive) != len(rows):
            save_watchlist(alive)


def void_pruner():
    """Background sweep: delete void captures (ended, no video at all) past the grace period
    and drop watchlist rows whose job folder is gone. Never touches a running recorder or a
    queued/running post-capture pipeline."""
    time.sleep(5)
    while True:
        try:
            void_pruner_tick()
        except Exception as e:
            print(f"void pruner: {e}", flush=True)
        time.sleep(60)


# ---------- settings & performance ----------

def mask_key(key):
    key = str(key or "")
    if not key:
        return ""
    return f"{key[:4]}…{key[-4:]}" if len(key) > 8 else "•••"


def apply_settings_update(s, body):
    """Merge one POSTed settings body into the stored settings; an ABSENT key leaves its
    stored value untouched (a partial POST from a script/curl must not wipe config — the
    Task 9 incident). A present-but-empty value still means "clear": the Ajustes form
    always sends every field. The LLM key is special: an empty post never wipes it."""
    if "llm" in body:
        llm = body.get("llm") or {}
        posted_key = str(llm.get("key") or "").strip()
        new_key = (s.get("llm") or {}).get("key") or ""
        if posted_key and "…" not in posted_key and posted_key != "•••":
            new_key = posted_key[:400]  # a masked placeholder never overwrites the stored key
        s["llm"] = {"base": str(llm.get("base") or "").strip()[:300],
                    "key": new_key,
                    "model": str(llm.get("model") or "").strip()[:120]}
    if "whisper_model" in body:
        wm = str(body.get("whisper_model") or "").strip().lower()
        if wm in {"", "base", "small", "medium", "large-v3"}:
            s["whisper_model"] = wm
    if "ytdlp_cookies" in body:
        yc = str(body.get("ytdlp_cookies") or "").strip()
        if yc:
            s["ytdlp_cookies"] = yc[:500]  # browser name or cookies.txt path (relative = project root)
        else:
            s.pop("ytdlp_cookies", None)
    return s


def settings_state():
    """Ajustes tab payload: the key is always masked, never returned in full."""
    s = common.load_settings()
    from_env = any(os.environ.get(k) for k in ("GTAVI_LLM_BASE", "GTAVI_LLM_KEY", "GTAVI_LLM_MODEL"))
    llm = s.get("llm") or {}
    return {"llm": {"base": str(llm.get("base") or ""), "key": mask_key(llm.get("key")),
                    "model": str(llm.get("model") or "")},
            "whisper_model": str(s.get("whisper_model") or ""),
            "ytdlp_cookies": str(s.get("ytdlp_cookies") or ""),
            "capture": settings_capture(),
            "llm_active": bool(common.llm_config()),
            "source": "env" if from_env else "settings"}


def performance_summary():
    agg = performance.aggregate()
    agg["context_active"] = performance.context() is not None
    return agg


def job_row_for(jid):
    jd = (JOBS / jid).resolve()
    jd.relative_to(JOBS.resolve())  # raises ValueError for paths outside jobs/
    return jd


# ---------- state scan ----------

def lang_label(meta):
    """Badge text: ES→PT when translated, ES when native/unique, ? when unknown."""
    tgt = str(meta.get("lang_target") or "").upper()
    src = str(meta.get("lang_source") or "").upper()
    if not tgt:
        return "?"
    if src and tgt and src != tgt:
        return f"{src}→{tgt}"
    return tgt


def build_retitle_cmd(meta, job, n, title, tmp_json, hook=""):
    """Re-cut one clip from its saved metadata, reusing the exact mode/cam/language/cache."""
    sentence = meta.get("captions_mode") == "sentence"
    caps = meta.get("captions_style") or meta.get("captions_mode") or "word"
    if caps == "sentence":  # sentence mode is selected by --lang, not a caption style
        caps = "bubble"
    cam = "auto" if sentence else (meta.get("cam") or "auto")
    cmd = [PYTHON, str(ROOT / "scripts" / "cut.py"), str(job), "--pick", str(n),
           "--start", str(meta["start"]), "--end", str(meta["end"]),
           "--layout", str(meta.get("layout") or "blur"), "--cam", str(cam),
           "--orientation", str(meta.get("orientation") or "vertical"),
           "--title", title, "--title-style", meta.get("title_style") or "box",
           "--title-secs", str(meta.get("title_secs") or 0), "--captions", str(caps)]
    if sentence and meta.get("lang_target"):
        cmd += ["--lang", str(meta["lang_target"])]
    if sentence and meta.get("sentences_cache") and tmp_json:
        cmd += ["--translations-file", str(tmp_json)]
    if meta.get("allow_profanity"):
        cmd += ["--allow-profanity"]
    if hook:
        cmd += ["--hook", hook]
    return cmd


def clip_info(job_d):
    out = []
    ed = job_d / "edits"
    if not ed.is_dir():
        return out
    # The D0 pipeline renders its own thumbs — a dashboard-side ffmpeg on the same output
    # file races it and fails the whole cut step (bug seen 2026-11-20: "Error opening
    # output files: Invalid argument" on thumb_<n>.jpg mid-render). Read creation.json
    # DIRECTLY: creation_run() calls clip_info() for auto mode — calling it here recurses.
    busy = pipeline_for(job_d.name) is not None
    if not busy:
        try:
            data = common.load_json(job_d / "creation.json")
            busy = bool(data) and data.get("status") in ("analyzing", "rendering")
        except Exception:
            busy = False
    for cj in sorted(ed.glob("clip_*.json")):
        m = re.match(r"clip_(\d+)(?:_([A-Za-z0-9]{2,8}))?\.json$", cj.name)
        if not m:
            continue
        n, suffix = m.group(1), m.group(2)
        try:
            d = common.load_json(cj)
        except Exception:
            d = {}
        posting_path = ed / f"clip_{m.group(1)}_posting.json"
        try:
            posting = common.load_json(posting_path) if posting_path.exists() else None
        except Exception:
            posting = None
        stem = cj.stem
        mp4 = ed / f"{stem}.mp4"
        if not mp4.exists():
            continue
        thumb_file = ed / f"thumb_{n}.jpg"
        if not thumb_file.exists() and suffix:
            thumb_file = ed / f"thumb_{suffix}.jpg"
        if not thumb_file.exists():
            thumb_file = ed / "thumb.jpg"
        if not thumb_file.exists():
            thumb_file = next(
                (ed / f"c{n}_{i}.jpg" for i in range(1, 12) if (ed / f"c{n}_{i}.jpg").exists()), None)
        if not thumb_file and not busy:
            thumb_file = ensure_thumb(mp4, ed / f"thumb_{n}.jpg")
        thumb = thumb_file if thumb_file else None
        try:
            st = mp4.stat()
        except FileNotFoundError:
            continue
        try:
            dur = max(0.0, float(d.get("end") or 0) - float(d.get("start") or 0))
        except (TypeError, ValueError):
            dur = 0.0
        variant = bool(suffix or d.get("variant_of"))
        out.append({"job": job_d.name, "n": n, "file": f"{job_d.name}/edits/{mp4.name}",
                    "hook": d.get("hook") or d.get("title") or f"clip_{n}",
                    "layout": d.get("layout") or "-", "orientation": d.get("orientation") or "vertical",
                    "start": d.get("start"), "end": d.get("end"),
                    "dur": round(dur, 1), "thumb": f"{job_d.name}/edits/{thumb.name}" if thumb else None,
                    "mb": round(st.st_size / 1e6, 1), "mtime": int(st.st_mtime),
                    "source": d.get("source_title") or (job_d / "info.json" and _info_title(job_d)) or job_d.name,
                    "source_url": d.get("source_url") or "", "ass": (ed / f"{stem}.ass").exists(),
                    "lang": lang_label(d), "lang_source": d.get("lang_source"),
                    "lang_target": d.get("lang_target"), "variant": variant,
                    "variant_of": d.get("variant_of"),
                    "variant_lang": suffix or d.get("lang_target"),
                    "hook_type": d.get("hook_type") or "", "llm_total": d.get("llm_total"),
                    "title_review": bool(d.get("title_review")), "posting": posting})
    return sorted(out, key=lambda c: c["mtime"], reverse=True)


def _info_title(job_d):
    try:
        return common.load_json(job_d / "info.json").get("title") or ""
    except Exception:
        return ""


YTDLP_NAME_TRIED = set()  # one metadata attempt per job — yt-dlp is slow, never run per poll


def resolve_channel(jd, url):
    """YouTube live captures start before yt-dlp resolves the channel (info.json has
    channel: ""). Ask yt-dlp ONCE per job for the metadata and write it back to
    info.json; on failure the generic fallback stays (spec: honest card, no lies)."""
    jd = Path(jd)
    key = jd.name
    if key in YTDLP_NAME_TRIED:
        return ""
    YTDLP_NAME_TRIED.add(key)
    try:
        info_path = jd / "info.json"
        info = common.load_json(info_path) if info_path.exists() else {}
        cmd = common.ytdlp_base() + ["--skip-download", "-j", "--", url]
        r = subprocess.run(cmd, capture_output=True, timeout=25, env=child_env())
        meta = json.loads((r.stdout or b"{}").decode("utf-8", errors="ignore"))
        name = str(meta.get("channel") or meta.get("uploader") or "").lstrip("@")
        if name:
            info["channel"] = name
            info.setdefault("url", meta.get("webpage_url") or url)
            common.save_json(info_path, info)
            return name
    except Exception:
        pass
    return ""


def _streamer_name(job_d, url=""):
    """Resolve a human channel name; a job hash or stream title is not a streamer name."""
    try:
        info = common.load_json(job_d / "info.json")
    except Exception:
        info = {}
    for key in ("channel", "uploader", "creator", "uploader_id"):
        value = str(info.get(key) or "").strip()
        if value and not re.fullmatch(r"(?:UC[A-Za-z0-9_-]{18,}|[0-9a-f]{8,}|job-[0-9a-f]+)", value, re.I):
            return value.lstrip("@")
    url = url or info.get("url") or info.get("webpage_url") or ""
    parsed = urlparse(url)
    path = parsed.path.strip("/").split("/")[0]
    if path.lower() in {"watch", "videos", "video", "live"}:
        return "Canal do YouTube" if "youtube" in (parsed.hostname or "") else "Canal"
    return path.lstrip("@") or "Canal"


def parse_spikes(ld):
    f = ld / "spikes.md"
    rows = []
    if f.exists():
        for ln in f.read_text(encoding="utf-8", errors="ignore").splitlines():
            if ln.startswith("|") and "---" not in ln and "UTC time" not in ln:
                rows.append([c.strip() for c in ln.strip("|").split("|")])
    return rows


def watch_running(entry, jd=None):
    """A watchlist row is really recording: PID alive AND the job wrote something recently.

    Windows reuses PIDs — a dead capture can otherwise show 'gravando' forever, and the
    same stale PID would keep the void pruner away from it."""
    if not pid_alive(entry.get("pid")):
        return False
    jd = jd or (JOBS / entry["id"])
    activity = max(voids.last_activity(jd), _iso_ts(entry.get("started")) or 0)
    return time.time() - activity < voids.STALE_SECONDS


# ---------- card state (spec D1/D2): derived server-side, the client only renders ----------

SUSPECT_STALE_SEG_S = 90   # blocks land every ~60 s; 90 s without one means trouble (dead at 120 s)
SUSPECT_CHAT_DEAD_S = 180  # no chat message for 3 min while recording
SUSPECT_NO_SPIKE_S = 1500  # recording for 25+ min without a single marked spike


def _iso_ts(v):
    try:
        return datetime.fromisoformat(str(v)).timestamp()
    except (ValueError, TypeError):
        return None


def session_spikes(rows, started):
    """Spike rows written after `started` (watchlist ISO UTC) — "Retomar gravação" reuses the
    job, so counting the whole spikes.md would fire the no-spike alarm after every resume."""
    if not started:
        return rows
    try:
        st = datetime.fromisoformat(str(started)).astimezone(timezone.utc).replace(tzinfo=None)
    except (ValueError, TypeError):
        return rows
    out = []
    for r in rows:
        try:
            dt = datetime.strptime(str(r[0]), "%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError, IndexError):
            out.append(r)
            continue
        if dt >= st:
            out.append(r)
    return out


def card_state(m, now=None):
    """One of the seven card states plus anomalies/countdown/reason (spec D1 precedence:
    running > pipeline > erro > sem-nada > pronto > offline)."""
    now = time.time() if now is None else now
    countdown = None
    if m.get("void") and m.get("ended_at"):
        countdown = max(0, VOID_GRACE_MIN - int((now - m["ended_at"]) // 60))
    meta = m.get("monitor_meta") or {}
    base = {"anomalies": [], "countdown_min": countdown, "reason": None}
    if m.get("status") == "running":
        st = _iso_ts(m.get("started"))
        started_age = max(0, now - st) if st else 0
        la = m.get("last_activity")
        cl = m.get("chat_last_ts")
        if la and now - la > SUSPECT_STALE_SEG_S:
            base["anomalies"].append("sem bloco novo há mais de 90s")
        if (cl is None and started_age > SUSPECT_CHAT_DEAD_S) or (cl and now - cl > SUSPECT_CHAT_DEAD_S):
            base["anomalies"].append("chat sem msgs novas")
        if started_age > SUSPECT_NO_SPIKE_S and not (m.get("spike_rows_session") or []):
            base["anomalies"].append("sem pico marcado")
        base["state"] = "suspeito" if base["anomalies"] else "gravando"
        return base
    if m.get("pipeline"):
        base["state"] = "processando"
        return base
    if meta.get("error"):
        base["state"], base["reason"] = "erro", str(meta["error"])
        return base
    if m.get("log_error"):
        lines = [ln for ln in (m.get("log") or []) if "Traceback" in ln or "Error" in ln]
        base["state"], base["reason"] = "erro", (lines[-1] if lines else "o processo terminou com erro")
        return base
    if m.get("void"):
        base["state"] = "sem-nada"
        return base
    if meta.get("clips"):
        base["state"] = "pronto"
        return base
    base["state"] = "offline"
    return base


def monitor_info(entry):
    jid, url = entry["id"], entry["url"]
    jd = JOBS / jid
    ld = jd / "live"
    running = watch_running(entry, jd)
    segs = []
    for segment in ld.glob("seg_*.ts"):
        try:
            segs.append(segment.stat())
        except FileNotFoundError:
            continue
    size_mb = round(sum(s.st_size for s in segs) / 1e6, 1)
    last_seg = max((s.st_mtime for s in segs), default=None)
    chat = 0
    chat_last_ts = None
    for cf in [ld / "chat.twitch.jsonl", *ld.glob("chat.live_chat.json*")]:
        if cf.exists():
            try:
                with open(cf, "rb") as fh:
                    chat = sum(1 for _ in fh)
                chat_last_ts = max(chat_last_ts or 0.0, cf.stat().st_mtime)
            except OSError:
                pass
    live_meta = {}
    if (ld / "live.json").exists():
        try:
            live_meta = common.load_json(ld / "live.json")
        except Exception:
            pass
    started = entry.get("started")
    elapsed = None
    if started:
        try:
            st = datetime.fromisoformat(started).timestamp()
            elapsed = round((time.time() - st) / 60) if running else None
        except Exception:
            pass
    host = (urlparse(url).hostname or "").lower()
    plat = "YouTube" if "youtube.com" in host or "youtu.be" in host else ("Twitch" if "twitch.tv" in host else ("Kick" if "kick.com" in host else "?"))
    if not url:
        url = live_meta.get("url") or ""
        host = (urlparse(url).hostname or "").lower()
        plat = "YouTube" if "youtube.com" in host or "youtu.be" in host else ("Twitch" if "twitch.tv" in host else ("Kick" if "kick.com" in host else "?"))
    chan = _streamer_name(jd, url) if url else _streamer_name(jd)
    if (not chan or chan in {"Canal do YouTube", "Canal"}) and url:
        resolved = resolve_channel(jd, url)
        if resolved:
            chan = resolved
    title = _info_title(jd) or str(live_meta.get("title") or "")
    # The watch log is cumulative (one live.py run appends to the same file). Split it at the
    # last "recording …" banner so an OLD crash never looks like a problem with the current
    # run — errors are judged, and shown, only for the run the user is looking at.
    log_rows = log_tail(jid, 20)
    start_idx = 0
    for i, ln in enumerate(log_rows):
        if ln.startswith("recording "):
            start_idx = i
    current_run = log_rows[start_idx:]
    log_error = any("Traceback" in ln or "Error" in ln for ln in current_run)
    log_rows = current_run
    void = (not running) and voids.is_void(jd)  # no video at all: the pruner will discard it
    monitor_meta = None
    if (jd / "monitor.json").is_file():
        try:
            monitor_meta = common.load_json(jd / "monitor.json")
        except Exception:
            monitor_meta = None
    spike_rows = parse_spikes(ld)
    return {"id": jid, "url": url, "platform": plat, "channel": chan, "title": title,
            "status": "running" if running else ("done" if live_meta else "stopped"),
            "pid": entry.get("pid") if running else 0, "minutes_planned": entry.get("minutes"),
            "started": started, "elapsed_min": elapsed, "void": void, "log_error": log_error,
            "spikes": live_meta.get("spikes"), "minutes": live_meta.get("minutes"),
            "segments": len(segs), "mb": size_mb, "last_activity": last_seg,
            "chat_msgs": chat, "chat_last_ts": chat_last_ts,
            "spike_rows": spike_rows, "log": log_rows,
            "ended_at": None if running else voids.ended_at(jd),
            "monitor_meta": monitor_meta, "pipeline": pipeline_for(jid),
            "feedback": {f"{r.get('utc')}|{r.get('offset')}": r.get("verdict")
                         for r in spike_feedback.load() if r.get("job") == jid},
            "spike_rows_session": session_spikes(spike_rows, started)}


def _moment_hook(text, n):
    t = " ".join((text or "").split())
    if not t:
        return f"Momento {n}"
    return (t[:70] + "…") if len(t) > 70 else t


def _moment_cards(jd, cands, run):
    """Cards for the Opus grid (spec D2/D10): best score first, honest image chain,
    `pick` marking the auto-recommended moment when the run ran in auto mode."""
    cards = []
    for c in cands:
        n = int(c.get("n") or 0)
        llm = c.get("llm") or {}
        kind = thumb_kind_for(jd, n)
        cards.append({"n": n, "start": c.get("start"), "end": c.get("end"),
                      "dur": round((c.get("end") or 0) - (c.get("start") or 0), 1),
                      "score": c.get("score"), "peak": c.get("peak"),
                      "why": c.get("why") or [], "text": (c.get("text") or "")[:160],
                      "llm": llm or None,
                      "hook": str(llm.get("hook_title") or "").strip() or _moment_hook(c.get("text"), n),
                      "hook_type": str(llm.get("hook_type") or ""),
                      "thumb": f"/api/moment-thumb?job={jd.name}&n={n}" if kind else None,
                      "thumb_kind": kind})
    cards.sort(key=lambda m: -float((m["llm"] or {}).get("total_score") or m["score"] or 0))
    if cards and run and ((run.get("options") or {}).get("mode") == "auto"):
        cards[0]["pick"] = True
    return cards


def split_moment(jd, pick, at=None, start=None, end=None):
    """Split a moment at one second (retitle 'dividir ao meio') or clone it with
    operator-adjusted limits as a NEW moment — the original stays intact (spec D3/G5).
    Exactly one of `at` or (start, end) must be given."""
    if (at is None) == (start is None and end is None):
        raise ValueError("informe `at` ou os limites start/end (apenas um modo)")
    jd = Path(jd)
    cand_path = jd / "candidates.json"
    if not cand_path.exists():
        raise ValueError("este job não tem candidatos")
    data = common.load_json(cand_path)
    cand = next((c for c in data.get("candidates", []) if int(c.get("n") or 0) == int(pick or 0)), None)
    if not cand:
        raise ValueError("momento inválido")
    s, e = float(cand.get("start") or 0), float(cand.get("end") or 0)
    new_n = max(int(c.get("n") or 0) for c in data["candidates"]) + 1
    if at is not None:
        at = float(at)
        if not s + 1.0 < at < e - 1.0:
            raise ValueError("o ponto de divisão precisa ficar dentro do momento (pelo menos 1 s de cada lado)")
        first, second = dict(cand), dict(cand)
        first["end"], second["start"] = round(at, 2), round(at, 2)
        for part in (first, second):
            part.pop("llm", None)
        second["n"] = new_n
        second["why"] = ["divisão manual"]
        idx = data["candidates"].index(cand)
        data["candidates"][idx] = first  # replaces the original with the trimmed half
        data["candidates"].insert(idx + 1, second)
    else:
        if start is None or end is None:
            raise ValueError("informe início e fim do trecho")
        start, end = float(start), float(end)
        if not s - 0.5 <= start < end <= e + 0.5:
            raise ValueError("os limites precisam ficar dentro do momento original")
        variant = dict(cand)
        variant.pop("llm", None)
        variant["n"] = new_n
        variant["start"], variant["end"] = round(start, 2), round(end, 2)
        variant["dur"] = round(end - start, 1)
        variant["why"] = ["ajuste manual no player"]
        data["candidates"].append(variant)
    common.save_json(cand_path, data)
    return {"ok": True, "new_n": new_n}


def creation_info(clips_by_job=None):
    jobs = []
    if not JOBS.is_dir():
        return jobs
    for jd in JOBS.iterdir():
        try:
            mtime = jd.stat().st_mtime
        except FileNotFoundError:
            continue
        if not jd.is_dir():
            continue
        if jd.name.startswith("viral-"):
            continue  # compilações do Vídeo viral têm canal próprio em viral_info()
        cands = []
        candidate_source = ""
        if (jd / "candidates.json").exists():
            try:
                candidate_data = common.load_json(jd / "candidates.json")
                cands = candidate_data.get("candidates") or []
                candidate_source = str(candidate_data.get("source") or "")
            except Exception:
                pass
        spikes = parse_spikes(jd / "live") if (jd / "live").is_dir() else []
        vids = list(jd.glob("*.mp4")) + list(jd.glob("*.ts"))
        clips = clip_info(jd) if clips_by_job is None else clips_by_job.get(jd.name, [])
        run = creation_run(jd) if clips_by_job is None else creation_run(jd, clips)
        try:
            cleanup = common.load_json(jd / "cleanup.json")
        except (OSError, ValueError):
            cleanup = None
        try:
            cand_mtime = int((jd / "candidates.json").stat().st_mtime)
        except OSError:
            cand_mtime = 0
        source_rel = ""
        if candidate_source and Path(candidate_source).is_file():
            try:
                source_rel = Path(candidate_source).relative_to(ROOT).as_posix()
            except ValueError:
                source_rel = ""
        jobs.append({"id": jd.name, "mtime": int(mtime),
                     "cand_mtime": cand_mtime,
                     "current": False,
                     "source_path": source_rel,
                     "source": _info_title(jd) or (vids[0].name if vids else None),
                     "candidates": [{"n": c.get("n"), "start": c.get("start"), "end": c.get("end"),
                                     "dur": round((c.get("end") or 0) - (c.get("start") or 0), 1),
                                     "score": c.get("score"), "peak": c.get("peak"),
                                     "why": c.get("why") or [], "text": (c.get("text") or "")[:160],
                                     "llm": c.get("llm") or None}
                                    for c in cands],
                     "moments": _moment_cards(jd, cands, run),
                     "compile": compile_run(jd),
                     "clips": clips, "spikes": len(spikes), "run": run,
                     "source_available": bool(candidate_source and Path(candidate_source).is_file()),
                     "cleanup": cleanup})
    jobs.sort(key=lambda j: j["mtime"], reverse=True)
    with_candidates = [j for j in jobs if j["candidates"]]
    if with_candidates:  # spec G7, by candidates.json mtime — dir mtime shifts on thumb extraction
        best = max(with_candidates, key=lambda j: j["cand_mtime"])
        best["current"] = True
    return jobs


def parse_streams_md():
    p = ROOT / "streams.md"
    data_path = p.with_suffix(".json")
    if data_path.exists():
        try:
            data = common.load_json(data_path)
            by_name = {str(s.get("platform") or ""): s for s in data.get("sections", [])}
            data["sections"] = [by_name.get(name, {"platform": name, "rows": []})
                                 for name in ("YouTube", "Twitch", "Kick")]
            return data
        except Exception:
            pass
    if not p.exists():
        return {"checked": None, "query": None, "sections": []}
    text = p.read_text(encoding="utf-8", errors="replace")
    qm = re.match(r"# Live streams: (.+)", text)
    checked = re.search(r"Checked ([\d\-]+ [\d:]+)", text)
    sections = []
    cur = None
    for ln in text.splitlines():
        if ln.startswith("## "):
            cur = {"platform": ln[3:].strip(), "rows": [], "note": ""}
            sections.append(cur)
        elif cur is not None and ln.startswith("> ⚠️"):
            cur["note"] = ln.lstrip("> ").removeprefix("⚠️").strip()
        elif cur is not None and ln.startswith("|") and "---" not in ln and not ln.startswith("| #"):
            c = [x.strip() for x in ln.strip("|").split("|")]
            if len(c) >= 6:
                cur["rows"].append({"channel": c[1], "title": c[2], "viewers": c[3],
                                    "live_for": c[4], "url": c[5]})
    by_name = {str(s.get("platform") or ""): s for s in sections}
    sections = [by_name.get(name, {"platform": name, "rows": []}) for name in ("YouTube", "Twitch", "Kick")]
    return {"checked": checked.group(1) if checked else None, "query": qm.group(1) if qm else None,
            "sections": sections}


def monitor_entries():
    rows = load_watchlist()
    seen = {r["id"] for r in rows}
    if JOBS.is_dir():
        for jd in JOBS.iterdir():
            if jd.name not in seen and (jd / "live").is_dir():
                rows.append({"id": jd.name, "url": "", "pid": 0, "minutes": None, "started": None})
    return rows


def monitor_urls():
    urls = set()
    for row in monitor_entries():
        url = row.get("url")
        if not url:
            try:
                url = common.load_json(JOBS / row["id"] / "live" / "live.json").get("url")
            except (OSError, ValueError, AttributeError):
                continue
        if isinstance(url, str) and url:
            urls.add(url)
    return sorted(urls)


def full_state():
    monitors = [monitor_info(r) for r in monitor_entries()]
    for m in monitors:
        m["card"] = card_state(m)  # derived server-side; the client only renders
    clips_by_job = {}
    if JOBS.is_dir():
        for jd in sorted(JOBS.iterdir()):
            if jd.is_dir():
                clips_by_job[jd.name] = clip_info(jd)
    clips = [clip for job_clips in clips_by_job.values() for clip in job_clips]
    clips.sort(key=lambda c: c["mtime"], reverse=True)
    discarded_rows = voids.discarded_rows()
    discarded = {"rows": discarded_rows[:20], "count": len(discarded_rows),
                 "mb": round(sum(float(r.get("mb") or 0) for r in discarded_rows), 1)}
    return {"now": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            "clips": clips, "monitors": monitors, "creation": creation_info(clips_by_job),
            "viral": viral_info(),
            "discarded": discarded, "performance": performance_summary(),
            "settings": settings_state(), "streams": parse_streams_md()}


def storage_summary():
    """Summarize jobs/ disk use for the dashboard without exposing file contents."""
    jobs = []
    kinds = {"Capturas da live (.ts)": 0, "Vídeos e cortes (.mp4)": 0,
             "Áudio de análise (.wav)": 0, "Outros arquivos": 0}
    files = []
    total = 0
    if JOBS.is_dir():
        for jd in JOBS.iterdir():
            if not jd.is_dir():
                continue
            job_bytes = 0
            count = 0
            for f in jd.rglob("*"):
                if not f.is_file():
                    continue
                try:
                    size = f.stat().st_size
                except OSError:
                    continue
                job_bytes += size
                total += size
                count += 1
                suffix = f.suffix.lower()
                if suffix == ".ts":
                    kinds["Capturas da live (.ts)"] += size
                elif suffix in (".mp4", ".mkv", ".mov"):
                    kinds["Vídeos e cortes (.mp4)"] += size
                elif suffix == ".wav":
                    kinds["Áudio de análise (.wav)"] += size
                else:
                    kinds["Outros arquivos"] += size
                if size >= 5_000_000:
                    files.append({"path": f.relative_to(ROOT).as_posix(), "bytes": size,
                                  "mb": round(size / 1e6, 1), "job": jd.name})
            if job_bytes:
                jobs.append({"id": jd.name, "bytes": job_bytes, "mb": round(job_bytes / 1e6, 1),
                             "files": count})
    jobs.sort(key=lambda x: x["bytes"], reverse=True)
    files.sort(key=lambda x: x["bytes"], reverse=True)
    return {"bytes": total, "gb": round(total / 1e9, 2), "mb": round(total / 1e6, 1),
            "files": sum(j["files"] for j in jobs), "jobs_count": len(jobs), "jobs": jobs[:12],
            "largest": files[:12],
            "breakdown": [{"label": label, "bytes": size, "mb": round(size / 1e6, 1),
                           "percent": round(size / total * 100, 1) if total else 0}
                          for label, size in kinds.items()],
            "checked": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")}


def youtube_suggest_context(file_rel):
    """Contexto para a IA sugerir título/descrição de UM arquivo no YouTube. Aceita cortes
    (jobs/<id>/edits/clip_<n>[_<lang>].mp4), compilações (…/compile/melhores.mp4) e origens.
    Devolve None quando o arquivo não é reconhecido."""
    posix = str(file_rel or "").replace("\\", "/").strip()
    if posix and not posix.startswith("jobs/"):  # clip.file vem relativo a jobs/ (formato /media/)
        posix = "jobs/" + posix
    try:
        path = (ROOT / posix).resolve()
        path.relative_to(JOBS.resolve())
    except (ValueError, OSError):
        return None
    if not path.is_file():
        return None
    m = re.fullmatch(r"jobs/([^/]+)/edits/(clip_\d+)(?:_[a-z]{2})?\.mp4", posix)
    ctx = {"kind": "vídeo", "extras": []}
    if m:
        jd = JOBS / m.group(1)
        for c in clip_info(jd):
            if c.get("file") == posix or f"jobs/{c.get('file')}" == posix:  # clip.file é relativo a jobs/
                ctx.update({"kind": "corte vertical" if c.get("orientation") != "horizontal" else "corte horizontal",
                            "hook": c.get("hook"), "dur": c.get("dur"),
                            "layout": c.get("layout"), "lang": c.get("lang"),
                            "score": c.get("llm_total"), "hook_type": c.get("hook_type"),
                            "source": c.get("source")})
                post = c.get("posting") or {}
                if post.get("title_suggestions"):
                    ctx["extras"] = post["title_suggestions"][:3]
                if (post.get("platforms") or {}).get("YouTube Shorts", {}).get("description"):
                    ctx["current_description"] = post["platforms"]["YouTube Shorts"]["description"]
                break
    elif posix.endswith("compile/melhores.mp4"):
        jid = posix.split("/")[1]
        comp = compile_run(JOBS / jid) or {}
        ctx.update({"kind": "compilação (melhores momentos)", "hook": comp.get("title"),
                    "dur": comp.get("duration"), "current_description": comp.get("description")})
        try:
            plan = common.load_json(JOBS / jid / "compile" / "plan.json")
            entries = plan.get("moments") or plan.get("entries") or []
            ctx["extras"] = [str(e.get("title") or "") for e in entries[:8] if e.get("title")]
        except (OSError, ValueError):
            pass
    else:
        jid = posix.split("/")[1] if posix.startswith("jobs/") and len(posix.split("/")) > 2 else ""
        jd = JOBS / jid if jid else None
        if jd and jd.is_dir():
            info = common.load_json(jd / "info.json") if (jd / "info.json").exists() else {}
            ctx.update({"hook": info.get("title"), "source": info.get("channel") or info.get("uploader")})
            cands = (common.load_json(jd / "candidates.json") or {}).get("candidates") or []
            hooks = [str((c.get("llm") or {}).get("hook_title") or c.get("text") or "")[:80]
                     for c in sorted(cands, key=score_of_llm, reverse=True)[:3]]
            ctx["extras"] = [h for h in hooks if h]
        else:
            return None
    return ctx


def score_of_llm(c):
    return float((c.get("llm") or {}).get("total_score") or c.get("score") or 0)


def draft_posting(candidate, info, title="", credit=""):
    """Draft platform copy from the selected moment's transcript; always mark it for review."""
    text = re.sub(r"\s+", " ", str(candidate.get("text") or "")).strip(" .!?-—")
    text = re.sub(r"\b(?:um|uh|eh|tipo|né)\b", "", text, flags=re.I)
    text = re.sub(r"\s+", " ", text).strip(" .!?-—")
    quote = text.split(". ", 1)[0].strip()[:76].rstrip(" ,;:-")
    source_title = str(info.get("title") or "GTA 6")
    channel = credit.strip() or str(info.get("channel") or info.get("uploader") or "")
    source_url = str(info.get("webpage_url") or info.get("url") or "")
    if not source_url.lower().startswith(("http://", "https://")):
        source_url = ""
    chosen = title.strip() or quote or source_title
    titles = [chosen[:100]]
    if quote and quote.lower() != chosen.lower():
        titles.append(quote[:100])
    if channel:
        titles.append(f"{channel} reage a este momento de GTA 6"[:100])
    else:
        titles.append("A reação a este momento de GTA 6"[:100])
    titles = list(dict.fromkeys(x for x in titles if x))[:3]
    credit_line = f"Crédito: {channel}" if channel else ""
    source_line = f"Fonte: {source_url}" if source_url else ""
    summary = quote or chosen
    tags = ["#GTA6", "#GTAVI", "#games"]
    low = f"{source_title} {text}".lower()
    if any(word in low for word in ("reage", "reação", "react", "reagiu")):
        tags.append("#reação")
    if any(word in low for word in ("momento", "aconteceu", "olha", "inacredit")):
        tags.append("#momentogamer")
    tags = list(dict.fromkeys(tags))
    return {
        "review_note": "Rascunhos baseados na fala transcrita. Revise nomes e contexto; não há garantia de alcance.",
        "title_suggestions": titles,
        "platforms": {
            "TikTok": {"title": chosen[:100], "description": "\n".join(x for x in [summary, "Você teria reagido igual?", credit_line] if x), "hashtags": tags + ["#TikTok"]},
            "YouTube Shorts": {"title": chosen[:100], "description": "\n".join(x for x in [summary, "Assista até o final e conte o que você achou.", credit_line, source_line] if x), "hashtags": tags + ["#Shorts"]},
            "Instagram Reels": {"title": chosen[:100], "description": "\n".join(x for x in [summary, "Qual foi a melhor parte para você?", credit_line] if x), "hashtags": tags + ["#Reels"]},
        },
    }


def allowed_video_url(url):
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return False
    host = (parsed.hostname or "").lower().rstrip(".")
    if host in {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}:
        return True
    path = parsed.path
    if host in {"twitch.tv", "www.twitch.tv", "m.twitch.tv"}:
        return bool(re.match(r"^/videos/\d+", path))
    if host in {"kick.com", "www.kick.com"}:
        return bool(re.match(r"^/[^/]+/videos/[^/]+", path))
    return False


def existing_sources():
    rows = []
    if not JOBS.is_dir():
        return rows
    for jd in JOBS.iterdir():
        if not jd.is_dir():
            continue
        for f in jd.rglob("*"):
            if not f.is_file() or "edits" in f.relative_to(jd).parts:
                continue
            if f.suffix.lower() not in {".mp4", ".mkv", ".mov", ".webm"}:
                continue
            if re.fullmatch(r"clip_\d+(?:_[A-Za-z0-9]{2,8})?", f.stem):
                continue
            try:
                st = f.stat()
            except OSError:
                continue
            rows.append({"path": f.relative_to(ROOT).as_posix(), "job": jd.name,
                         "name": f.name, "title": _info_title(jd) or jd.name,
                         "mb": round(st.st_size / 1e6, 1), "mtime": int(st.st_mtime)})
    return sorted(rows, key=lambda x: x["mtime"], reverse=True)[:100]


WHISPER_MODELS = {"fast": "base", "balanced": "small", "accurate": "medium"}
CREATION_LOCK = threading.RLock()

COMPILE_LOCK = threading.RLock()  # melhores momentos: spawn + reconcile of compile.json


def compile_run(jd):
    """compile.json with the same pid-liveness reconciliation as creation_run: the child only
    patches stage/done/total, so the dashboard decides running/ready/failed on its own."""
    path = Path(jd) / "compile.json"
    try:
        data = common.load_json(path)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    if pid_alive(data.get("pid")):
        data["status"] = "running"
    elif data.get("status") in ("running", None):
        output = str(data.get("output") or "")
        data["status"] = "ready" if output and (ROOT / output).is_file() else "failed"
        if data["status"] == "failed" and not data.get("error"):
            data["error"] = "o processamento terminou sem produzir o vídeo"
        with COMPILE_LOCK:
            try:
                current = common.load_json(path)
                if current.get("pid") == data.get("pid") and current.get("status") in ("running", None):
                    current.update({"pid": 0, "status": data["status"]})
                    common.save_json(path, current)
            except (OSError, ValueError, AttributeError):
                pass
        data["pid"] = 0
    data["log"] = log_tail(jd.name, 12, comp=True)
    return data


def start_compile(jid, options=None):
    jd = (JOBS / jid).resolve()
    try:
        jd.relative_to(JOBS.resolve())
    except ValueError:
        raise RuntimeError("job inválido")
    if not (jd / "candidates.json").exists():
        raise RuntimeError("analise esta fonte antes de gerar os melhores momentos")
    current = compile_run(jd)
    if current and current.get("status") == "running":
        raise RuntimeError("os melhores momentos já estão sendo gerados para este job")
    LOGS.mkdir(exist_ok=True)
    with open(LOGS / f"compile_{jd.name}.log", "w", encoding="utf-8") as fh:
        proc = _spawn_detached([PYTHON, str(ROOT / "scripts" / "compile.py"), str(jd),
                                json.dumps(options or {}, ensure_ascii=False)], fh, child_env())
    data = {"pid": proc.pid, "status": "running", "stage": "planejando os momentos", "done": 0,
            "total": 0, "options": options or {},
            "started": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    common.save_json(jd / "compile.json", data)
    return data


def start_viral(options=None):
    """Vídeo viral: momentos de VÁRIOS projetos num único 1080p. jobs/viral-<stamp>/ é um
    job sintético — só compile.json + compile/ (plan.json, melhores.mp4, thumb, legendas) —
    então compile_run()/compile_run reconciliation funcionam sem mudança."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    jd = JOBS / f"viral-{stamp}"
    jd.mkdir(parents=True, exist_ok=True)
    LOGS.mkdir(exist_ok=True)
    with open(LOGS / f"compile_{jd.name}.log", "w", encoding="utf-8") as fh:
        proc = _spawn_detached([PYTHON, str(ROOT / "scripts" / "compile.py"), str(jd),
                                json.dumps(options or {}, ensure_ascii=False)], fh, child_env())
    data = {"pid": proc.pid, "status": "running", "stage": "planejando os momentos", "done": 0,
            "total": 0, "options": options or {}, "multi": True,
            "started": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    common.save_json(jd / "compile.json", data)
    return data


def viral_info():
    """Estado dos vídeos virais para /api/state — mais recente primeiro."""
    items = []
    if JOBS.is_dir():
        for jd in sorted(JOBS.glob("viral-*")):
            if not jd.is_dir():
                continue
            comp = compile_run(jd)
            if comp:
                items.append({"id": jd.name, "compile": comp})
    items.sort(key=lambda v: str(v["compile"].get("started") or ""), reverse=True)
    return items


def start_creation_analysis(src, display, top, max_minutes, options=None):
    jd = common.job_dir(src)
    current = creation_run(jd)
    if current and current.get("status") in {"analyzing", "rendering"}:
        raise RuntimeError("esta fonte já está sendo analisada ou os cortes ainda estão sendo gerados")
    shutil.rmtree(jd / "thumbs", ignore_errors=True)  # spec D9: new analysis, new frames
    LOGS.mkdir(exist_ok=True)
    env = child_env()
    model = WHISPER_MODELS.get(str((options or {}).get("model") or ""))
    if model:
        env["GTAVI_WHISPER_MODEL"] = model
    with open(LOGS / f"create_{jd.name}.log", "w", encoding="utf-8") as fh:
        proc = _spawn_detached([PYTHON, str(ROOT / "scripts" / "analyze_creation.py"), str(src),
                                str(top), str(max_minutes), json.dumps(options or {}, ensure_ascii=False)],
                               fh, env)
    data = {"id": jd.name, "url": display if str(src).startswith("http") else "",
            "source_path": "" if str(src).startswith("http") else str(src),
            "pid": proc.pid, "status": "analyzing", "top": top,
            "max_minutes": max_minutes,
            "options": options or {"mode": "manual", "orientation": "vertical", "duration": "auto"},
            "started": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    common.save_json(jd / "creation.json", data)
    return data


def creation_run(job_d, clips=None):
    path = job_d / "creation.json"
    try:
        data = common.load_json(path)
    except Exception:
        return None
    running = pid_alive(data.get("pid"))
    has_candidates = (job_d / "candidates.json").exists()
    if running:
        data["status"] = "rendering" if has_candidates and (data.get("options") or {}).get("mode") == "auto" else "analyzing"
    else:
        auto_failed = (data.get("options") or {}).get("mode") == "auto" and not (
            clip_info(job_d) if clips is None else clips)
        data["status"] = "ready" if has_candidates and not auto_failed else "failed"
    if not running and data.get("pid"):
        with CREATION_LOCK:
            try:
                current = common.load_json(path)
                if current.get("pid") == data["pid"] and current.get("started") == data.get("started"):
                    current.update({"pid": 0, "status": data["status"]})
                    common.save_json(path, current)
            except (OSError, ValueError, AttributeError):
                pass
        data["pid"] = 0
    data["log"] = log_tail(job_d.name, 18, creation=True)
    return data


def cleanup_preview(job_d):
    """Only list source and analysis media; never include finished edits or metadata."""
    files = []
    for f in job_d.rglob("*"):
        if not f.is_file() or "edits" in f.relative_to(job_d).parts:
            continue
        suffix = f.suffix.lower()
        if suffix not in {".ts", ".wav", ".m4a", ".mp3", ".mp4", ".mkv", ".mov"}:
            continue
        try:
            size = f.stat().st_size
        except OSError:
            continue
        if size:
            files.append({"path": f, "bytes": size})
    return {"files": len(files), "bytes": sum(f["bytes"] for f in files),
            "mb": round(sum(f["bytes"] for f in files) / 1e6, 1), "items": files}


def perform_cleanup(jd):
    """Apaga origens/análise de UM projeto preservando edits/ — usado pelo cleanup individual
    e pelo lote de "Liberar espaço". Devolve o payload da resposta com o código HTTP embutido."""
    if not clip_info(jd):
        return {"error": "crie ao menos um corte final antes de limpar os arquivos de origem", "_status": 400}
    run = creation_run(jd)
    if run and run.get("status") in {"analyzing", "rendering"}:
        return {"error": "aguarde o processamento terminar antes de liberar espaço", "_status": 409}
    if voids.recording_running(jd) or any(row.get("id") == jd.name and watch_running(row) for row in load_watchlist()):
        return {"error": "pare a gravação antes de liberar espaço", "_status": 409}
    if pipeline_for(jd.name):
        return {"error": "aguarde a pós-captura terminar antes de liberar espaço", "_status": 409}
    preview = cleanup_preview(jd)
    for item in preview["items"]:
        try:
            item["path"].unlink()
        except OSError as exc:
            locked = getattr(exc, "winerror", None) == 32 or "being used" in str(exc).lower()
            if locked:
                return {"error": "o vídeo ainda está em uso (corte em andamento ou player aberto). Feche a reprodução e tente de novo.", "_status": 409}
            return {"error": f"não foi possível remover {item['path'].name}: {exc}", "_status": 500}
    common.save_json(jd / "cleanup.json", {
        "files_removed": preview["files"], "bytes_freed": preview["bytes"],
        "cleaned": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    return {"ok": True, "files_removed": preview["files"], "mb_freed": preview["mb"]}


def cleanup_all_preview():
    """"Liberar espaço" em lote: quanto daria para recuperar nos projetos que já têm cortes
    prontos (origem ainda em disco e nada rodando). Nunca inclui edits/."""
    jobs, total = [], 0
    if JOBS.is_dir():
        for jd in sorted(JOBS.iterdir()):
            if not jd.is_dir() or jd.name.startswith("viral-"):
                continue
            if not clip_info(jd):
                continue
            preview = cleanup_preview(jd)
            if preview["files"]:
                jobs.append({"id": jd.name, "files": preview["files"], "mb": preview["mb"]})
                total += preview["bytes"]
    return {"total_mb": round(total / 1e6, 1), "jobs": jobs}


# ---------- viewers ----------

def fetch_twitch(login):
    if not tw:
        return None
    q = """query($l:String!){ user(login:$l){ displayName stream{ viewersCount title createdAt } } }"""
    d = tw.gql(q, {"l": login})
    s = ((d.get("data") or {}).get("user") or {}).get("stream")
    return {"viewers": s.get("viewersCount") if s else 0, "title": s.get("title") if s else None, "live": bool(s)}


def fetch_viewers(url):
    try:
        if "twitch.tv" in url:
            login = urlparse(url).path.strip("/").split("/")[0].lower()
            d = fetch_twitch(login)
            return (d or {}).get("viewers") if d else None
        info = common.ytdlp_json(url)
        return info.get("concurrent_view_count") if info.get("is_live") else 0
    except Exception:
        return None


def viewers_snapshot(urls, force=False):
    todo = [u for u in urls if u and (force or u not in VIEWERS
            or time.time() - VIEWERS[u].get("_ts", 0) > VIEWER_TTL)]
    if todo:
        with ThreadPoolExecutor(max_workers=4) as ex:
            res = dict(zip(todo, ex.map(fetch_viewers, todo)))
        for u in todo:
            host = (urlparse(u).hostname or "").lower()
            plat = "Twitch" if "twitch.tv" in host else ("Kick" if "kick.com" in host else "YouTube")
            VIEWERS[u] = {"viewers": res.get(u), "platform": plat, "_ts": time.time(),
                          "checked": datetime.now(timezone.utc).strftime("%H:%M:%S UTC")}
    now = time.time()
    return {u: {k: v for k, v in VIEWERS.get(u, {}).items() if k != "_ts"} | {"age_s": int(now - VIEWERS[u].get("_ts", now))} for u in urls if u in VIEWERS}


# ---------- HTTP ----------

class QuietHTTPServer(ThreadingHTTPServer):
    """A dashboard tab refreshing mid-response aborts connections; that must never
    take the whole panel down (it did twice today)."""
    daemon_threads = True

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, BrokenPipeError, TimeoutError, ConnectionAbortedError)):
            return
        super().handle_error(request, client_address)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _youtube_local(self, *, write=False):
        port = self.server.server_port
        host = self.headers.get("Host")
        if host not in {f"127.0.0.1:{port}", f"localhost:{port}"}:
            return False
        if not write:
            return True
        origin = self.headers.get("Origin")
        return (self.headers.get_content_type() == "application/json" and
                (origin is None or origin == f"http://{host}"))

    def _youtube_callback(self, query):
        message = "Canal do YouTube conectado."
        try:
            YOUTUBE.callback(parse_qs(query))
        except ValueError as error:
            message = str(error)
        except Exception:
            message = "Não foi possível concluir a conexão com o YouTube."
        self.send_response(303)
        self.send_header("Location", "/?" + urlencode({"youtube_notice": message}) + "#youtube")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def _file(self, path):
        size = path.stat().st_size
        ctype = MIME.get(path.suffix.lower(), "application/octet-stream")
        rng = self.headers.get("Range")
        start, end = 0, size - 1
        code = 200
        if rng and "," not in rng:
            m = re.fullmatch(r"bytes=([0-9]{0,20})-([0-9]{0,20})", rng)
            if m and (m.group(1) or m.group(2)):
                code = 206
                if m.group(1):
                    start = int(m.group(1))
                    if m.group(2):
                        end = min(int(m.group(2)), size - 1)
                else:
                    start = max(0, size - int(m.group(2)))
                if start >= size or start > end:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
        chunk = 256 * 1024
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        if ctype.startswith("text/") or path.suffix.lower() in {".js", ".css", ".html"}:
            self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(end - start + 1))
        if code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        try:
            with open(path, "rb") as fh:
                fh.seek(start)
                left = end - start + 1
                while left > 0:
                    buf = fh.read(min(chunk, left))
                    if not buf:
                        break
                    self.wfile.write(buf)
                    left -= len(buf)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, FileNotFoundError):
            pass

    def do_GET(self):
        try:
            self._get()
        except FileNotFoundError:
            self._json({"error": "arquivo não encontrado"}, 404)
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
            pass
        except Exception:
            self.server.handle_error(self.request, self.client_address)
            self._json({"error": "falha ao ler os dados do painel"}, 500)

    def _get(self):
        target = urlparse(self.path)
        path = target.path
        if path.startswith("/api/youtube/"):
            if not self._youtube_local():
                return self._json({"error": "Abra o dashboard pelo endereço local."}, 403)
            if path == "/api/youtube/callback":
                return self._youtube_callback(target.query)
            if path == "/api/youtube/state":
                return self._json(YOUTUBE.state())
        if path in ("/", "/index.html"):
            return self._file(STATIC / "index.html")
        if path.startswith("/static/"):
            f = (STATIC / unquote(path[len("/static/"):])).resolve()
            try:
                f.relative_to(STATIC.resolve())
            except ValueError:
                return self._json({"error": "forbidden"}, 403)
            if f.is_file():
                return self._file(f)
            return self._json({"error": "not found"}, 404)
        if path == "/api/state":
            return self._json(full_state())
        if path == "/api/storage":
            return self._json(storage_summary())
        if path == "/api/sources":
            return self._json(existing_sources())
        if path == "/api/create/status":
            jid = parse_qs(target.query).get("id", [""])[0]
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", jid):
                return self._json({"error": "job inválido"}, 400)
            jd = (JOBS / jid).resolve()
            try:
                jd.relative_to(JOBS.resolve())
            except ValueError:
                return self._json({"error": "forbidden"}, 403)
            data = creation_run(jd)
            return self._json(data or {"error": "análise não encontrada"}, 200 if data else 404)
        if path == "/api/create/cleanup-preview":
            jid = parse_qs(target.query).get("id", [""])[0]
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", jid):
                return self._json({"error": "job inválido"}, 400)
            jd = (JOBS / jid).resolve()
            try:
                jd.relative_to(JOBS.resolve())
            except ValueError:
                return self._json({"error": "forbidden"}, 403)
            if not jd.is_dir():
                return self._json({"error": "projeto não encontrado"}, 404)
            preview = cleanup_preview(jd)
            preview.pop("items", None)
            return self._json(preview)
        if path == "/api/create/cleanup-all-preview":
            return self._json(cleanup_all_preview())
        if path == "/api/viewers":
            params = parse_qs(target.query)
            force = params.get("force") == ["1"]
            urls = monitor_urls()
            if "url" in params:
                url = params["url"][0]
                if url not in urls:
                    return self._json({"error": "live não encontrada no monitoramento"}, 404)
                urls = [url]
            return self._json(viewers_snapshot(urls, force))
        if path == "/api/hooks":
            f = ROOT / "log.md"
            try:
                lines = f.read_text(encoding="utf-8", errors="replace").splitlines()[-20:]
            except OSError:
                lines = []
            return self._json(lines)
        if path == "/api/moment-thumb":
            q = parse_qs(target.query)
            try:
                jd = job_row_for(q.get("job", [""])[0])
                n = int(q.get("n", ["0"])[0])
            except (ValueError, TypeError):
                return self._json({"error": "job/n inválido"}, 400)
            cand = _moment_cand(jd, n)
            if not cand:
                return self._json({"error": "momento não encontrado"}, 404)
            made = moment_thumb(jd, cand)
            if not made or not made[0].exists():
                return self._json({"error": "sem imagem para este momento"}, 404)
            return self._file(made[0])
        if path.startswith("/media/"):
            rel = unquote(path[len("/media/"):])
            if rel.startswith("jobs/"):
                rel = rel[len("jobs/"):]
            f = (JOBS / rel).resolve()
            try:
                f.relative_to(JOBS.resolve())
            except ValueError:
                return self._json({"error": "forbidden"}, 403)
            if f.is_file():
                return self._file(f)
            return self._json({"error": "not found"}, 404)
        return self._json({"error": "not found"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
        if path.startswith("/api/youtube/") and not self._youtube_local(write=True):
            return self._json({"error": "Faça esta operação pelo dashboard local."}, 403)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if not 0 <= n <= 1024 * 1024:
                return self._json({"error": "tamanho da requisição inválido"}, 400)
            body = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(body, dict):
                return self._json({"error": "envie um objeto JSON"}, 400)
        except (OSError, ValueError):
            return self._json({"error": "JSON inválido"}, 400)
        try:
            if path == "/api/youtube/connect":
                redirect_uri = f"http://127.0.0.1:{self.server.server_port}/api/youtube/callback"
                return self._json({"url": YOUTUBE.authorize(body, redirect_uri)})
            if path == "/api/youtube/disconnect":
                YOUTUBE.disconnect()
                return self._json({"ok": True})
            if path == "/api/youtube/upload":
                return self._json({"id": YOUTUBE.start(body)}, 202)
            if path == "/api/youtube/folder":
                return self._json({"path": str(YOUTUBE.inbox(create=True))})
            if path == "/api/youtube/suggest":
                cfg = common.llm_config()
                if not cfg:
                    return self._json({"error": "configure o LLM na aba Ajustes para usar a IA"}, 400)
                ctx = youtube_suggest_context(body.get("file"))
                if not ctx:
                    return self._json({"error": "não encontrei dados de análise para este arquivo"}, 400)
                system = ("Você é editor de conteúdo de games no Brasil. Responda SOMENTE com um JSON "
                          'válido no formato {"title": "...", "description": "..."}. O título tem até 100 '
                          "caracteres, em PT-BR, direto e clicável sem mentir, sem aspas duplas e sem <>. "
                          "A descrição tem até 5000 caracteres: 2 a 4 linhas curtas em PT-BR terminando com "
                          "3 a 5 hashtags relevantes em linhas separadas. Não invente fatos que não estão no contexto.")
                user_lines = [f"Tipo de vídeo: {ctx.get('kind')}"]
                if ctx.get("hook"):
                    user_lines.append(f"Gancho/título atual: {ctx['hook']}")
                if ctx.get("source"):
                    user_lines.append(f"Fonte/canal: {ctx['source']}")
                if ctx.get("dur"):
                    user_lines.append(f"Duração: {ctx['dur']}s")
                if ctx.get("hook_type"):
                    user_lines.append(f"Tipo de hook: {ctx['hook_type']}")
                if ctx.get("score"):
                    user_lines.append(f"Score de viralidade: {ctx['score']}")
                if ctx.get("extras"):
                    user_lines.append("Sugestões existentes: " + " | ".join(ctx["extras"]))
                if ctx.get("current_description"):
                    user_lines.append("Descrição atual: " + str(ctx["current_description"])[:800])
                payload = json.dumps({"model": cfg["model"], "temperature": 0.5, "max_tokens": 800,
                                      "messages": [{"role": "system", "content": system},
                                                   {"role": "user", "content": "\n".join(user_lines)}]}).encode("utf-8")
                req = urllib.request.Request(cfg["base"] + "/chat/completions", data=payload, method="POST",
                                             headers={"Content-Type": "application/json",
                                                      "Authorization": f"Bearer {cfg['key']}"})
                try:
                    with urllib.request.urlopen(req, timeout=60) as resp:
                        data = json.loads(resp.read().decode("utf-8"))
                    reply = str(((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
                    reply = re.sub(r"^```(?:json)?|```$", "", reply, flags=re.M).strip()
                    match = re.search(r"\{.*\}", reply, re.S)
                    if not match:
                        return self._json({"error": "a IA respondeu em formato inesperado; tente de novo"}, 502)
                    out = json.loads(match.group(0))
                    title = str(out.get("title") or "").replace("<", "").replace(">", "").strip()[:100]
                    description = str(out.get("description") or "").replace("<", "").replace(">", "").strip()[:5000]
                    if not title:
                        return self._json({"error": "a IA não devolveu um título; tente de novo"}, 502)
                    return self._json({"ok": True, "title": title, "description": description})
                except urllib.error.HTTPError as exc:
                    detail = (exc.read() or b"")[:300].decode("utf-8", errors="replace")
                    return self._json({"error": f"HTTP {exc.code}: {detail}"}, 502)
                except Exception as exc:
                    return self._json({"error": str(exc)}, 502)
            if path == "/api/create/cleanup":
                if body.get("confirm") is not True:
                    return self._json({"error": "confirmação obrigatória"}, 400)
                jid = str(body.get("job") or "").strip()
                if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", jid):
                    return self._json({"error": "job inválido"}, 400)
                jd = (JOBS / jid).resolve()
                try:
                    jd.relative_to(JOBS.resolve())
                except ValueError:
                    return self._json({"error": "forbidden"}, 403)
                res = perform_cleanup(jd)
                return self._json(res, res.pop("_status", 200))
            if path == "/api/create/cleanup-all":
                if body.get("confirm") is not True:
                    return self._json({"error": "confirmação obrigatória"}, 400)
                cleaned, freed, skipped = [], 0.0, []
                if JOBS.is_dir():
                    for jd in sorted(JOBS.iterdir()):
                        if not jd.is_dir() or jd.name.startswith("viral-") or not clip_info(jd):
                            continue
                        res = perform_cleanup(jd)
                        if res.get("ok"):
                            cleaned.append(jd.name)
                            freed += float(res.get("mb_freed") or 0)
                        else:
                            skipped.append({"job": jd.name, "reason": str(res.get("error") or "falhou")})
                return self._json({"ok": True, "cleaned": cleaned, "mb_freed": round(freed, 1),
                                   "skipped": skipped})
            if path == "/api/create/viral":
                plan = body.get("moments")
                if not isinstance(plan, list) or not plan:
                    return self._json({"error": "escolha ao menos um momento para o vídeo viral"}, 400)
                cleaned, seen = [], set()
                for item in plan[:24]:
                    if not isinstance(item, dict):
                        return self._json({"error": "escolha os momentos pelos cards do vídeo viral"}, 400)
                    jid = str(item.get("job") or "").strip()
                    try:
                        n = int(item.get("n"))
                    except (TypeError, ValueError):
                        return self._json({"error": f"momento inválido: {jid} #{item.get('n')}"}, 400)
                    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", jid) or n < 1:
                        return self._json({"error": f"momento inválido: {jid} #{item.get('n')}"}, 400)
                    jd = (JOBS / jid).resolve()
                    try:
                        jd.relative_to(JOBS.resolve())
                    except ValueError:
                        return self._json({"error": "forbidden"}, 403)
                    if not (jd / "candidates.json").exists():
                        return self._json({"error": f"projeto {jid} não tem momentos analisados"}, 400)
                    if (jid, n) not in seen:
                        seen.add((jid, n))
                        cleaned.append({"job": jid, "n": n})
                if not cleaned:
                    return self._json({"error": "escolha ao menos um momento válido"}, 400)
                options = {"multi": True, "moments_plan": cleaned}
                if body.get("target_min") is not None:
                    try:
                        options["target_min"] = max(2, min(30, int(body.get("target_min"))))
                    except (TypeError, ValueError):
                        return self._json({"error": "duração alvo inválida"}, 400)
                options["pip"] = bool(body.get("pip", True))
                options["title"] = str(body.get("title") or "").strip()[:120]
                with COMPILE_LOCK:
                    data = start_viral(options)
                return self._json({"ok": True, "viral": data}, 202)
            if path == "/api/jobs/delete":
                if body.get("confirm") is not True:
                    return self._json({"error": "confirmação obrigatória para apagar o projeto"}, 400)
                jid = str(body.get("job") or "").strip()
                if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", jid):
                    return self._json({"error": "job inválido"}, 400)
                jd = (JOBS / jid).resolve()
                try:
                    jd.relative_to(JOBS.resolve())
                except ValueError:
                    return self._json({"error": "forbidden"}, 403)
                if not jd.is_dir():
                    return self._json({"error": "projeto não encontrado"}, 404)
                if voids.recording_running(jd) or any(row.get("id") == jid and watch_running(row) for row in load_watchlist()):
                    return self._json({"error": "pare a gravação antes de apagar o projeto"}, 409)
                run = creation_run(jd)
                if run and run.get("status") in {"analyzing", "rendering"}:
                    return self._json({"error": "aguarde o processamento terminar antes de apagar o projeto"}, 409)
                if pipeline_for(jid):
                    return self._json({"error": "aguarde a pós-captura terminar antes de apagar o projeto"}, 409)
                if voids.source_referenced(jd):
                    return self._json({"error": "a fonte deste projeto alimenta momentos de outro projeto; libere-o primeiro"}, 409)
                try:
                    shutil.rmtree(jd)
                except OSError as exc:
                    locked = getattr(exc, "winerror", None) == 32 or "being used" in str(exc).lower()
                    if locked:
                        return self._json({"error": "algum arquivo ainda está em uso (player aberto?). Feche a reprodução e tente de novo."}, 409)
                    return self._json({"error": f"não foi possível apagar o projeto: {exc}"}, 500)
                return self._json({"ok": True})
            if path == "/api/create/analyze":
                url = str(body.get("url") or "").strip()
                if not re.match(r"^https://", url, re.I) or not allowed_video_url(url):
                    return self._json({"error": "use um link público do YouTube ou um VOD da Twitch/Kick"}, 400)
                top = max(1, min(12, int(body.get("top") or 6)))
                max_minutes = max(5, min(180, int(body.get("max_minutes") or 90)))
                options = {
                    "mode": body.get("mode") if body.get("mode") in {"auto", "manual"} else "manual",
                    "orientation": body.get("orientation") if body.get("orientation") in {"vertical", "horizontal"} else "vertical",
                    "duration": str(body.get("duration")) if str(body.get("duration")) in {"auto", "60", "180"} else "auto",
                    "layout": body.get("layout") if body.get("layout") in {"blur", "fill", "react", "facecam"} else "blur",
                    "captions": body.get("captions") if body.get("captions") in {"word", "bubble", "sentence", "none"} else "word",
                    "lang": str(body.get("lang") or "pt").lower() if str(body.get("lang") or "pt").lower() in {"pt", "en", "es", "de", "ru", "fr", "it"} else "pt",
                    "model": body.get("model") if body.get("model") in {"fast", "balanced", "accurate"} else "balanced",
                    "rerank": bool(body.get("rerank")) and bool(common.llm_config()),
                }
                try:
                    with CREATION_LOCK:
                        data = start_creation_analysis(url, url, top, max_minutes, options)
                except RuntimeError as exc:
                    return self._json({"error": str(exc)}, 409)
                return self._json({"ok": True, "job": data})
            if path == "/api/create/analyze-file":
                relative = str(body.get("path") or "").strip().replace("\\", "/")
                if not relative.startswith("jobs/") or ".." in Path(relative).parts:
                    return self._json({"error": "selecione um arquivo de vídeo dentro de jobs/"}, 400)
                src = (ROOT / relative).resolve()
                try:
                    src.relative_to(JOBS.resolve())
                except ValueError:
                    return self._json({"error": "forbidden"}, 403)
                if not src.is_file() or src.suffix.lower() not in {".mp4", ".mkv", ".mov", ".webm"} or "edits" in src.relative_to(JOBS.resolve()).parts:
                    return self._json({"error": "arquivo de origem inválido ou indisponível"}, 400)
                top = max(1, min(12, int(body.get("top") or 6)))
                max_minutes = max(5, min(180, int(body.get("max_minutes") or 90)))
                options = {
                    "mode": body.get("mode") if body.get("mode") in {"auto", "manual"} else "manual",
                    "orientation": body.get("orientation") if body.get("orientation") in {"vertical", "horizontal"} else "vertical",
                    "duration": str(body.get("duration")) if str(body.get("duration")) in {"auto", "60", "180"} else "auto",
                    "layout": body.get("layout") if body.get("layout") in {"blur", "fill", "react", "facecam"} else "blur",
                    "captions": body.get("captions") if body.get("captions") in {"word", "bubble", "sentence", "none"} else "word",
                    "lang": str(body.get("lang") or "pt").lower() if str(body.get("lang") or "pt").lower() in {"pt", "en", "es", "de", "ru", "fr", "it"} else "pt",
                    "model": body.get("model") if body.get("model") in {"fast", "balanced", "accurate"} else "balanced",
                    "rerank": bool(body.get("rerank")) and bool(common.llm_config()),
                }
                try:
                    with CREATION_LOCK:
                        data = start_creation_analysis(str(src), relative, top, max_minutes, options)
                except RuntimeError as exc:
                    return self._json({"error": str(exc)}, 409)
                return self._json({"ok": True, "job": data})
            if path == "/api/create/compile":
                jid = str(body.get("job") or "").strip()
                if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", jid):
                    return self._json({"error": "job inválido"}, 400)
                jd = (JOBS / jid).resolve()
                try:
                    jd.relative_to(JOBS.resolve())
                except ValueError:
                    return self._json({"error": "forbidden"}, 403)
                if not (jd / "candidates.json").exists():
                    return self._json({"error": "analise esta fonte antes de gerar os melhores momentos"}, 400)
                options = {}
                moments = body.get("moments")
                if moments:
                    if isinstance(moments, str):
                        moments = [m for m in re.findall(r"\d+", moments)]
                    elif isinstance(moments, list):
                        moments = [str(m) for m in moments if str(m).isdigit()]
                    if moments:
                        options["moments"] = ",".join(moments[:24])
                if body.get("target_min") is not None:
                    try:
                        options["target_min"] = max(2, min(30, int(body.get("target_min"))))
                    except (TypeError, ValueError):
                        return self._json({"error": "duração alvo inválida"}, 400)
                options["pip"] = bool(body.get("pip", True))
                options["title"] = str(body.get("title") or "").strip()[:120]
                try:
                    with COMPILE_LOCK:
                        data = start_compile(jid, options)
                except RuntimeError as exc:
                    return self._json({"error": str(exc)}, 409)
                return self._json({"ok": True, "compile": data}, 202)
            if path == "/api/create/cut":
                jid = str(body.get("job") or "").strip()
                if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", jid):
                    return self._json({"error": "job inválido"}, 400)
                jd = (JOBS / jid).resolve()
                try:
                    jd.relative_to(JOBS.resolve())
                except ValueError:
                    return self._json({"error": "forbidden"}, 403)
                cand_path = jd / "candidates.json"
                if not cand_path.exists():
                    return self._json({"error": "analise esta fonte antes de escolher um momento"}, 400)
                candidates = common.load_json(cand_path)
                pick = int(body.get("pick") or 0)
                candidate = next((c for c in candidates.get("candidates", []) if int(c.get("n") or 0) == pick), None)
                if not candidate:
                    return self._json({"error": "momento inválido"}, 400)
                layout = str(body.get("layout") or "blur")
                orientation = str(body.get("orientation") or "vertical")
                duration = str(body.get("duration") or "auto")
                captions = str(body.get("captions") or "word")
                title_style = str(body.get("title_style") or "box")
                zoom = str(body.get("zoom") or "off")
                lang = str(body.get("lang") or "").strip().lower()
                if layout not in {"fill", "blur", "facecam", "react"}:
                    return self._json({"error": "layout inválido"}, 400)
                if orientation not in {"vertical", "horizontal"} or duration not in {"auto", "60", "180"}:
                    return self._json({"error": "formato ou duração inválidos"}, 400)
                if captions not in {"word", "bubble", "none", "sentence"}:
                    return self._json({"error": "tipo de legenda inválido"}, 400)
                if title_style not in {"box", "outline"} or zoom not in {"off", "face", "game", "both"}:
                    return self._json({"error": "estilo de texto ou zoom inválido"}, 400)
                if captions == "sentence" and lang not in {"pt", "en", "es", "de", "ru", "fr", "it"}:
                    return self._json({"error": "escolha o idioma para as legendas em frases"}, 400)
                overlay = str(body.get("overlay") or "").strip()[:120]
                hook = str(body.get("hook") or "").strip()[:80]
                post_title = str(body.get("post_title") or "").strip()[:100]
                credit = str(body.get("credit") or "").strip()[:100]
                cmd = [PYTHON, str(ROOT / "scripts" / "cut.py"), str(jd), "--pick", str(pick),
                       "--layout", layout, "--orientation", orientation,
                       "--captions", captions, "--title-style", title_style, "--zoom", zoom]
                if duration != "auto":
                    source_duration = float(candidates.get("duration") or candidate.get("end") or 0)
                    target = float(duration)
                    center = float(candidate.get("peak") if candidate.get("peak") is not None else
                                   (float(candidate.get("start") or 0) + float(candidate.get("end") or 0)) / 2)
                    start = max(0.0, center - target / 2)
                    end = min(source_duration, start + target)
                    start = max(0.0, end - target)
                    cmd += ["--start", f"{start:.2f}", "--end", f"{end:.2f}"]
                if layout == "react" and overlay:
                    cmd += ["--title", overlay]
                if hook:
                    cmd += ["--hook", hook]
                if captions == "sentence":
                    cmd += ["--lang", lang]
                cut_result = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True,
                                            encoding="utf-8", errors="replace", timeout=1800, env=child_env())
                if cut_result.returncode != 0:
                    return self._json({"error": (cut_result.stderr or cut_result.stdout or "cut.py falhou")[-1800:]}, 500)
                ed = jd / "edits"
                info = candidates.get("info") or {}
                package = draft_posting(candidate, info, post_title or overlay or hook, credit)
                posting_path = ed / f"clip_{pick}_posting.json"
                common.save_json(posting_path, package)
                md = ["# Sugestões de publicação", "", package["review_note"], "",
                      "## Ideias de título", *[f"- {t}" for t in package["title_suggestions"]], ""]
                for platform, copy in package["platforms"].items():
                    md += [f"## {platform}", "", f"**Título:** {copy['title']}", "", copy["description"], "",
                           " ".join(copy.get("hashtags") or []), ""]
                (ed / f"clip_{pick}_posting.md").write_text("\n".join(md), encoding="utf-8")
                pack_result = subprocess.run([PYTHON, str(ROOT / "scripts" / "pack.py"), str(jd),
                                              "--pick", str(pick), "--title", post_title or overlay or hook,
                                              "--credit", credit], cwd=str(ROOT), capture_output=True,
                                             text=True, encoding="utf-8", errors="replace", timeout=300,
                                             env=child_env())
                return self._json({"ok": True, "package": package,
                                   "thumbnail": f"jobs/{jid}/edits/thumb_{pick}.jpg" if (ed / f"thumb_{pick}.jpg").exists() else None,
                                   "package_note": "" if pack_result.returncode == 0 else "O vídeo foi criado, mas a miniatura precisará ser gerada novamente."})
            if path == "/api/rerank":
                jid = str(body.get("job") or "").strip()
                if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", jid):
                    return self._json({"error": "job inválido"}, 400)
                jd = job_row_for(jid)
                if not (jd / "candidates.json").exists():
                    return self._json({"error": "analise esta fonte antes de reordenar os momentos"}, 400)
                top = max(1, min(12, int(body.get("top") or 6)))
                r = subprocess.run([PYTHON, str(ROOT / "scripts" / "rank_llm.py"), str(jd), "--top", str(top)],
                                   cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8",
                                   errors="replace", timeout=600, env=child_env())
                if r.returncode == 3:
                    return self._json({"error": "configure o LLM na aba Ajustes para usar o re-rank"}, 400)
                if r.returncode != 0:
                    return self._json({"error": (r.stderr or "rank_llm.py falhou")[-1500:]}, 500)
                return self._json({"ok": True, "summary": r.stdout[-400:]})
            if path == "/api/recut":
                job = str(body.get("job") or "").strip()
                n = str(body.get("n") or "").strip()
                if not re.fullmatch(r"\d+", n) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", job):
                    return self._json({"error": "job e n são obrigatórios"}, 400)
                jd = job_row_for(job)
                side = jd / "edits" / f"clip_{n}.json"
                if not side.exists():
                    return self._json({"error": f"clip_{n}.json não existe"}, 404)
                meta = common.load_json(side)
                o_s, o_e = float(meta.get("start") or 0), float(meta.get("end") or 0)
                try:
                    start = float(body.get("start")) if body.get("start") is not None else o_s
                    end = float(body.get("end")) if body.get("end") is not None else o_e
                except (TypeError, ValueError):
                    return self._json({"error": "início e fim precisam ser números (segundos)"}, 400)
                # trim freely inside the moment; extend at most 60 s beyond each original bound
                start = max(0.0, min(start, o_e + 60), o_s - 60)
                end = min(end, o_e + 60)
                if end - start < 1.0:
                    return self._json({"error": "o corte precisa de pelo menos 1 segundo (ajuste início e fim)"}, 400)
                meta = dict(meta)
                meta["start"], meta["end"] = round(start, 2), round(end, 2)
                title = str(body.get("title") or "").strip()[:100] or str(meta.get("title") or meta.get("hook") or "")
                hook = str(body.get("hook") or "").strip()[:80]
                STATE_DIR.mkdir(exist_ok=True)
                tmp = STATE_DIR / f"tmp_{jd.name}_{n}_{int(time.time())}.json"
                tmp.write_text(json.dumps(meta.get("sentences_cache") or [], ensure_ascii=False), encoding="utf-8")
                cmd = build_retitle_cmd(meta, jd, n, title, tmp, hook=hook)
                r = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8",
                                   errors="replace", timeout=600, env=child_env())
                tmp.unlink(missing_ok=True)
                if r.returncode != 0:
                    return self._json({"error": (r.stderr or "cut.py falhou")[-1500:]}, 500)
                return self._json({"ok": True, "start": meta["start"], "end": meta["end"]})
            if path == "/api/split-moment":
                jid = str(body.get("job") or "").strip()
                if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", jid):
                    return self._json({"error": "job inválido"}, 400)
                try:
                    return self._json(split_moment(job_row_for(jid), int(body.get("n") or 0),
                                                   at=body.get("at"),
                                                   start=body.get("start"), end=body.get("end")))
                except (ValueError, TypeError) as e:
                    return self._json({"error": str(e)}, 400)
            if path == "/api/perf":
                if body.get("action") == "delete":
                    if not performance.delete(str(body.get("id") or "")):
                        return self._json({"error": "registro não encontrado"}, 404)
                    return self._json({"ok": True})
                if body.get("id"):
                    row = performance.update_metrics(str(body.get("id")), body.get("metrics") or {})
                    if not row:
                        return self._json({"error": "registro não encontrado"}, 404)
                    return self._json({"ok": True, "row": row})
                jid = str(body.get("job") or "").strip()
                n = int(body.get("n") or 0)
                platform = str(body.get("platform") or "").strip().lower()
                if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", jid) or n < 1:
                    return self._json({"error": "job e número do clipe são obrigatórios"}, 400)
                if platform not in performance.PLATFORMS:
                    return self._json({"error": f"plataforma inválida (use: {', '.join(performance.PLATFORMS)})"}, 400)
                hook_type = str(body.get("hook_type") or "").strip().lower()
                if hook_type not in performance.HOOK_TYPES:
                    hook_type = ""
                try:
                    row = performance.add(jid, n, platform,
                                          title=str(body.get("title") or ""), url=str(body.get("url") or ""),
                                          hook_type=hook_type,
                                          total_score=body.get("total_score"),
                                          dur=body.get("dur"), metrics=body.get("metrics") or {})
                except (TypeError, ValueError) as exc:
                    return self._json({"error": f"dados inválidos: {exc}"}, 400)
                return self._json({"ok": True, "row": row})
            if path == "/api/settings":
                s = common.load_settings()
                s = apply_settings_update(s, body)
                if "capture" in body:  # absent key = untouched; empty dict = back to defaults
                    cap, err = parse_capture(body)
                    if err:
                        return self._json({"error": err}, 400)
                    s["capture"] = cap
                common.SETTINGS.parent.mkdir(parents=True, exist_ok=True)
                common.save_json(common.SETTINGS, s)
                return self._json({"ok": True, "settings": settings_state()})
            if path == "/api/settings/test":
                cfg = common.llm_config()
                if not cfg:
                    return self._json({"error": "preencha base, chave e modelo antes de testar"}, 400)
                payload = json.dumps({"model": cfg["model"], "max_tokens": 8,
                                      "messages": [{"role": "user", "content": "Responda apenas: ok"}]}).encode("utf-8")
                req = urllib.request.Request(cfg["base"] + "/chat/completions", data=payload, method="POST",
                                             headers={"Content-Type": "application/json",
                                                      "Authorization": f"Bearer {cfg['key']}"})
                try:
                    with urllib.request.urlopen(req, timeout=30) as resp:
                        data = json.loads(resp.read().decode("utf-8"))
                    reply = str(((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
                    if not reply:
                        return self._json({"error": "o modelo respondeu sem texto"}, 502)
                    return self._json({"ok": True, "model": cfg["model"], "reply": reply[:80]})
                except urllib.error.HTTPError as exc:
                    detail = (exc.read() or b"")[:300].decode("utf-8", errors="replace")
                    return self._json({"error": f"HTTP {exc.code}: {detail}"}, 502)
                except Exception as exc:
                    return self._json({"error": str(exc)}, 502)
            if path == "/api/watch":
                url = str(body.get("url") or "").strip()
                parsed = urlparse(url)
                if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                    return self._json({"error": "informe uma URL http(s) válida"}, 400)
                minutes = float(body.get("minutes", 60))
                if not math.isfinite(minutes) or not 1 <= minutes <= 720:
                    return self._json({"error": "informe uma duração de 1 a 720 minutos"}, 400)
                try:
                    row = start_watch(url, minutes)
                except RuntimeError as exc:
                    return self._json({"error": str(exc)}, 409)
                return self._json({"ok": True, "watch": row})
            if path == "/api/watch/stop":
                try:
                    row = stop_watch(body.get("id"))
                except KeyError as exc:
                    return self._json({"error": exc.args[0]}, 404)
                return self._json({"ok": True, "stopped": row})
            if path == "/api/spike-feedback":
                jid = str(body.get("job_id") or "").strip()
                if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", jid):
                    return self._json({"error": "job inválido"}, 400)
                if not (JOBS / jid).is_dir():
                    return self._json({"error": "job não encontrado"}, 400)
                verdict = str(body.get("verdict") or "")
                if verdict not in ("fp", "ok"):
                    return self._json({"error": "verdict inválido (use ok ou fp)"}, 400)
                spike_feedback.record(jid, str(body.get("utc") or ""), str(body.get("offset") or ""), verdict)
                return self._json({"ok": True})
            if path == "/api/watch/remove":
                with WATCH_LOCK:
                    rows = load_watchlist()
                    if any(r["id"] == body.get("id") and watch_running(r) for r in rows):
                        return self._json({"error": "pare o monitor antes de remover"}, 400)
                    save_watchlist([r for r in rows if r["id"] != body.get("id")])
                return self._json({"ok": True})
            if path == "/api/streams/refresh":
                q = (body.get("query") or "gta 6").strip()
                r = subprocess.run([PYTHON, str(ROOT / "scripts" / "streams.py"),
                                    "--query", q, "--out", "streams.md"],
                                   cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8",
                                   errors="replace", timeout=300, env=child_env())
                if r.returncode != 0:
                    return self._json({"error": (r.stderr or "streams.py falhou")[-400:]}, 500)
                return self._json({"ok": True, "streams": parse_streams_md()})
            if path == "/api/retitle":
                job = str(body.get("job") or "").strip()
                n = str(body.get("n") or "").strip()
                title = str(body.get("title") or "").strip()
                if not re.fullmatch(r"\d+", n) or not title:
                    return self._json({"error": "job, n e title são obrigatórios"}, 400)
                jd = (JOBS / job).resolve()
                try:
                    jd.relative_to(JOBS.resolve())
                except ValueError:
                    return self._json({"error": "forbidden"}, 403)
                side = jd / "edits" / f"clip_{n}.json"
                if not side.exists():
                    return self._json({"error": f"clip_{n}.json não existe"}, 404)
                meta = common.load_json(side)
                STATE_DIR.mkdir(exist_ok=True)
                tmp = STATE_DIR / f"tmp_{jd.name}_{n}_{int(time.time())}.json"
                tmp.write_text(json.dumps(meta.get("sentences_cache") or [], ensure_ascii=False), encoding="utf-8")
                cmd = build_retitle_cmd(meta, jd, n, title, tmp, hook=str(body.get("hook") or "").strip())
                r = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8",
                                   errors="replace", timeout=300, env=child_env())
                tmp.unlink(missing_ok=True)
                if r.returncode != 0:
                    return self._json({"error": (r.stderr or "cut.py falhou")[-1500:]}, 500)
                return self._json({"ok": True})
            if path == "/api/lang":
                job = str(body.get("job") or "").strip()
                n = str(body.get("n") or "").strip()
                langs = body.get("langs") or ""
                if isinstance(langs, list):
                    langs = ",".join(str(x) for x in langs)
                langs = str(langs).strip()
                if not re.fullmatch(r"\d+", n) or not langs:
                    return self._json({"error": "job, n e langs são obrigatórios"}, 400)
                jd = (JOBS / job).resolve()
                try:
                    jd.relative_to(JOBS.resolve())
                except ValueError:
                    return self._json({"error": "forbidden"}, 403)
                cmd = [PYTHON, str(ROOT / "scripts" / "translate.py"), str(jd),
                       "--pick", n, "--langs", langs]
                if body.get("voice"):
                    cmd.append("--voice")
                r = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8",
                                   errors="replace", timeout=900, env=child_env())
                if r.returncode != 0:
                    return self._json({"error": (r.stderr or "translate.py falhou")[-1500:]}, 500)
                return self._json({"ok": True})
            if path == "/api/open":
                p = Path(body.get("path") or "").resolve()
                try:
                    p.relative_to(JOBS.resolve())
                except ValueError:
                    return self._json({"error": "forbidden"}, 403)
                if p.exists():
                    if sys.platform == "win32":
                        os.startfile(p)  # noqa: S606
                    elif sys.platform == "darwin":
                        subprocess.run(["open", str(p)], check=False)
                    else:
                        subprocess.run(["xdg-open", str(p)], check=False)
                    return self._json({"ok": True})
                return self._json({"error": "não existe"}, 404)
        except (TypeError, ValueError) as e:
            return self._json({"error": str(e)}, 400)
        except Exception as e:
            return self._json({"error": str(e)}, 500)
        return self._json({"error": "not found"}, 404)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--no-open", action="store_true")
    a = ap.parse_args()
    if not acquire_server_lock():
        ap.exit(1, "Já existe um dashboard em execução para este projeto.\n")
    try:
        with QuietHTTPServer(("127.0.0.1", a.port), Handler) as srv:
            LOGS.mkdir(exist_ok=True)
            threading.Thread(target=pipeline_worker, daemon=True).start()
            threading.Thread(target=capture_watcher, daemon=True).start()
            threading.Thread(target=void_pruner, daemon=True).start()
            url = f"http://127.0.0.1:{srv.server_port}"
            print(f"dashboard em {url}  (Ctrl+C para sair)", flush=True)
            if not a.no_open:
                threading.Timer(0.8, webbrowser.open, (url,)).start()
            srv.serve_forever()
    except KeyboardInterrupt:
        print("\nencerrado")
    finally:
        SERVER_LOCK.unlink(missing_ok=True)


if __name__ == "__main__":
    main()

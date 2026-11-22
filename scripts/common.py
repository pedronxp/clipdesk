"""Shared helpers: paths, tool lookup, yt-dlp and ffmpeg wrappers."""
import hashlib, json, os, re, shutil, subprocess, sys
from pathlib import Path

ROOT = Path(os.environ.get("CLIP_DESK_HOME", Path.cwd()))
JOBS = ROOT / "jobs"
SETTINGS = ROOT / "dashboard" / "state" / "settings.json"


def transcribe_progress(last_end, total, state):
    """One line per completed audio-minute of transcription, only when the minute is new.
    state is a per-run dict; returns None when there is nothing new to say (spec D8)."""
    total_min = int(float(total or 0) // 60)
    if total_min <= 0:
        return None
    minute = int(float(last_end or 0) // 60)
    if minute <= state.get("last", 0):
        return None
    state["last"] = minute
    return f"transcrição {minute}/{total_min} min"


def load_settings():
    """User settings saved by the dashboard's Ajustes tab. Missing or broken file degrades
    to empty defaults; scripts never fail because settings are absent."""
    defaults = {"llm": {"base": "", "key": "", "model": ""}, "whisper_model": "", "ytdlp_cookies": ""}
    try:
        data = load_json(SETTINGS)
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    out = dict(defaults)
    out.update({k: v for k, v in data.items() if v is not None})
    if not isinstance(out.get("llm"), dict):
        out["llm"] = {}
    return out


def llm_config():
    """{base, key, model} for an OpenAI-compatible chat endpoint, or None when unset.
    Env GTAVI_LLM_BASE/KEY/MODEL wins over the dashboard settings so scripts can run
    headless. `base` must include the version path (e.g. https://api.openai.com/v1)."""
    s = load_settings().get("llm") or {}
    cfg = {"base": (os.environ.get("GTAVI_LLM_BASE") or s.get("base") or "").strip().rstrip("/"),
           "key": (os.environ.get("GTAVI_LLM_KEY") or s.get("key") or "").strip(),
           "model": (os.environ.get("GTAVI_LLM_MODEL") or s.get("model") or "").strip()}
    if not (cfg["base"] and cfg["key"] and cfg["model"]):
        return None
    return cfg


def tool(name):
    p = shutil.which(name)
    if not p and name == "yt-dlp":
        # The dashboard can run under a different Python than the project venv.
        # In that case, use the venv's CLI before reporting yt-dlp as missing.
        scripts = "Scripts" if sys.platform == "win32" else "bin"
        code_root = Path(__file__).resolve().parent.parent
        for env_name in (".venv", ".venv313"):
            candidate = code_root / env_name / scripts / ("yt-dlp.exe" if sys.platform == "win32" else "yt-dlp")
            if candidate.is_file():
                p = str(candidate)
                break
    if not p:
        sys.exit(f"{name} not found. Run the setup skill first (it installs ffmpeg and yt-dlp).")
    return p


def run(cmd, check=True, capture=False):
    r = subprocess.run(cmd, check=False, text=True, encoding="utf-8", errors="replace",
                       stdout=subprocess.PIPE if capture else None, stderr=subprocess.PIPE if capture else None)
    if check and r.returncode != 0:
        msg = (r.stderr or "")[-1500:] if capture else ""
        sys.exit(f"command failed ({r.returncode}): {' '.join(map(str, cmd))[:200]}\n{msg}")
    return r


_BROWSERS = {"chrome", "edge", "firefox", "brave", "chromium", "vivaldi", "safari", "whale", "librewolf"}


def ytdlp_cookies_args():
    """YouTube blocks anonymous extraction with a bot check; cookies are configured once via
    GTAVI_YTDLP_COOKIES (browser name or cookies.txt path), dashboard/state/config.json
    {"ytdlp_cookies": ...} or the Ajustes tab (settings.json). Empty list = no cookies
    configured, and every YouTube extraction then fails with "Sign in to confirm you're
    not a bot" — see scripts/export_cookies.py for the one-time export."""
    v = os.environ.get("GTAVI_YTDLP_COOKIES", "").strip()
    if not v:
        try:
            cfg = json.loads((ROOT / "dashboard" / "state" / "config.json").read_text(encoding="utf-8"))
            v = str(cfg.get("ytdlp_cookies") or "").strip()
        except Exception:
            v = ""
    if not v:
        v = str(load_settings().get("ytdlp_cookies") or "").strip()
    if not v:
        return []
    if v.lower() in _BROWSERS:
        return ["--cookies-from-browser", v.lower()]
    p = Path(v)
    if not p.is_absolute():
        p = ROOT / p
    return ["--cookies", str(p)]


def js_runtime_args():
    """YouTube serves JS challenges (EJS) that yt-dlp must solve before listing playable
    formats; without a runtime every video comes back with storyboards only ("Some formats
    may be missing"). yt-dlp auto-detects Deno alone, so point it at an available runtime
    explicitly when the yt-dlp-ejs solver package is installed. See yt-dlp wiki/EJS."""
    import importlib.util
    try:
        if importlib.util.find_spec("yt_dlp_ejs") is None:
            return []
    except Exception:
        return []
    for name in ("deno", "node", "bun"):
        if shutil.which(name):
            return ["--js-runtimes", name]
    return []


def ytdlp_base(cookies=True):
    import importlib.util
    exe = [sys.executable, "-m", "yt_dlp"] if importlib.util.find_spec("yt_dlp") else [tool("yt-dlp")]
    base = exe + ["--no-warnings", "--no-progress"]
    if sys.platform == "win32":
        base += ["--compat-options", "no-certifi"]  # use the Windows certificate store
    return base + js_runtime_args() + (ytdlp_cookies_args() if cookies else [])


def ytdlp_json(url, extra=()):
    r = run(ytdlp_base() + ["-J", *extra, url], capture=True)
    return json.loads(r.stdout)


def is_url(s):
    return bool(re.match(r"https?://", s))


def pid_alive(pid):
    """True when the PID belongs to a running process (Windows and POSIX)."""
    if not pid:
        return False
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.windll.kernel32
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        h = k32.OpenProcess(0x1F0FFF, False, pid)  # PROCESS_ALL_ACCESS; the limited variant can be denied
        if not h:
            return False
        ec = wintypes.DWORD()
        ok = k32.GetExitCodeProcess(h, ctypes.byref(ec))
        k32.CloseHandle(h)
        return bool(ok) and ec.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def job_dir(src):
    key = hashlib.sha1(src.encode()).hexdigest()[:8]
    stem = re.sub(r"[^a-z0-9]+", "-", Path(src).stem.lower() if not is_url(src) else src.split("v=")[-1].split("/")[-1].lower())[:24].strip("-")
    d = JOBS / f"{stem or 'job'}-{key}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def ffprobe(path, at=None):
    r = run([tool("ffprobe"), "-v", "error", "-show_entries", "format=duration:stream=codec_type,width,height,r_frame_rate",
             "-of", "json", str(path)], capture=True)
    out = json.loads(r.stdout)
    # Container/stream-header dimensions can disagree with what the decoder outputs, and live
    # recordings (concatenated HLS segments) can change resolution mid-stream. Pixel-based crops
    # run on ffmpeg's decode, so probe a real frame — at the clip's position when given — and trust that.
    try:
        iv = f"{max(0.0, at):.2f}%+0.5" if at is not None else "%+1"
        f = run([tool("ffprobe"), "-v", "error", "-select_streams", "v:0", "-show_entries", "frame=width,height",
                 "-read_intervals", iv, "-of", "json", str(path)], capture=True)
        fr = (json.loads(f.stdout).get("frames") or [{}])[0]
        vs = [s for s in out.get("streams", []) if s.get("codec_type") == "video"]
        if fr.get("width") and vs and (int(fr["width"]), int(fr["height"])) != (vs[0].get("width"), vs[0].get("height")):
            vs[0]["width"], vs[0]["height"] = int(fr["width"]), int(fr["height"])
    except Exception:
        pass
    return out


def save_json(path, obj):
    Path(path).write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load_presets(path=None):
    """Named editing presets from scripts/presets.json.

    Returns {"default": name, "presets": {name: {argparse defaults}}}. Missing or broken
    files degrade to an empty preset table so old jobs keep working.
    """
    p = Path(path) if path else Path(__file__).resolve().parent / "presets.json"
    try:
        data = load_json(p)
    except Exception:
        data = {}
    presets = data.get("presets")
    if not isinstance(presets, dict):
        presets = {k: v for k, v in data.items() if k not in ("default", "presets") and isinstance(v, dict)}
    return {"default": data.get("default") or "react-br", "presets": presets}


def preset_values(name=None, path=None):
    """(name, values) for a preset; explicit --preset wins over the file default."""
    table = load_presets(path)
    key = name or table["default"]
    if key == "default":
        key = table["default"]
    if key in ("", "none", "off"):
        return None, {}
    if key not in table["presets"]:
        sys.exit(f"unknown preset '{key}' (available: {', '.join(table['presets']) or 'none'})")
    return key, dict(table["presets"][key])


def rebuild_cfr(src, dst=None, fps=30):
    """Decode any recording (single file or a list of segments) into a constant-frame-rate
    H.264/AAC source. Live HLS segments reset timestamps and can change resolution; a CFR
    rebuild makes every later crop, face track and zoom use one stable timeline."""
    parts = [Path(p) for p in src] if isinstance(src, (list, tuple)) else [Path(src)]
    if not parts:
        sys.exit("rebuild_cfr: no input")
    if dst is None:
        dst = parts[0].with_name(parts[0].stem + "_cfr.mp4")
    dst = Path(dst)
    if dst.exists() and dst.stat().st_size > 0:
        return dst
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = [tool("ffmpeg"), "-y", "-v", "error"]
    concat = None
    if len(parts) > 1:
        concat = dst.with_suffix(".concat.txt")
        concat.write_text("".join(f"file '{p.resolve().as_posix()}'\n" for p in parts), encoding="utf-8")
        cmd += ["-f", "concat", "-safe", "0", "-i", str(concat)]
    else:
        cmd += ["-i", str(parts[0])]
    cmd += ["-fflags", "+genpts", "-vf", f"fps={fps},setpts=PTS-STARTPTS", "-r", str(fps)]
    try:
        has_audio = any(s.get("codec_type") == "audio" for s in ffprobe(parts[0]).get("streams", []))
    except Exception:
        has_audio = False
    if has_audio:
        cmd += ["-af", "aresample=async=1:first_pts=0"]
    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p"]
    if has_audio:
        cmd += ["-c:a", "aac", "-b:a", "160k", "-ar", "48000"]
    cmd += ["-movflags", "+faststart", str(dst)]
    try:
        run(cmd)
    finally:
        if concat:
            concat.unlink(missing_ok=True)
    return dst

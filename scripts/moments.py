"""Find the best clip moments in a video or VOD.

Signals per second: loudness spikes, YouTube "most replayed" heatmap, live-chat spikes
(YouTube VOD chat replay or a TwitchDownloader chat.json) and hype words in the transcript.
Writes jobs/<id>/candidates.md + candidates.json.

usage: python moments.py <url-or-file> [--top 8] [--min 60] [--max 90] [--chat chat.json] [--max-minutes 90]
"""
import argparse, json, math, os, re, sys, wave
from pathlib import Path
import numpy as np
sys.path.insert(0, str(Path(__file__).parent))
from common import ffprobe, is_url, job_dir, load_json, run, save_json, tool, ytdlp_base, ytdlp_json, JOBS, transcribe_progress

BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/131.0.0.0 Safari/537.36")
VIDEO_FORMAT = "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/b[height<=1080]/b"


def inherit_source_info(source, jobs_root=None):
    """Clips cut from a live job's spike inherit the live's URL and channel: moments.py makes a
    separate job per spike file, so without this the credit would be lost."""
    try:
        jobs_root = Path(jobs_root) if jobs_root else JOBS
        root = Path(jobs_root).resolve().as_posix().rstrip("/") + "/"
        p = Path(source).resolve().as_posix()
        if not p.startswith(root):
            return None
        rel = p[len(root):]
        parts = rel.split("/")
        if len(parts) < 2 or not parts[0]:
            return None
        jd = Path(jobs_root) / parts[0]
        live = load_json(jd / "live.json") if (jd / "live.json").exists() else {}
        info = load_json(jd / "info.json") if (jd / "info.json").exists() else {}
        url = (live.get("url") or info.get("url") or "").strip()
        channel = ""
        for name in ("channel", "uploader", "creator", "uploader_id"):
            value = str(info.get(name) or "").strip()
            if value and not re.fullmatch(r"(?:UC[A-Za-z0-9_-]{18,}|[0-9a-f]{8,}|job-[0-9a-f]+)", value, re.I):
                channel = value.lstrip("@")
                break
        if not channel and url:
            from urllib.parse import urlparse, parse_qs
            parsed = urlparse(url)
            host = (parsed.hostname or "").lower()
            first = parsed.path.strip("/").split("/")[0]
            if "twitch.tv" in host or "kick.com" in host:
                channel = first.lstrip("@") if first.lower() not in {"videos", "video", "watch", "live"} else ""
            elif first.startswith("@"):
                channel = first.lstrip("@")
            elif "youtube" in host or "youtu.be" in host:
                channel = ""
            else:
                channel = first
        out = {}
        if url:
            out["webpage_url"] = url
        if channel:
            out["channel"] = channel
        if info.get("title"):
            out["title"] = info["title"]
        return out or None
    except Exception:
        return None

# Reaction words that mark a spike — EN + PT-BR (BR streamers scored zero on the EN-only list).
# Whisper emits one word at a time, so the multi-word phrases are best-effort; the single-word
# terms carry the signal. "porr\\w*" / "puta\\w*" match inflections without colliding with EN words.
HYPE = re.compile(r"\b(no way|oh my god|omg|what|wait|bro|let'?s go|holy|insane|crazy|yo+|wtf|dude|look at|nah|run|go go|"
                  r"meu deus|n[ãa]o acredito|que porra|olha isso|que foda|ai meu deus|"
                  r"caralh\w*|porr\w*|caramba|putz|credo|eita|vixi|vish|caraca|rapaz|socorro|nossa|puta\w*|mano|"
                  r"mds|pqp|k{2,}|(?:ha){2,}|(?:rs)+)\b", re.I)
CHAT_HYPE = re.compile(r"(LMAO|LOL|KEKW|OMEGALUL|W+\b|CLIP|💀|😂|🔥|NO WAY|HOLY)", re.I)


def trim_in_place(vid, seconds):
    """Cut a downloaded file locally (copy codec) when the remote section download failed."""
    tmp = vid.with_suffix(".trim.mp4")
    r = run([tool("ffmpeg"), "-y", "-v", "error", "-i", str(vid), "-t", str(seconds), "-c", "copy", str(tmp)], check=False)
    if r.returncode == 0 and tmp.exists() and tmp.stat().st_size > 0:
        tmp.replace(vid)
        return True
    tmp.unlink(missing_ok=True)
    return False


def download_source(src, vid, info, max_min):
    """Download the source video, honouring the max-minutes budget.

    Cutting a section straight from YouTube (--download-sections) asks ffmpeg to open the
    signed googlevideo URL; without browser headers YouTube answers 403, so the headers are
    mandatory. If it still fails, fall back to the full file and trim it locally.
    """
    base = ytdlp_base() + ["-f", VIDEO_FORMAT, "--merge-output-format", "mp4", "-o", str(vid)]
    duration = float(info.get("duration") or 0)
    if duration <= max_min * 60:
        run(base + [src])
        return
    section = base + ["--download-sections", f"*0-{max_min * 60}", "--force-keyframes-at-cuts",
                      "--add-header", "Referer:https://www.youtube.com/",
                      "--add-header", f"User-Agent:{BROWSER_UA}"]
    r = run(section + [src], check=False)
    if r.returncode == 0 and vid.exists() and vid.stat().st_size > 0:
        return
    print("Download em partes falhou; baixando o vídeo inteiro e cortando localmente.")
    vid.unlink(missing_ok=True)
    run(base + [src])
    trim_in_place(vid, max_min * 60)


def fetch(src, d, max_min):
    info = {}
    if is_url(src):
        info = ytdlp_json(src)
        save_json(d / "info.json", {k: info.get(k) for k in ("id", "title", "uploader", "channel", "uploader_id", "webpage_url",
                                                             "duration", "view_count", "upload_date", "heatmap", "is_live", "was_live")})
        vid = d / "source.mp4"
        if not vid.exists():
            download_source(src, vid, info, max_min)
        if info.get("was_live") and not (d / "chat.json").exists():  # YouTube chat replay
            run(ytdlp_base() + ["--skip-download", "--write-subs", "--sub-langs", "live_chat", "-o", str(d / "chat"), src], check=False)
    else:
        vid = Path(src).resolve()
        info = {"title": vid.stem, "webpage_url": str(vid)}
        extra = inherit_source_info(str(vid)) or {}
        info = {**info, **{k: v for k, v in extra.items() if v}}  # live job credit replaces the placeholder
        save_json(d / "info.json", info)
    return vid, info


SR = 16000


def audio_rms(vid, d):
    """Per-second dB from the 16 kHz mono wav, read in 10-minute chunks: a 4-hour live is ~900 MB
    as float32 and loading it whole was pushing 8 GB machines into swap. Same output as the old
    whole-array version (the trailing partial second is dropped, as before)."""
    wav = d / "audio.wav"
    if not wav.exists():
        run([tool("ffmpeg"), "-y", "-v", "error", "-i", str(vid), "-ac", "1", "-ar", "16000", str(wav)])
    out = []
    with wave.open(str(wav)) as w:
        while True:
            x = np.frombuffer(w.readframes(SR * 600), dtype=np.int16).astype(np.float32) / 32768
            n = len(x) // SR
            if n == 0:
                break
            rms = np.sqrt((x[: n * SR].reshape(n, SR) ** 2).mean(1) + 1e-9)
            out.append(20 * np.log10(rms))
    return wav, (np.concatenate(out) if out else np.zeros(0))


PIECE = 600    # transcribe 10 minutes per pass, saving each piece: an interrupted live resumes
OVERLAP = 2.0  # audio context on each side of a piece so boundary words are not clipped in half


def transcribe(wav, d):
    """Word-level transcript, 10-minute pieces at a time. Each piece lands in transcribe/NNNN.json
    as it finishes, so an interrupted 4-hour live resumes where it stopped and peak RAM is one
    piece (~40 MB) instead of the whole PCM. Pieces carry OVERLAP seconds of context; words outside
    the piece's own span are dropped, so boundaries neither clip nor duplicate words."""
    out = d / "transcript.json"
    if out.exists():
        return load_json(out)
    from faster_whisper import WhisperModel
    try:
        import ctranslate2
        cuda = ctranslate2.get_cuda_device_count() > 0
    except Exception:
        cuda = False
    with wave.open(str(wav)) as w:
        sr = w.getframerate()
        total = w.getnframes() / sr
    pieces = max(1, math.ceil(total / PIECE))
    pdir = d / "transcribe"
    pdir.mkdir(exist_ok=True)
    size = (os.environ.get("GTAVI_WHISPER_MODEL") or "small").strip()
    if size not in {"tiny", "base", "small", "medium", "large-v3"}:
        size = "small"

    def transcribe_piece(model, k, language, pstate):
        """One piece's record: words placed on the global timeline, clipped to the piece's own span."""
        a = max(0.0, k * PIECE - OVERLAP)
        b = min(total, (k + 1) * PIECE + OVERLAP)
        with wave.open(str(wav)) as w:
            w.setpos(int(a * sr))
            pcm = np.frombuffer(w.readframes(int((b - a) * sr)), dtype=np.int16).astype(np.float32) / 32768
        segs, info = model.transcribe(pcm, language=language, word_timestamps=True, vad_filter=True,
                                      beam_size=1, condition_on_previous_text=False)
        lo, hi = k * PIECE, (k + 1) * PIECE
        words = []
        for s in segs:
            for wd in (s.words or []):
                t = wd.start + a
                if wd.word.strip() and lo - 1e-6 <= t < hi:
                    words.append({"w": wd.word.strip(), "s": round(t, 2), "e": round(wd.end + a, 2)})
            line = transcribe_progress(min(total, a + float(getattr(s, "end", 0) or 0)), total, pstate)
            if line:
                print(line, flush=True)
        return {"language": getattr(info, "language", None),
                "prob": float(getattr(info, "language_probability", 0) or 0), "words": words}

    def run_on(device):
        model = WhisperModel(size, device=device, compute_type="float16" if device == "cuda" else "int8")
        lang, pstate, langs = None, {"last": 0}, {}
        for k in range(pieces):
            arq = pdir / f"{k:04d}.json"
            if arq.exists():
                rec = load_json(arq)
                line = transcribe_progress(min(total, (k + 1) * PIECE), total, pstate)
                if line:
                    print(line, flush=True)
            else:
                rec = transcribe_piece(model, k, lang, pstate)
                save_json(arq, rec)
            if lang is None and rec.get("prob", 0) > 0.8 and rec.get("words"):
                lang = rec["language"]  # fix the language once confident: faster and steadier
            langs[rec.get("language")] = langs.get(rec.get("language"), 0) + len(rec.get("words") or [])
        words = [wd for k in range(pieces) for wd in (load_json(pdir / f"{k:04d}.json").get("words") or [])]
        return {"language": lang or (max(langs, key=langs.get) if langs else None), "words": words}

    try:
        result = run_on("cuda" if cuda else "cpu")
    except RuntimeError as e:  # GPU present but CUDA libraries missing: fall back to CPU
        print(f"GPU transcription unavailable ({str(e)[:80]}), using CPU")
        result = run_on("cpu")
    save_json(out, result)
    return result


def chat_series(d, n, chat_arg, info):
    """Messages per second from YouTube live_chat json lines or a TwitchDownloader chat.json."""
    c = np.zeros(n)
    yt = list(d.glob("chat*.live_chat.json"))
    if yt:
        for line in yt[0].read_text(encoding="utf-8", errors="ignore").splitlines():
            try:
                a = json.loads(line)["replayChatItemAction"]
                t = int(a["videoOffsetTimeMsec"]) / 1000
                txt = json.dumps(a["actions"], ensure_ascii=False)
            except Exception:
                continue
            if 0 <= t < n:
                c[int(t)] += 2 if CHAT_HYPE.search(txt) else 1
    elif chat_arg:
        js = load_json(chat_arg)
        for m in js.get("comments", []):
            t = m.get("content_offset_seconds", 0)
            body = (m.get("message") or {}).get("body", "")
            if 0 <= t < n:
                c[int(t)] += 2 if CHAT_HYPE.search(body) else 1
    return c


def z(a):
    a = np.asarray(a, dtype=float)
    if a.std() < 1e-9:
        return np.zeros_like(a)
    return (a - np.median(a)) / (a.std() + 1e-9)


def smooth(a, k):
    k = max(1, int(k))
    return np.convolve(a, np.ones(k) / k, mode="same")


def loudness_spikes(db):
    """dB excess over the local speech level. The old baseline (30 s median including silence)
    collapsed through long quiet stretches, so the first loud sentence after one scored like a
    scream; here the baseline is the ±5 min median of speech only (bottom-fifth dB dropped)."""
    if not len(db):
        return np.zeros(0)
    fala = db > np.percentile(db, 20)
    base = np.empty_like(db)
    for i in range(0, len(db), 60):
        j0, j1 = max(0, i - 150), min(len(db), i + 150)
        win = db[j0:j1][fala[j0:j1]]
        base[i:i + 60] = np.median(win) if len(win) else np.median(db)
    return db - base


def dampen_edges(score, n):
    """The first/last minutes of a live always spike (greetings, goodbyes, the chat exploding at
    'boa noite') but are not moments — damp them. Short videos keep everything."""
    if n >= 600:
        edge = min(240, n // 10)
        score[:edge] *= 0.3
        score[n - edge:] *= 0.3
    return score


def window_for(words, dur, peak, target):
    """A ~target-second take around a peak: the peak sits ~40% in (hook early), the edges
    snap to speech boundaries — the start lands on a sentence edge when one sits nearby —
    and the window is clamped to the video. Speech-sparse windows keep at least 70% of target."""
    s0 = max(0.0, peak - 0.4 * target)
    e0 = min(dur, s0 + target)
    if e0 - s0 < target * 0.6:  # clamped at the end of the video: pull the start back
        s0 = max(0.0, e0 - target)
    inside = [x for x in words if s0 <= x["s"] < e0 and x["w"]]
    if inside:
        s0 = max(0.0, inside[0]["s"] - 0.3)
        e0 = min(dur, inside[-1]["e"] + 0.6)
        cand = [x["s"] for k, x in enumerate(inside)
                if k == 0 or x["s"] - inside[k - 1]["e"] > 0.8]
        if cand:
            best = min(cand, key=lambda s: abs(s - s0))
            if abs(best - s0) < target * 0.2:  # a clean sentence opening within reach
                s0 = max(0.0, best - 0.3)
        if e0 - s0 < target * 0.7:
            e0 = min(dur, s0 + target * 0.7)
    return round(s0, 2), round(e0, 2)


def why_for(p, loud, hype, heat, chatz, w_chat):
    why = []
    if loud[p] > 1: why.append("loudness spike")
    if hype[p] > 0: why.append("hype words")
    if heat[p] > 0.5: why.append(f"most replayed {heat[p]:.2f}")
    if w_chat and chatz[p] > 1: why.append("chat spike")
    return why or ["overall energy"]


def pick_takes(score, words, dur, n, tiers, top, loud, hype, heat, chatz, w_chat):
    """One take per duration tier for every chosen moment. Peaks closer than 1.1x the longest
    tier are the same moment (and would produce overlapping takes), so only the best is kept."""
    order = np.argsort(-score)
    chosen = []
    min_gap = 1.1 * max(tiers)
    for p in order:
        if len(chosen) >= top or score[p] <= 0:
            break
        if any(abs(p - c) < min_gap for c in chosen):
            continue  # same moment as a peak already chosen
        chosen.append(int(p))
    picks = []
    for p in chosen:
        for t in tiers:
            s0, e0 = window_for(words, dur, p, t)
            text = " ".join(x["w"] for x in words if s0 <= x["s"] < e0)
            picks.append({"start": s0, "end": e0, "peak": p, "tier": f"{t}s",
                          "score": round(float(score[p]), 2),
                          "why": why_for(p, loud, hype, heat, chatz, w_chat), "text": text[:400]})
    picks.sort(key=lambda x: (-x["score"], int(x["tier"][:-1])))
    for i, x in enumerate(picks, 1):
        x["n"] = i
    return picks


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src"); ap.add_argument("--top", type=int, default=8)
    ap.add_argument("--min", type=float, default=60); ap.add_argument("--max", type=float, default=90)
    ap.add_argument("--durations", default="",
                    help="comma-separated take lengths in seconds, e.g. 30,60,150 — every chosen moment "
                         "becomes one take per length (overrides --min/--max; --top counts moments)")
    ap.add_argument("--chat"); ap.add_argument("--max-minutes", type=int, default=90)
    a = ap.parse_args()
    d = job_dir(a.src)
    vid, info = fetch(a.src, d, a.max_minutes)
    dur = float(ffprobe(vid)["format"]["duration"])
    wav, db = audio_rms(vid, d)
    n = len(db)
    tr = transcribe(wav, d)
    words = tr["words"] if isinstance(tr, dict) else tr  # legacy transcripts were a bare list
    # signals
    loud = smooth(z(np.clip(loudness_spikes(db), 0, None)), 3)          # jumps above the local speech level
    hype = np.zeros(n)
    for w in words:
        if HYPE.search(w["w"]) and int(w["s"]) < n:
            hype[int(w["s"])] += 1
    hype = smooth(hype, 5)
    heat = np.zeros(n)
    for h in (info.get("heatmap") or []):
        start = int(h["start_time"])
        end = min(n, int(math.ceil(h["end_time"])))
        if 0 <= start < end:
            heat[start:end] = h["value"]
    chat = chat_series(d, n, a.chat, info)
    chatz = smooth(np.clip(z(smooth(chat, 10)), 0, None), 3)
    w_heat = 2.0 if heat.any() else 0
    w_chat = 1.5 if chat.any() else 0
    score = 1.0 * loud + 0.8 * z(hype).clip(0) + w_heat * z(heat).clip(0) + w_chat * chatz
    score = dampen_edges(score, n)
    tiers = sorted({int(x) for x in a.durations.split(",") if x.strip().isdigit()})
    if tiers:
        picks = pick_takes(score, words, dur, n, tiers, a.top, loud, hype, heat, chatz, w_chat)
    else:
        # pick non-overlapping windows around peaks
        picks, used = [], np.zeros(n, bool)
        order = np.argsort(-score)
        for p in order:
            if len(picks) >= a.top or score[p] <= 0:
                break
            s0 = max(0, p - 0.55 * a.max); e0 = min(dur, s0 + a.max)
            # snap start to the first word after s0 and end to a word end, keep length in [min, max]
            ws = [x for x in words if x["s"] >= s0 and x["e"] <= e0]
            if ws:
                s0 = max(0, ws[0]["s"] - 0.3); e0 = min(dur, ws[-1]["e"] + 0.6)
            if e0 - s0 < a.min:
                e0 = min(dur, s0 + a.min)
            e0 = min(e0, s0 + a.max)
            if used[int(s0):int(e0)].any():
                continue
            used[int(s0):int(e0)] = True
            text = " ".join(x["w"] for x in words if s0 <= x["s"] < e0)
            picks.append({"start": round(s0, 2), "end": round(e0, 2), "peak": int(p),
                          "score": round(float(score[p]), 2),
                          "why": why_for(p, loud, hype, heat, chatz, w_chat), "text": text[:400]})
        picks.sort(key=lambda x: x["start"])
        for i, x in enumerate(picks, 1):
            x["n"] = i
    meta = load_json(d / "info.json")
    save_json(d / "candidates.json", {"source": str(vid), "info": meta, "duration": dur, "candidates": picks,
                                      "signals": {"heatmap": bool(w_heat), "chat": bool(w_chat), "words": len(words)},
                                      "tiers": [f"{t}s" for t in tiers] if tiers else []})
    fmt = lambda t: f"{int(t // 60)}:{int(t % 60):02d}"
    md = [f"# Candidates: {meta.get('title', '')}", f"source: {meta.get('webpage_url', '')}",
          f"signals: loudness, transcript ({len(words)} words)" + (", most replayed" if w_heat else "") + (", chat" if w_chat else ""), ""]
    for x in picks:
        tier = f" [{x['tier']}]" if x.get("tier") else ""
        md += [f"## {x['n']}. {fmt(x['start'])}-{fmt(x['end'])} ({x['end'] - x['start']:.0f}s) score {x['score']}{tier}",
               f"why: {', '.join(x['why'])}", f"> {x['text']}", ""]
    (d / "candidates.md").write_text("\n".join(md), encoding="utf-8")
    print(f"job: {d}\n{len(picks)} candidates -> {d / 'candidates.md'}")


if __name__ == "__main__":
    main()

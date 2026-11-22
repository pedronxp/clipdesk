"""Melhores momentos: compile the top-ranked moments of a job into ONE horizontal video.

Reads jobs/<id>/candidates.json, picks the best moments (LLM total_score, or the operator's
own list), renders each one at 1920x1080 (gameplay full-screen; when facecam detects the
streamer's webcam, a picture-in-picture covers and highlights that cam region — it is already
burned into the frame, so this standardizes/enlarges it instead of duplicating it), fades each
segment in/out through black and concatenates everything with the concat demuxer (-c copy: one
encode per moment, no second pass). No burned captions and no transcription — the render is
straight off the analyzed source file.

Outputs (all inside the job, plus a post-ready kit):
  jobs/<id>/compile/plan.json      ordered moments actually used for planning
  jobs/<id>/compile/melhores.mp4   the compilation (uploadable: files under jobs/ pass the
                                   YouTube publisher's path guard)
  jobs/<id>/compile/thumb.jpg      1280x720 cover with the title band
  out/<id>-melhores/               melhores.mp4 + thumb.jpg + legendas.md

Progress goes to jobs/<id>/compile.json (status/stage/done/total) so the dashboard can poll it
the same way it polls creation.json.

usage: python scripts/compile.py jobs/<id> <options-json>
       options: {"moments": "3,5,8", "target_min": 10, "pip": true, "title": "..."}
"""
import json, os, re, shutil, sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import ROOT, ffprobe, load_json, run, save_json, tool
from pack import draw_band_filter

W, H = 1920, 1080
THUMB_W, THUMB_H = 1280, 720
PIP_W, PIP_H = 480, 270  # minimum webcam PiP size — grows to cover the burned-in cam
FADE = 0.5            # fade in/out through black on every moment (the simple look)
MOMENT_CAP = 120.0    # a moment longer than this is trimmed around its peak
LOUDNORM = "loudnorm=I=-14:TP=-1.5:LRA=11"
DEFAULT_TARGET_MIN = 10


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def update_state(job, **fields):
    """Patch the job's compile.json (best-effort, mirrors analyze_creation.set_stage)."""
    path = Path(job) / "compile.json"
    try:
        data = load_json(path) if path.exists() else {}
    except Exception:
        data = {}
    if isinstance(data, dict):
        data.update(fields)
        try:
            save_json(path, data)
        except Exception:
            pass


def resolve_source(job, data):
    """The video the moments were analyzed from. candidates.json["source"] is authoritative,
    but capture retention may have moved it — fall back to the job's own video like the
    dashboard's _moment_source_video does, so old captures still compile."""
    ref = str(data.get("source") or "")
    if ref and Path(ref).is_file():
        return Path(ref)
    cfr = Path(job) / "live" / "source_cfr.mp4"
    if cfr.exists():
        return cfr
    for f in sorted(Path(job).iterdir()):
        if f.is_file() and f.suffix.lower() in (".mp4", ".mkv", ".mov", ".webm") \
                and not re.fullmatch(r"clip_\d+.*", f.stem):
            return f
    return None


def score_of(c):
    return float((c.get("llm") or {}).get("total_score") or c.get("score") or 0)


def window_of(c):
    """(start, end, dur) of the moment, trimmed to MOMENT_CAP around the peak when longer."""
    start = float(c.get("start") or 0.0)
    end = float(c.get("end") or start)
    dur = max(1.0, end - start)
    if dur <= MOMENT_CAP:
        return start, end, dur
    peak = c.get("peak")
    if peak is None:  # no peak reference: keep the beginning of the window
        return start, start + MOMENT_CAP, MOMENT_CAP
    peak = float(peak)
    s = min(max(start, peak - MOMENT_CAP / 2), max(start, end - MOMENT_CAP))
    e = min(end, s + MOMENT_CAP)
    return s, e, e - s


def select_moments(cands, target_s, manual=None):
    """Moments for the compilation, always in chronological order in the final video.

    manual: "3,5,8" — the operator's pick, in candidates.json order. Auto: greedy fit — walk
    the candidates from the best score down and take every moment that still fits the target
    duration (the first one always goes in, so a too-short source still compiles)."""
    if manual:
        nums = re.findall(r"\d+", manual) if isinstance(manual, str) else [str(n) for n in manual]
        by_n = {int(c.get("n") or 0): c for c in cands}
        picked = [by_n[int(n)] for n in nums if int(n) in by_n]
        if not picked:
            sys.exit("nenhum dos momentos informados existe em candidates.json")
    else:
        picked, total = [], 0.0
        for c in sorted(cands, key=score_of, reverse=True):
            dur = window_of(c)[2]
            if not picked or total + dur <= target_s:
                picked.append(c)
                total += dur
    return sorted(picked, key=lambda c: float(c.get("start") or 0))


def pip_layout(source, s0, e0):
    """Picture-in-picture plan for the webcam, or None without a stable face.

    The streamer's webcam is already BURNED INTO the source (OBS composites it, on Twitch,
    YouTube and Kick alike) — a PiP elsewhere would show the person twice. So the PiP COVERS
    the detected cam region instead: same place, guaranteed minimum size (PIP_WxPIP_H) and a
    light border, i.e. "webcam em destaque" without any duplicate."""
    from facecam import find_cam  # lazy: opencv import only when PiP is actually wanted
    r = find_cam(str(source), s0, e0)
    if r.get("mode") not in ("cam", "face") or not r.get("box"):
        print(f"pip: sem webcam estável neste momento ({r.get('reason', '')}); só o gameplay")
        return None
    bx = list(r["box"])
    px = max(PIP_W, int(round(bx[2] * W)) + 8)
    py = max(PIP_H, int(round(bx[3] * H)) + 8)
    px += px % 2
    py += py % 2
    x0 = min(max(0, int(round(bx[0] * W)) - 4), W - px)
    y0 = min(max(0, int(round(bx[1] * H)) - 4), H - py)
    x, y, w, h = (f"{float(v):.4f}" for v in bx)
    crop = (f"crop=iw*{w}:ih*{h}:iw*{x}:ih*{y},"
            f"scale={px}:{py}:force_original_aspect_ratio=decrease,"
            f"pad={px}:{py}:(ow-iw)/2:(oh-ih)/2:black,"
            f"drawbox=x=0:y=0:w=iw:h=ih:color=white@0.85:t=4")
    return {"crop": crop, "x": x0, "y": y0}


def moment_cmd(src, s0, e0, out, pip=None, vertical=False, silent=False):
    """ffmpeg command for ONE moment: 1920x1080 full-frame gameplay, optional webcam PiP
    covering the streamer's own cam region, fade in/out through black, -14 LUFS. Every moment
    is encoded with the SAME parameters so the concat demuxer can join them with -c copy. The
    video always goes through -filter_complex with an explicit -map [vo]: a bare -map 0:a?
    would disable automatic stream selection and silently drop the video (first smoke test).

    Generalização para o vídeo viral (momentos de jobs diferentes no mesmo vídeo): fps e SAR
    são FORÇADOS (fontes de VOD podem vir a 60 fps; concat -c copy exige stream uniforme),
    áudio sempre estéreo (-ac 2) e, sem faixa de áudio, um anullsrc entra no lugar. Fonte
    vertical vira fundo desfocado + gameplay centrado (o crop agressivo perderia o frame)."""
    dur = max(0.5, e0 - s0)
    out_st = max(0.0, dur - FADE)
    finish = f"fps=30,setsar=1,fade=t=in:st=0:d={FADE},fade=t=out:st={out_st:.2f}:d={FADE}"
    prep = f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H}"
    if vertical:
        # fundo: a própria fonte esticada + blur; frente: gameplay inteiro centrado
        graph = (f"[0:v]split[fgsrc][bgsrc];"
                 f"[bgsrc]{prep},boxblur=20:2[bgb];"
                 f"[fgsrc]scale=-2:{H}[fgs];"
                 f"[bgb][fgs]overlay=(W-w)/2:(H-h)/2,{finish}[vo]")
    elif pip:
        graph = (f"[0:v]split[base][camsrc];"
                 f"[camsrc]{pip['crop']}[pip];"
                 f"[base]{prep}[bg];"
                 f"[bg][pip]overlay={pip['x']}:{pip['y']},{finish}[vo]")
    else:
        graph = f"[0:v]{prep},{finish}[vo]"
    cmd = [tool("ffmpeg"), "-y", "-v", "error", "-ss", f"{s0:.2f}", "-to", f"{e0:.2f}", "-i", str(src)]
    if silent:  # concat demuxer exige que todo segmento tenha o mesmo layout de áudio
        cmd += ["-f", "lavfi", "-t", f"{dur:.2f}", "-i", "anullsrc=r=48000:cl=stereo",
                "-filter_complex", graph, "-map", "[vo]", "-map", "1:a"]
    else:
        cmd += ["-filter_complex", graph, "-map", "[vo]", "-map", "0:a?"]
    cmd += ["-af", f"{LOUDNORM},afade=t=in:st=0:d={FADE},afade=t=out:st={out_st:.2f}:d={FADE}",
            "-t", f"{dur:.2f}",
            "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2", str(out)]
    return cmd


def probe_media(path):
    """(vertical, silent) da fonte: fps/SAR/áudio têm de ser uniformes para o concat -c copy.
    Sem informação de streams (probe indisponível), assume paisagem com áudio — o cenário das
    capturas CFR."""
    try:
        data = ffprobe(path)
        streams = data.get("streams") or []
        v = next((s for s in streams if s.get("codec_type") == "video"), None)
        a = any(s.get("codec_type") == "audio" for s in streams)
        w, h = int(v.get("width") or 0), int(v.get("height") or 0)
        if v and (v.get("side_data_list") or [{}])[0].get("rotation") in (90, -90, 270, -270):
            w, h = h, w
        vertical = bool(w and h and h > w)
        return vertical, not a
    except (Exception, SystemExit):  # common.run aborta com SystemExit em probe falho
        return False, False


def probe_duration(path):
    try:
        return max(0.0, float(ffprobe(path).get("format", {}).get("duration") or 0.0))
    except Exception:
        return 0.0


def fallback_title(info, channels=None):
    ch = [c for c in (channels if channels is not None else [str(info.get("channel") or info.get("uploader") or "").strip()]) if c]
    if len(ch) <= 1:
        return f"MELHORES MOMENTOS — {ch[0] if ch else 'GTA 6'}"
    return f"VÍDEO VIRAL — {' · '.join(ch[:3])}"


def grab_thumb(mom_file, mom_dur, title, out):
    """1280x720 cover from a frame inside the best moment (40% in, near the peak) with the
    same Arial Black band used by the clips' thumbnails."""
    t = max(0.1, mom_dur * 0.4)
    vf = (f"scale={THUMB_W}:{THUMB_H}:force_original_aspect_ratio=increase,crop={THUMB_W}:{THUMB_H}"
          + draw_band_filter(title, height=THUMB_H, band_center=THUMB_H // 2))
    run([tool("ffmpeg"), "-y", "-v", "error", "-ss", f"{t:.2f}", "-i", str(mom_file),
         "-frames:v", "1", "-vf", vf, "-q:v", "2", str(out)])
    return out


def fmt_ts(t):
    t = int(round(t))
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def yt_description(title, rendered):
    """YouTube description with the moment list timestamped against THIS video."""
    at = 0.0
    rows = []
    for i, m in enumerate(rendered, 1):
        rows.append(f"{fmt_ts(at)} Momento {i} — {m['title']}")
        at += m["dur"]
    return "\n".join([title, "Melhores momentos da live, escolhidos pelo motor de análise.", ""] + rows)


def legendas_md(title, channel, source_url, rendered):
    """PT-BR posting kit for the compilation: title, credit, and the YouTube description."""
    cred = f"Crédito: {channel}" if channel else ""
    src = f"Fonte: {source_url}" if source_url else ""
    head = "\n".join(x for x in (f"# {title}", cred, src) if x)
    return f"{head}\n\n**YouTube (vídeo)**\n\n{yt_description(title, rendered)}\n"


def run_compile(job, options):
    """Compilação de melhores momentos.

    Modo single (padrão): options.moments/"target_min" sobre UM job — fluxo e mensagens
    originais preservados. Modo multi (vídeo viral): options.moments_plan = [{"job","n"}…]
    traz momentos de QUALQUER projeto; cada entrada usa a fonte do seu próprio job e a
    ordem escolhida pelo operador."""
    plan_jobs = options.get("moments_plan") if options.get("multi") else None

    update_state(job, stage="planejando os momentos")
    target_s = max(2.0, float(int(options.get("target_min") or DEFAULT_TARGET_MIN))) * 60
    cdir = job / "compile"
    cdir.mkdir(exist_ok=True)

    infos = {}  # job id -> candidates.json (cache compartilhado entre as entradas)

    def job_data(jid):
        if jid not in infos:
            jd = job if jid == job.name else job.parent / jid
            infos[jid] = load_json(jd / "candidates.json")
        return infos[jid]

    entries = []
    if plan_jobs:
        for item in plan_jobs:
            jid = str(item.get("job") or "").strip()
            try:
                n = int(item.get("n") or 0)
            except (TypeError, ValueError):
                sys.exit(f"momento inválido no plano: {item!r}")
            if not (job.parent / jid / "candidates.json").is_file() and jid != job.name:
                sys.exit(f"projeto {jid} não tem momentos analisados")
            cand = next((c for c in job_data(jid).get("candidates") or []
                         if int(c.get("n") or 0) == n), None)
            if cand is None:
                sys.exit(f"momento {n} não existe em {jid}")
            s, e, dur = window_of(cand)
            entries.append({"job": jid, "n": n, "start": round(s, 2), "end": round(e, 2),
                            "dur": round(dur, 2), "score": score_of(cand),
                            "title": str((cand.get("llm") or {}).get("hook_title") or "").strip() or f"Momento {n}"})
    else:
        data = load_json(job / "candidates.json")
        cands = data.get("candidates") or []
        if not cands:
            sys.exit("candidates.json não tem momentos; analise a fonte antes de compilar")
        source = resolve_source(job, data)
        if not source:
            sys.exit("o vídeo da análise não está mais em disco; analise a fonte novamente")
        manual = re.findall(r"\d+", str(options.get("moments") or ""))
        picked = select_moments(cands, target_s, manual)
        for c in picked:
            s, e, dur = window_of(c)
            entries.append({"job": job.name, "n": int(c.get("n") or 0), "start": round(s, 2), "end": round(e, 2),
                            "dur": round(dur, 2), "score": score_of(c),
                            "title": str((c.get("llm") or {}).get("hook_title") or "").strip() or f"Momento {c.get('n')}"})

    sources = {}

    def source_of(e):
        jid = e["job"]
        if jid not in sources:
            jd = job if jid == job.name else job.parent / jid
            sources[jid] = resolve_source(jd, job_data(jid))
        return sources[jid]

    if plan_jobs:
        save_json(cdir / "plan.json", {"job": job.name, "multi": True,
                                       "target_min": options.get("target_min") or DEFAULT_TARGET_MIN,
                                       "pip": bool(options.get("pip", True)),
                                       "entries": [{**e, "source": str(source_of(e) or "")} for e in entries]})
    else:
        save_json(cdir / "plan.json", {"job": job.name, "source": str(source),
                                       "target_min": options.get("target_min") or DEFAULT_TARGET_MIN,
                                       "pip": bool(options.get("pip", True)), "moments": entries})

    use_pip = options.get("pip", True) not in (False, "false", "none", "off")
    media = {}  # source path -> (vertical, silent)

    def flags_of(e):
        src = source_of(e)
        if src is None:
            return None, False, False
        key = str(src)
        if key not in media:
            media[key] = probe_media(src)
        return src, media[key][0], media[key][1]

    mom_files, rendered, skipped = [], [], []
    n = len(entries)
    for i, e in enumerate(entries, 1):
        src, vertical, silent = flags_of(e)
        if src is None:
            print(f"momento {e['n']} ({e['job']}): fonte não está mais em disco; pulado", file=sys.stderr)
            skipped.append(e["n"])
            continue
        update_state(job, stage=f"renderizando momento {i}/{n}", done=len(rendered), total=n)
        out = cdir / f"mom_{i:02d}.mp4"
        try:
            pip = pip_layout(src, e["start"], e["end"]) if (use_pip and not vertical) else None
            if use_pip and vertical:
                print(f"momento {e['n']}: fonte vertical — fundo desfocado, sem PiP", file=sys.stderr)
            run(moment_cmd(src, e["start"], e["end"], out, pip, vertical=vertical, silent=silent))
        except SystemExit as exc:  # one bad moment must not kill the whole compilation
            print(f"momento {e['n']} falhou e foi pulado: {exc}", file=sys.stderr)
            skipped.append(e["n"])
            continue
        mom_files.append(out)
        rendered.append(e)
    if not mom_files:
        sys.exit("nenhum momento foi renderizado")
    if skipped:
        print(f"pulados: momentos {', '.join(map(str, skipped))}", file=sys.stderr)

    update_state(job, stage="juntando os momentos", done=len(rendered), total=n)
    concat = cdir / "concat.txt"
    concat.write_text("".join(f"file '{p.resolve().as_posix()}'\n" for p in mom_files), encoding="utf-8")
    final = cdir / "melhores.mp4"
    run([tool("ffmpeg"), "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(concat),
         "-c", "copy", "-movflags", "+faststart", str(final)])
    concat.unlink(missing_ok=True)

    update_state(job, stage="gerando a capa")
    best = max(range(len(rendered)), key=lambda i: rendered[i]["score"])
    info = (job_data(rendered[best]["job"]).get("info") or {}) if plan_jobs else (data.get("info") or {})
    chans = []
    for e in rendered:
        ei = (job_data(e["job"]).get("info") or {}) if plan_jobs else info
        ch = str(ei.get("channel") or ei.get("uploader") or "").strip()
        if ch and ch not in chans:
            chans.append(ch)
    title = str(options.get("title") or "").strip() or fallback_title(info, channels=chans if plan_jobs else None)
    thumb = cdir / "thumb.jpg"
    grab_thumb(mom_files[best], probe_duration(mom_files[best]), title, thumb)

    for p in mom_files:  # intermediates are disposable once the concat is done
        p.unlink(missing_ok=True)

    update_state(job, stage="exportando o kit")
    channel = ", ".join(chans) if plan_jobs else str(info.get("channel") or info.get("uploader") or "")
    best_info = (job_data(rendered[best]["job"]).get("info") or {}) if plan_jobs else info
    source_url = str(best_info.get("webpage_url") or "")
    leg = cdir / "legendas.md"
    leg.write_text(legendas_md(title, channel, source_url, rendered), encoding="utf-8")
    dest = ROOT / "out" / (job.name if job.name.startswith("viral-") else f"{job.name}-melhores")
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copy2(final, dest / "melhores.mp4")
    shutil.copy2(thumb, dest / "thumb.jpg")
    shutil.copy2(leg, dest / "legendas.md")

    update_state(job, status="ready", stage="", finished=now_iso(),
                 output=f"jobs/{job.name}/compile/melhores.mp4",
                 thumb=f"jobs/{job.name}/compile/thumb.jpg",
                 kit=dest.relative_to(ROOT).as_posix(),
                 moments=len(rendered), duration=round(probe_duration(final), 1),
                 title=title, description=yt_description(title, rendered),
                 multi=bool(plan_jobs))
    print(final)


def main():
    if len(sys.argv) != 3:
        print("usage: compile.py <job> <options-json>", file=sys.stderr)
        return 2
    job = Path(sys.argv[1])
    options = json.loads(sys.argv[2] or "{}")
    update_state(job, status="running", pid=os.getpid(), started=now_iso(), stage="planejando os momentos",
                 error=None, output=None, thumb=None, kit=None, moments=None, duration=None, finished=None,
                 done=0, total=0)
    try:
        run_compile(job, options)
    except BaseException as exc:  # SystemExit from common.run counts as a failure too
        update_state(job, status="failed", error=str(exc) or exc.__class__.__name__,
                     stage="", finished=now_iso())
        raise
    return 0


if __name__ == "__main__":
    sys.exit(main())

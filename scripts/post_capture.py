"""Pós-captura: transforms an ended live capture into post-ready clips.

Pipeline (spec D0 — runs automatically after every dashboard capture, one at a time
via the server's queue; monitor.py's CLI delegates to the same run()):
  CFR rebuild -> chat.json -> moments.py --chat -> rank_llm.py (LLM re-rank, skipped
  when no LLM is configured) -> cut.py --preset for the top N -> translate.py for
  extra languages -> pack.py --export

Failure semantics (spec D0/D1): a failed analysis writes monitor.json {"error","failed_ts"}
and keeps the capture on disk — the card shows the real reason instead of the material
vanishing. Only a capture with no seg_*.ts at all (or one whose full pipeline produced no
candidate) is a void and gets discarded.

Draft titles are the cleaned first line of the moment's own speech and are marked for human
review (review title is a human decision). Nothing is uploaded.
"""
import argparse, csv, json, os, re, subprocess, sys
from datetime import datetime, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from common import JOBS, ROOT, ffprobe, job_dir, load_json, preset_values, rebuild_cfr, save_json
from cut import clean
import voids

PY = sys.executable
SCRIPTS = ROOT / "scripts"


def run_step(cmd, title):
    print(f"\n== {title} ==\n$ {' '.join(str(x) for x in cmd)}", flush=True)
    return subprocess.run([str(x) for x in cmd], cwd=str(ROOT), env=os.environ.copy()).returncode


def segment_table(live_jd):
    """[(path, cumulative_start, duration, wall_end, wall_start)] in recording order."""
    ld = Path(live_jd) / "live"
    rows = []
    csv_path = ld / "segments.csv"
    entries = []
    if csv_path.exists():
        for row in csv.reader(csv_path.read_text(encoding="utf-8", errors="ignore").splitlines()):
            if len(row) >= 3 and (ld / row[0]).exists():
                try:
                    entries.append((ld / row[0], float(row[2]) - float(row[1])))
                except ValueError:
                    continue
    if not entries:
        for f in sorted(ld.glob("seg_*.ts")):
            try:
                dur = float(ffprobe(f)["format"]["duration"])
            except Exception:
                dur = 0.0
            entries.append((f, dur))
    cum = 0.0
    for path, dur in entries:
        mtime = path.stat().st_mtime
        rows.append((path, cum, dur, mtime, mtime - dur))
        cum += dur
    return rows


def twitch_chat_json(live_jd, out_path):
    """Map wall-clock Twitch chat timestamps onto the rebuilt source timeline, using each
    segment's mtime as the wall-clock end of that segment."""
    src = Path(live_jd) / "live" / "chat.twitch.jsonl"
    comments = []
    table = segment_table(live_jd)
    if src.exists() and table:
        total = table[-1][1] + table[-1][2]
        for line in src.read_text(encoding="utf-8", errors="ignore").splitlines():
            try:
                m = json.loads(line)
                t = float(m["t"])
                text = (m.get("text") or "").strip()
            except Exception:
                continue
            if not text:
                continue
            if t <= table[0][4]:
                off = 0.0
            elif t >= table[-1][3]:
                off = total
            else:
                off = total
                for _p, cum, dur, wall_end, wall_start in table:
                    if wall_start <= t <= wall_end:
                        off = cum + max(0.0, min(dur, t - wall_start))
                        break
            comments.append({"content_offset_seconds": round(off, 2),
                             "message": {"body": text}})
    save_json(out_path, {"comments": comments})
    return len(comments)


def youtube_chat_json(live_jd, out_path):
    """YouTube live_chat replay already carries stream offsets; reuse them directly."""
    files = sorted((Path(live_jd) / "live").glob("chat*.live_chat.json*"))
    comments = []
    if files:
        for line in files[0].read_text(encoding="utf-8", errors="ignore").splitlines():
            try:
                a = json.loads(line)["replayChatItemAction"]
                t = int(a["videoOffsetTimeMsec"]) / 1000
                actions = json.dumps(a.get("actions") or [], ensure_ascii=False)
                text = re.sub(r'"text":"([^"]*)"', r"\1 ", actions).strip() or "chat"
            except Exception:
                continue
            comments.append({"content_offset_seconds": round(t, 2), "message": {"body": text}})
    save_json(out_path, {"comments": comments})
    return len(comments)


def build_chat(live_jd):
    out = Path(live_jd) / "live" / "chat.json"
    n = twitch_chat_json(live_jd, out)
    if not n:
        n = youtube_chat_json(live_jd, out)
    return out, n


def draft_title(text, n):
    """Cleaned first line of the moment. A human still writes the creative title (D5)."""
    text = clean(" ".join((text or "").split()), profanity=False)
    text = re.split(r"(?<=[.!?])\s+", text)[0]
    text = text.strip(" -–—:;|")[:86].strip()
    if len(text) < 8:
        text = f"Momento {n} de GTA 6"
    return text[:1].upper() + text[1:]


def slugify(s):
    s = re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")
    return s[:80] or "clip"


def extra_langs(base, langs):
    return [x for x in (langs or "").split(",") if x.strip() and x.strip() != base]


def parse_legendas(value, base, langs):
    """--legendas original|pt|pt,en overrides the preset's language plan."""
    v = (value or "").strip().lower()
    if not v:
        return base, extra_langs(base, langs)
    if v in ("original", "none", "off"):
        return "", []
    parts = [x.strip() for x in v.split(",") if x.strip()]
    return (parts[0] if parts else ""), parts[1:]


def blur_cut_cmd(cut_cmd, pvals):
    """Same cut without the cam: blur layout, and the cam zoom replaced by the game zoom
    (there is no cam panel to punch in on)."""
    zoom = pvals.get("zoom") or "off"
    if zoom in ("face", "both"):
        zoom = "game"
    if "--zoom" in cut_cmd:
        i = cut_cmd.index("--zoom")
        cut_cmd = cut_cmd[:i + 1] + [zoom] + cut_cmd[i + 2:]
    return cut_cmd + ["--layout", "blur"]


def discard_void(keep, *folders):
    """A capture that ended without a single clip is a void: it is deleted, not stored.
    With --keep-void the folders stay on disk for a manual retry."""
    if keep:
        print("live sem cortes mantida (--keep-void)")
        return
    for folder in folders:
        folder = Path(folder)
        if not folder.is_dir():
            continue
        if (folder / "monitor.json").is_file():
            print(f"{folder.name} mantida: tem registro de um monitoramento concluído")
            continue
        row = voids.discard(folder, reason="live encerrada sem cortes")
        if row:
            print(f"live sem cortes descartada: {row['job']} ({row['mb']} MB liberados)")
        else:
            print(f"{folder.name} mantida: já tem vídeos (spikes ou cortes)")


def write_monitor_json(live_jd, payload):
    save_json(Path(live_jd) / "monitor.json", payload)


def already_processed(live_jd):
    """True when a finished, error-free monitor.json is newer than the newest segment:
    this capture was already analyzed (queued rerun after a manual run, or duplicate).
    Failure records stay re-runnable."""
    live_jd = Path(live_jd)
    monitor = live_jd / "monitor.json"
    if not monitor.is_file():
        return False
    try:
        data = json.loads(monitor.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if data.get("error"):
        return False
    try:
        newest_seg = max(s.stat().st_mtime for s in (live_jd / "live").glob("seg_*.ts"))
    except (OSError, ValueError):
        return True  # nothing left to analyze
    return monitor.stat().st_mtime >= newest_seg


def run(live_jd, top=3, preset="", legendas="", out="out", export=True, keep_void=False, slug="", force=False):
    """Full post-capture pipeline for one ended capture. Returns 0 with clips cut and
    monitor.json written, or 1 with monitor.json {"error"} (material kept) unless the
    capture holds nothing at all — then it is discarded like any other void."""
    live_jd = Path(live_jd)

    if voids.recording_running(live_jd):
        print("captura ainda em andamento; pós-captura adiada", file=sys.stderr)
        return 1

    segs = sorted((live_jd / "live").glob("seg_*.ts"))
    if not segs:
        print("no segments recorded; stopping", file=sys.stderr)
        discard_void(keep_void, live_jd)
        return 1

    if already_processed(live_jd) and not force:
        print("captura já analisada; pulando (use --force para refazer)", file=sys.stderr)
        return 0
    pname = ""  # set inside the try; fail() may run before/without it

    def fail(msg):
        print(f"pós-captura falhou: {msg}", file=sys.stderr)
        write_monitor_json(live_jd, {"preset": pname, "error": msg, "failed_ts": datetime.now(
            timezone.utc).isoformat(timespec="seconds")})
        return 1

    try:
        # Inside the try: an unknown preset (settings.json edited by hand, key renamed in
        # presets.json) must leave a monitor.json error record, not respawn forever.
        pname, pvals = preset_values(preset)
        pname = pname or ""
        base = (pvals.get("lang") or "").strip()
        langs = (pvals.get("langs") or "").strip()
        base, extras = parse_legendas(legendas, base, langs)
        print(f"preset: {pname} | base captions: {base or 'original (word)'} | variants: {', '.join(extras) or '-'}")

        source = live_jd / "live" / "source_cfr.mp4"
        source.unlink(missing_ok=True)
        rebuild_cfr(segs, source)
        print(f"source: {source}")

        chat, nchat = build_chat(live_jd)
        print(f"chat: {nchat} messages -> {chat}")

        moments_jd = job_dir(str(source))
        cmd = [PY, SCRIPTS / "moments.py", source, "--top", top, "--min", "60", "--max", "90"]
        if nchat:
            cmd += ["--chat", chat]
        if run_step(cmd, "rank moments") != 0:
            return fail(f"moments.py falhou (rc != 0)")

        cj = moments_jd / "candidates.json"
        if not cj.exists():
            return fail("moments.py não escreveu candidates.json")
        # LLM re-rank: precision on top of the signal heuristics (supoclip idea, our own code).
        # rank_llm.py exits 3 with no LLM configured — that is "skip", not a failure.
        if run_step([PY, SCRIPTS / "rank_llm.py", str(moments_jd), "--top", str(top)], "re-rank (LLM)") != 0:
            print("re-rank indisponível; seguindo com a heurística", file=sys.stderr)
        cands = load_json(cj).get("candidates") or []
        cands.sort(key=lambda c: -float((c.get("llm") or {}).get("total_score") or c.get("score") or 0))
        picks = cands[: top]
        if not picks:
            print("no candidates; stopping", file=sys.stderr)
            discard_void(keep_void, moments_jd, live_jd)
            return 1

        info = load_json(live_jd / "info.json") if (live_jd / "info.json").exists() else {}
        channel = info.get("channel") or ""
        summary = []
        for c in picks:
            n = int(c["n"])
            llm = c.get("llm") or {}
            title = str(llm.get("hook_title") or "").strip() or draft_title(c.get("text", ""), n)
            cut_cmd = [PY, SCRIPTS / "cut.py", moments_jd, "--pick", n]
            if pname:
                cut_cmd += ["--preset", pname]
            cut_cmd += ["--title", title, "--title-style", pvals.get("title_style") or "box",
                        "--zoom", pvals.get("zoom") or "off"]
            cut_cmd += ["--lang", base or "none"]  # explicit value overrides the preset for --legendas original
            if pvals.get("allow_profanity"):
                cut_cmd += ["--allow-profanity"]
            rc = run_step(cut_cmd, f"cut clip {n}")
            if rc != 0 and pvals.get("layout") == "react":
                # No facecam in this moment (or in this stream): a react clip is impossible, so
                # fall back to the blur layout instead of losing the moment.
                print("react cut skipped (no facecam): retrying with the blur layout", file=sys.stderr)
                rc = run_step(blur_cut_cmd(cut_cmd, pvals), f"cut clip {n} (blur)")
            if rc != 0:
                continue
            side = moments_jd / "edits" / f"clip_{n}.json"
            meta = load_json(side) if side.exists() else {}
            meta.update({"title_review": True, "title_draft": title, "preset": pname,
                         "hook_type": llm.get("hook_type") or "", "llm_total": llm.get("total_score")})
            save_json(side, meta)

            variants = []
            if extras:
                tcmd = [PY, SCRIPTS / "translate.py", moments_jd, "--pick", n]
                if pname:
                    tcmd += ["--preset", pname]
                tcmd += ["--langs", ",".join(extras), "--zoom", pvals.get("zoom") or "off"]
                if pvals.get("voice"):
                    tcmd.append("--voice")
                if pvals.get("dub"):
                    tcmd.append("--dub")
                if run_step(tcmd, f"translate clip {n}") == 0:
                    variants = extras

            exported = ""
            if export:
                s = slug or f"{live_jd.name}-clip{n}"
                dest = Path(out) / slugify(s)
                pcmd = [PY, SCRIPTS / "pack.py", moments_jd, "--pick", n, "--export", dest,
                        "--title", title, "--credit", channel]
                if run_step(pcmd, f"pack clip {n}") == 0:
                    exported = str(dest)
            summary.append({"n": n, "title": title, "review": True, "variants": variants,
                            "export": exported,
                            "clip": str(moments_jd / "edits" / f"clip_{n}.mp4")})

        print("\n== resumo ==")
        for s in summary:
            print(f"clip {s['n']}: {s['title']}  (título-rascunho: revisar)")
            print(f"  vídeo: {s['clip']}")
            if s["variants"]:
                print(f"  variantes: {', '.join(s['variants'])}")
            if s["export"]:
                print(f"  export: {s['export']} (clip.mp4 + thumb.jpg + legendas.md)")
        if not summary:
            print("nenhum clipe gerado")
            discard_void(keep_void, moments_jd, live_jd)
            return 1
        write_monitor_json(live_jd, {"preset": pname, "source": str(source),
                                     "moments_job": str(moments_jd), "clips": summary})
        return 0
    except (Exception, SystemExit) as exc:
        # SystemExit included: a corrupt segment makes ffprobe sys.exit — the capture still
        # deserves its monitor.json error record instead of silently vanishing (spec D0).
        return fail(str(exc))


def main():
    ap = argparse.ArgumentParser(description="Run the post-capture pipeline for an ended live job.")
    ap.add_argument("job_id")
    ap.add_argument("--top", type=int, default=3)
    ap.add_argument("--preset", default="")
    ap.add_argument("--legendas", default="")
    ap.add_argument("--out", default="out")
    ap.add_argument("--slug", default="")
    ap.add_argument("--no-export", action="store_true")
    ap.add_argument("--force", action="store_true", help="re-analyze even if monitor.json is fresh")
    a = ap.parse_args()
    jd = JOBS / a.job_id
    if not jd.is_dir():
        print(f"job não encontrado: {jd}", file=sys.stderr)
        return 1
    return run(jd, top=a.top, preset=a.preset, legendas=a.legendas, out=a.out,
               export=not a.no_export, slug=a.slug, force=a.force)


if __name__ == "__main__":
    sys.exit(main())

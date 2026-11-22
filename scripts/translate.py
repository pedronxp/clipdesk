"""One clip, many languages.

Translates the spoken sentences (whole-sentence captions, same engine as cut.py --lang) and the hook
of an already cut clip, then renders jobs/<id>/edits/clip_<n>_<lang>.mp4 with the same layout, writes
a clip_<n>_<lang>.json sidecar per language (so the dashboard lists variants) and clip_<n>_captions.md
with per-platform captions in each language.

usage: python translate.py jobs/<id> --pick <n> --langs es,pt,de,ru [--voice] [--layout ...] [--hook "..."] [--captions sentence|word]

--captions word is the legacy word-by-word spread: not recommended for translated captions because the
translated words do not match the spoken timing. --voice adds a stock text-to-speech voice (edge-tts,
Microsoft neural stock voices) that speaks the translated hook only, over the first 2.5 s, with the
original audio ducked under it. It never clones or imitates a real person's or streamer's voice.
"""
import argparse, asyncio, sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from common import ffprobe, load_json, preset_values, run, save_json, tool
from cut import (CAP_Y, build_ass, build_ass_sentences, cap_pos, ff_color, game_offset, load_transcript,
                 react_title_band, sentences, set_orientation, translator, tr, translate_sentences,
                 video_filter, wrap)

VOICES = {"es": "es-ES-AlvaroNeural", "pt": "pt-BR-AntonioNeural", "de": "de-DE-ConradNeural",
          "ru": "ru-RU-DmitryNeural", "fr": "fr-FR-HenriNeural", "it": "it-IT-DiegoNeural",
          "pl": "pl-PL-MarekNeural", "tr": "tr-TR-AhmetNeural", "ja": "ja-JP-KeitaNeural",
          "ko": "ko-KR-InJoonNeural", "ar": "ar-SA-HamedNeural", "hi": "hi-IN-MadhurNeural"}
HOOK_SECONDS = 2.5
HASHTAGS = ["#GTA6", "#GTAVI", "#gaming", "#rockstargames", "#gta"]


def translate_words(t, words, cache=None, lang=None):
    """Translate each sentence, then spread the translated words evenly over the sentence's timing."""
    res = []
    for sent in sentences(words):
        new = tr(t, " ".join(w["w"] for w in sent), cache, lang=lang).split()
        if not new:
            continue
        a, b = sent[0]["s"], sent[-1]["e"]
        step = (b - a) / len(new)
        res += [{"w": x, "s": a + i * step, "e": a + (i + 1) * step} for i, x in enumerate(new)]
    return res


def write_variant_meta(edits, n, lang, base):
    """Per-language sidecar JSON so the dashboard can list translated variants with badges."""
    meta = dict(base)
    meta["file"] = str(Path(edits) / f"clip_{n}_{lang}.mp4")
    meta["lang_target"] = lang
    meta["variant_of"] = f"clip_{n}"
    save_json(Path(edits) / f"clip_{n}_{lang}.json", meta)
    return meta


def make_voice(lang, text, dest):
    try:
        import edge_tts
    except ImportError:
        sys.exit("edge-tts missing. Run: pip install -r requirements.txt")
    voice = VOICES.get(lang)
    if not voice:
        print(f"no stock voice configured for '{lang}', skipping --voice", file=sys.stderr)
        return None
    asyncio.run(edge_tts.Communicate(text, voice).save(str(dest)))
    return dest


def make_dubs(lang, events, s0, out_dir):
    """One stock TTS mp3 per caption sentence, tempo-fitted to its slot (a line that runs into
    the next sentence is sped up, never overlapped). Returns [(path, offset_s, tempo)] for the
    mix; empty when the language has no stock voice. Text-to-speech never imitates the
    streamer's voice — it is a neutral narrator reading the translated captions."""
    import edge_tts
    voice = VOICES.get(lang)
    if not voice:
        print(f"no stock voice configured for '{lang}', skipping --dub", file=sys.stderr)
        return []
    out = []
    for i, e in enumerate(events):
        text = (e.get("text") or "").strip()
        if not text:
            continue
        path = Path(out_dir) / f"dub_{lang}_{i}.mp3"
        asyncio.run(edge_tts.Communicate(text, voice).save(str(path)))
        nxt = events[i + 1]["s"] if i + 1 < len(events) else None
        slot = max(1.0, float((nxt if nxt is not None else e["e"])) - float(e["s"]))
        try:
            dur = float(ffprobe(path)["format"]["duration"])
        except Exception:
            dur = 0.0
        tempo = min(2.0, dur / slot) if dur > slot * 1.05 else 0.0
        out.append((path, max(0.0, float(e["s"]) - s0), tempo))
    return out


def dub_audio_filter(dubs, duck=0.35):
    """Ducked original audio + every dub clip delayed to its sentence start."""
    n = len(dubs)
    parts = [f"[0:a]volume={duck}[bed]"]
    labels = []
    for i, (path, offset, tempo) in enumerate(dubs):
        tempo_s = f"atempo={tempo:.3f}," if tempo and tempo > 1.001 else ""
        delay = int(offset * 1000)
        parts.append(f"[{i + 1}:a]aformat=sample_rates=48000:channel_layouts=mono,{tempo_s}"
                     f"adelay={delay}:all=1[d{i}]")
        labels.append(f"[d{i}]")
    return (";".join(parts) + f";[bed]{''.join(labels)}"
            f"amix=inputs={n + 1}:duration=first:normalize=0,loudnorm=I=-14:TP=-1.5:LRA=11[ao]")


def render(src, s0, e0, layout, cam, ass, mp4, voice_mp3=None, band=None, src_dims=None, game_x=None,
           zoom="off", dubs=None, band_fill=None):
    v = video_filter(layout, cam, ass, band=band, src=src_dims, game_x=game_x,
                     zoom=zoom, zoom_secs=e0 - s0, band_fill=band_fill)
    cmd = [tool("ffmpeg"), "-y", "-v", "error", "-ss", f"{s0:.2f}", "-to", f"{e0:.2f}", "-i", src]
    if dubs:
        for path, _off, _tempo in dubs:
            cmd += ["-i", str(path)]
        cmd += ["-filter_complex", v + ";" + dub_audio_filter(dubs),
                "-map", "[vo]", "-map", "[ao]"]
    elif voice_mp3:
        dur = float(ffprobe(voice_mp3)["format"]["duration"])
        tempo = f"atempo={min(2.0, dur / HOOK_SECONDS):.3f}," if dur > HOOK_SECONDS else ""
        cmd += ["-i", str(voice_mp3)]
        a = (f"[0:a]volume=0.2:enable='lt(t,{HOOK_SECONDS})'[bed];"
             f"[1:a]{tempo}atrim=0:{HOOK_SECONDS},afade=t=out:st={HOOK_SECONDS - 0.15}:d=0.15,apad[tts];"
             f"[bed][tts]amix=inputs=2:duration=first:normalize=0,loudnorm=I=-14:TP=-1.5:LRA=11[ao]")
        cmd += ["-filter_complex", v + ";" + a, "-map", "[vo]", "-map", "[ao]"]
    else:
        cmd += ["-filter_complex", v, "-map", "[vo]", "-map", "0:a?", "-af", "loudnorm=I=-14:TP=-1.5:LRA=11"]
    cmd += ["-t", f"{e0 - s0:.2f}",
            "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k",
            "-ar", "48000", "-movflags", "+faststart", str(mp4)]
    run(cmd)


def platform_block(lang, hook, credit, url):
    line = hook or ""
    cred = ""
    src = f"\n{url}" if url else ""
    x = "\n".join(p for p in [line, cred] if p) + src
    ig = "\n".join(p for p in [line, cred, " ".join(HASHTAGS[:4])] if p)
    tk = "\n".join(p for p in [line, cred, " ".join(HASHTAGS[:3])] if p)
    return (f"## {lang}\n\n**X** (no hashtags)\n\n{x}\n\n**Instagram** (4 hashtags)\n\n{ig}\n\n"
            f"**TikTok / YouTube Shorts**\n\n{tk}\n")


def main():
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--preset", default="")
    pre_a, _ = pre.parse_known_args()
    _pname, pvals = preset_values(pre_a.preset) if pre_a.preset else (None, {})
    # The preset's `captions` describes the base cut (word-by-word native captions). Translations
    # always default to the sentence engine; only an explicit --captions word falls back.
    pvals.pop("captions", None)
    pvals.pop("title_style", None)
    pvals.pop("title_secs", None)
    pvals.pop("thumb", None)
    ap = argparse.ArgumentParser(description="Render an existing clip in extra languages.")
    ap.add_argument("--preset", default="", help="editing preset from scripts/presets.json")
    ap.add_argument("job"); ap.add_argument("--pick", type=int, default=1)
    ap.add_argument("--langs", default="es,pt,de,ru")
    ap.add_argument("--voice", action="store_true")
    ap.add_argument("--dub", action="store_true",
                    help="add a full TTS narration of the translated captions over ducked original audio (stock voices only, never a cloned voice)")
    ap.add_argument("--captions", choices=["sentence", "word"], default="sentence",
                    help="sentence = whole-sentence captions (recommended for translated text); word = legacy spread")
    ap.add_argument("--layout", choices=["fill", "blur", "facecam", "react"])
    ap.add_argument("--cam"); ap.add_argument("--hook")
    ap.add_argument("--title", help="manual title text (used as-is, no translation)")
    ap.add_argument("--zoom", choices=["off", "face", "game", "both"], default="off")
    ap.add_argument("--allow-profanity", action="store_true")
    ap.add_argument("--cap-color", default="#FFFFFF", help="caption text colour, #RRGGBB")
    ap.add_argument("--cap-highlight", default="#FFE500", help="active-word highlight colour, #RRGGBB")
    ap.add_argument("--cap-outline", default="#000000", help="caption outline colour, #RRGGBB")
    ap.add_argument("--box-fill", default="#FFFFFF", help="title/hook/bubble box colour, #RRGGBB")
    ap.add_argument("--box-text", default="#000000", help="text colour on the title/hook/bubble box, #RRGGBB")
    if pvals:
        ap.set_defaults(**pvals)
    a = ap.parse_args()
    palette = {"cap": a.cap_color, "highlight": a.cap_highlight, "outline": a.cap_outline,
               "box_fill": a.box_fill, "box_text": a.box_text}
    d = Path(a.job); out = d / "edits"
    side = out / f"clip_{a.pick}.json"
    if not side.exists():
        sys.exit(f"{side} not found. Cut the clip first with cut.py --pick {a.pick}.")
    meta = load_json(side)
    cands = load_json(d / "candidates.json")
    set_orientation(meta.get("orientation", "vertical"))  # canvas follows the base cut
    s0, e0 = meta["start"], meta["end"]
    layout = a.layout or meta.get("layout", "blur")
    # "auto" means the base cut already resolved the webcam: reuse its saved fractions here.
    cam = (a.cam if a.cam and a.cam != "auto" else meta.get("cam")) or "0.70:0.68:0.30:0.32"
    side = 'left' if float(cam.split(':')[0]) + float(cam.split(':')[2]) / 2 < 0.5 else 'right'
    cap_x, cap_y = cap_pos(layout, cam_side=side)
    hook = a.hook if a.hook is not None else meta.get("hook", "")
    credit = f"Clip de twitch.tv/{meta.get('source_channel')}" if meta.get("source_channel") else ""
    words = [w for w in load_transcript(d)[1] if s0 <= w["s"] < e0 and w["w"]]
    langs = [x.strip() for x in a.langs.split(",") if x.strip()]
    if not langs:
        sys.exit("no languages requested (preset has no extras; pass --langs pt,en)")
    profanity = bool(a.allow_profanity or meta.get("allow_profanity"))
    cache_path = out / "translate_cache.json"
    cache = load_json(cache_path) if cache_path.exists() else {}  # reruns only re-request what failed
    caps = ["# Captions, one clip in several languages\n",
            f"Source: {meta.get('source_title') or ''} ({meta.get('source_channel') or ''})\n",
            "Translations are machine translated. Read each one before posting.\n"]
    for lang in langs:
        t = translator(lang)
        h = tr(t, hook, cache, lang=lang) if hook else ""
        events = translate_sentences(words, lang, s0, e0, cache)
        band = title = src_dims = gx = None
        if layout == "react":
            vs = [x for x in ffprobe(cands["source"], at=s0)["streams"] if x.get("codec_type") == "video"][0]
            src_dims = (int(vs["width"]), int(vs["height"]))
            gx = game_offset(cands["source"], s0, e0, src_dims, [float(x) for x in cam.split(":")])
            if a.title:
                title_text = a.title
            elif meta.get("title"):
                title_text = tr(t, meta["title"].strip(), cache, lang=lang)
            else:
                title_text = ""
            if title_text:  # same title treatment as cut.py, translated
                style, secs = meta.get("title_style", "box"), meta.get("title_secs", 0)
                title, band = react_title_band(style, title_text, secs)
        ass = out / f"clip_{a.pick}_{lang}.ass"
        if a.captions == "word":  # legacy spread: translated words do not match the spoken timing
            tw = translate_words(t, words, cache, lang=lang)
            ass.write_text(build_ass(tw, s0, e0, h, cap_y, title=title,
                                     profanity=profanity, palette=palette, cap_x=cap_x), encoding="utf-8")
        else:
            ass.write_text(build_ass_sentences(events, s0, cap_y, e0=e0, hook=h, title=title,
                                               profanity=profanity, palette=palette, cap_x=cap_x),
                           encoding="utf-8")
        mp4 = out / f"clip_{a.pick}_{lang}.mp4"
        mp3 = None
        if a.voice and h:
            mp3 = make_voice(lang, h, Path(tempfile.gettempdir()) / f"clip_{a.pick}_{lang}_hook.mp3")
        dubs = None
        if a.dub and events:
            dubs = make_dubs(lang, events, s0, out)
            if dubs:
                print(f"dub: {len(dubs)} falas TTS em '{lang}' sobre o áudio original abafado")
        render(cands["source"], s0, e0, layout, cam, ass, mp4, mp3, band=band, src_dims=src_dims, game_x=gx,
               zoom=a.zoom, orientation=meta.get("orientation", "vertical"), dubs=dubs,
               band_fill=ff_color(a.box_fill) if band else None)
        write_variant_meta(out, a.pick, lang, dict(meta, file=str(mp4), zoom=a.zoom,
                                                   lang_source=meta.get("lang_source") or load_transcript(d)[0],
                                                   sentences_cache=events if a.captions != "word" else []))
        caps.append(platform_block(lang, h, credit, meta.get("source_url")))
        print(mp4)
    (out / f"clip_{a.pick}_captions.md").write_text("\n".join(caps), encoding="utf-8")
    save_json(cache_path, cache)
    print(out / f"clip_{a.pick}_captions.md")


if __name__ == "__main__":
    main()

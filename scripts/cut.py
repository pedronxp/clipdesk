"""Cut one candidate into a finished clip.

Vertical (default): 1080x1920, 30 fps. --orientation horizontal composes natively on
1920x1080 (react: gameplay full-frame + corner cam; facecam: cam column beside the game).
Burned captions (slurs and swears masked), hook box in the first seconds, loudness normalised
to -14 LUFS. Writes jobs/<id>/edits/clip_<n>.mp4 + .json (metadata for check.py).

usage: python cut.py jobs/<id> --pick 1 [--layout fill|blur|facecam|react] [--hook "TEXT"]
                     [--cam auto|x:y:w:h] [--title "TEXT" --title-style box|outline --title-secs 0] [--captions word|bubble|none]
                     [--lang pt] [--translations-file events.json]

--lang renders the captions in that language, translated whole sentence: each spoken sentence
becomes one bubble caption filling the sentence's time window (no fake word sync). Equal to the
audio language (or omitted) keeps native word-by-word captions. --captions-lang is a deprecated
alias of --lang. The title and hook stay exactly as typed — write them in the target language
yourself. Proofread before posting. --translations-file re-renders cached sentence events offline.
"""
import argparse, math, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from common import load_json, load_presets, preset_values, run, save_json, tool

W, H = 1080, 1920
LAND = False  # horizontal canvas (1920x1080) when the clip orientation is 'horizontal'


def set_orientation(orientation):
    """Swap the editing canvas: 1080x1920 vertical (default) or 1920x1080 horizontal. Runs
    before any ASS/filter building — PlayRes, caption anchors and the layout graphs all read
    these globals."""
    global W, H, LAND
    LAND = orientation == "horizontal"
    W, H = (1920, 1080) if LAND else (1080, 1920)


def load_transcript(d):
    """(language, words) from a job dir; accepts the new dict transcript and the legacy bare list."""
    payload = load_json(Path(d) / "transcript.json")
    if isinstance(payload, list):
        return None, payload
    return payload.get("language"), payload.get("words") or []


def resolve_lang(args, lang_source):
    """Decide how captions are rendered: {"mode": word|sentence|none, "lang_target", "warnings"}.
    --captions none wins; --lang beats the deprecated --captions-lang alias (which is kept as a
    plain alias); --lang equal to the audio language keeps native word-by-word captions."""
    norm = {"pt-br": "pt", "pt_br": "pt", "ptbr": "pt"}
    raw = (args.lang or "").strip().lower()
    if raw in ("none", "off", "original"):
        raw = ""
    lang = norm.get(raw, raw)
    cap_lang = norm.get((args.captions_lang or "").strip().lower(), (args.captions_lang or "").strip().lower())
    warnings = []
    if cap_lang and lang and cap_lang != lang:
        warnings.append("--lang wins over the deprecated --captions-lang alias")
        cap_lang = ""
    lang = lang or cap_lang
    if args.captions == "none":
        return {"mode": "none", "lang_target": lang or lang_source, "warnings": warnings}
    if not lang:
        return {"mode": "word", "lang_target": lang_source, "warnings": warnings}
    if lang_source and lang == lang_source:
        warnings.append(f"captions already in audio language '{lang}'; word-by-word kept")
        return {"mode": "word", "lang_target": lang_source, "warnings": warnings}
    return {"mode": "sentence", "lang_target": lang, "warnings": warnings}


def log_hook(job_id, pick, lang_target, text, root=None):
    """Append one row to the repo log.md so hook-writer's no-repeat-in-7-days rule has real data."""
    root = Path(root) if root else Path(__file__).resolve().parent.parent
    f = root / "log.md"
    if not f.exists():
        f.write_text("| date | job | pick | lang | hook/title |\n|---|---|---|---|---|\n", encoding="utf-8")
    stamp = time.strftime("%Y-%m-%d %H:%M")
    with open(f, "a", encoding="utf-8") as fh:
        fh.write(f"| {stamp} | {job_id} | {pick} | {lang_target or '-'} | {(text or '').replace('|', '/')} |\n")


_CONJ = {"que", "e", "mas", "porque", "com", "quando", "para", "and", "but", "because", "so", "then", "when", "if"}

# Editing palette: every colour is a preset/CLI knob (--cap-color, --cap-highlight, --cap-outline,
# --box-fill, --box-text) with these values as the shipped defaults. Hook/TitleBox/Bubble use
# BorderStyle 3, where libass renders OutlineColour as the box fill — hence "box_fill" feeding it.
DEFAULT_PALETTE = {"cap": "#FFFFFF", "highlight": "#FFE500", "outline": "#000000",
                   "box_fill": "#FFFFFF", "box_text": "#000000"}


def _hex6(v):
    """'#RRGGBB' or 'RRGGBB' -> uppercase RGB hex; sys.exit()s with a helpful message otherwise."""
    s = (v or "").strip().lstrip("#")
    if len(s) != 6 or any(ch not in "0123456789abcdefABCDEF" for ch in s):
        sys.exit(f"invalid color '{v}' — use #RRGGBB (e.g. #FFE500)")
    return s.upper()


def ass_color(v):
    """RGB hex -> ASS colour &H00BBGGRR (ASS stores BGR, not RGB)."""
    s = _hex6(v)
    return "&H00" + s[4:6] + s[2:4] + s[0:2]


def ff_color(v):
    """RGB hex -> ffmpeg drawbox colour 0xRRGGBB."""
    return "0x" + _hex6(v)


def ass_hdr(cap="#FFFFFF", highlight="#FFE500", outline="#000000", box_fill="#FFFFFF", box_text="#000000"):
    """ASS header from the palette (#RRGGBB each)."""
    cap, hl, ol, fill, txt = (ass_color(v) for v in (cap, highlight, outline, box_fill, box_text))
    return f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,Arial Black,92,{cap},{hl},{ol},&H64000000,-1,0,0,0,100,100,0,0,1,7,3,5,60,60,0,1
Style: Hook,Arial Black,68,{txt},{txt},{fill},{fill},-1,0,0,0,100,100,0,0,3,18,0,8,70,70,170,1
Style: TitleBox,Arial Black,66,{txt},{txt},{fill},{fill},-1,0,0,0,100,100,0,0,1,0,0,5,40,40,0,1
Style: TitleOut,Arial Black,80,{cap},{cap},{ol},&H64000000,-1,0,0,0,100,100,0,0,1,9,2,8,50,50,0,1
Style: Bubble,Arial,58,{txt},{txt},{fill},{fill},-1,0,0,0,100,100,0,0,3,16,0,5,60,60,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def sentences(words):
    """Split clip words into sentences (end punctuation or a pause > 0.8 s)."""
    out, cur = [], []
    for w in words:
        if cur and w["s"] - cur[-1]["e"] > 0.8:
            out.append(cur); cur = []
        cur.append(w)
        if w["w"][-1:] in ".?!":
            out.append(cur); cur = []
    if cur:
        out.append(cur)
    return out


def translator(lang):
    try:
        from deep_translator import GoogleTranslator
    except ImportError:
        sys.exit("deep-translator missing. Run: pip install -r requirements.txt")
    return GoogleTranslator(source="auto", target=lang)


_MEM_LOCALE = {"es": "es-ES", "en": "en-GB", "pt": "pt-BR", "de": "de-DE", "ru": "ru-RU",
               "fr": "fr-FR", "it": "it-IT", "pl": "pl-PL", "tr": "tr-TR", "ja": "ja-JP",
               "ko": "ko-KR", "ar": "ar-SA", "hi": "hi-IN"}


def _mymemory(text, lang):
    """Fallback endpoint when Google blocks the IP (rate limits are per endpoint). MyMemory
    auto-detects the source language server-side, so a generic en source works."""
    from deep_translator import MyMemoryTranslator
    return MyMemoryTranslator(source="en-GB", target=_MEM_LOCALE.get(lang, lang)).translate(text)


def _cache_bucket(cache, lang):
    """Per-target-language namespace. The legacy flat cache (written before the multi-language
    work) holds PT-BR translations, so keep using it for pt and give every other target its
    own nested dict (otherwise an EN render would happily return the cached PT line)."""
    if cache is None:
        return None
    if not lang:
        return cache
    lang = (lang or "").lower()
    bucket = cache.get(lang)
    if isinstance(bucket, dict):
        return bucket
    flat = any(isinstance(k, str) and not isinstance(v, dict) for k, v in cache.items())
    if lang in ("pt", "pt-br", "pt_br", "ptbr") and flat:
        return cache
    return cache.setdefault(lang, {})


def tr(t, text, cache=None, lang=None):
    text = (text or "").strip()
    if not text:
        return ""
    bucket = _cache_bucket(cache, lang)
    if bucket is not None and text in bucket:
        return bucket[text]
    out, ok = None, False
    for attempt in range(4):  # Google allows ~5 req/s: pace the requests, back off hard on the limiter
        try:
            out = (t.translate(text) or text).strip()
            ok = True
            break
        except Exception as e:
            if attempt == 3:
                # network / rate limit: try a second endpoint (Google blocks whole IPs, MyMemory
                # is a different service), then keep the original rather than crash the batch
                if lang:
                    try:
                        out = (_mymemory(text, lang) or text).strip()
                        ok = True
                        print("note: google translate blocked, used the MyMemory fallback", file=sys.stderr)
                    except Exception as e2:
                        print(f"warning: translate failed ({e}; {e2}); keeping original text", file=sys.stderr)
                        out = text
                else:
                    print(f"warning: translate failed ({e}); keeping original text", file=sys.stderr)
                    out = text
            else:
                time.sleep(5)
    if bucket is not None and ok:  # never cache the untranslated fallback: a later run may recover
        bucket[text] = out
    time.sleep(0.5)
    return out


def translate_sentences(words, lang, s0=0.0, e0=None, cache=None):
    """Translate each spoken sentence as a whole (no word-by-word spread). Returns
    [{"text", "s", "e"}] events on the source timeline; falls back to the original
    text per sentence when translation fails."""
    inside = [w for w in words if s0 <= w["s"] < (e0 if e0 is not None else float("inf")) and w["w"]]
    try:
        t = translator(lang)
    except Exception as e:
        print(f"warning: translate failed ({e}); keeping original text", file=sys.stderr)
        t = None
    evs = []
    for sent in sentences(inside):
        txt = " ".join(w["w"] for w in sent)
        if t is not None:
            txt = tr(t, txt, cache, lang=lang)
        if txt:
            evs.append({"text": txt, "s": sent[0]["s"], "e": sent[-1]["e"]})
    return evs


def _split_event(ev, max_s=4.0, max_chars=48):
    """Split one sentence event into caption-sized pieces: each piece <= max_s seconds and
    short enough for a 2-line Bubble. Piece boundaries prefer commas/conjunctions; timing is
    proportional to piece length."""
    text = " ".join((ev["text"] or "").split())
    if not text or ev["e"] - ev["s"] <= 0.01:
        return []
    dur = ev["e"] - ev["s"]
    n = max(1, math.ceil(dur / max_s), math.ceil(len(text) / max_chars))
    words = text.split()
    if n == 1 or len(words) < 2:
        return [dict(ev, text=text)]
    n = min(n, len(words))
    bounds = []
    for i in range(1, n):
        ideal = max(1, round(i * len(words) / n))
        cand = range(max(1, ideal - 2), min(len(words) - 1, ideal + 2) + 1)
        best = min(cand, key=lambda j: (0 if (words[j - 1].rstrip().endswith((",", ";", ":"))
                                                or words[j - 1].lower().strip(",.;:!?") in _CONJ) else 1,
                                        abs(j - ideal)))
        bounds.append(best)
    bounds = sorted(set(bounds))
    cuts = [0] + bounds + [len(words)]
    chunks = [" ".join(words[cuts[i]:cuts[i + 1]]) for i in range(len(cuts) - 1) if cuts[i] < cuts[i + 1]]
    total = sum(len(c) for c in chunks) or 1
    out, acc = [], 0
    for i, c in enumerate(chunks):
        a = ev["s"] + dur * acc / total
        acc += len(c)
        b = ev["e"] if i == len(chunks) - 1 else ev["s"] + dur * acc / total
        out.append({"text": c, "s": a, "e": max(b, a + 0.05)})
    return out


def build_ass_sentences(events, s0, cap_y, e0=None, hook=None, title=None, profanity=False, palette=None, cap_x=None):
    """One Bubble caption per spoken sentence: the translated text is rendered as whole
    phrases, each filling the time window of the sentence it translates (no fake word sync)."""
    cx = cap_x if cap_x is not None else W // 2
    ev = []
    all_ev = []
    for e in events:
        if e.get("e", 0) > s0 and (e0 is None or e["s"] < e0):
            all_ev += _split_event({"text": e["text"], "s": max(e["s"], s0), "e": e["e"]})
    for e in all_ev:
        a = max(0.0, e["s"] - s0)
        b = e["e"] - s0
        if b <= a:
            continue
        body = "\\N".join(wrap(esc(clean(e["text"], profanity)), 24)[:3])
        ev.append(f"Dialogue: 0,{ts(a)},{ts(b)},Bubble,,0,0,0,,{{\\pos({cx},{cap_y})}}{body}")
    if title:  # reaction title: (style, text, y, seconds)
        st, text, ty, secs = title
        end = secs
        if secs <= 0:
            end = (e0 - s0) if e0 is not None else ((max((e["e"] for e in all_ev), default=s0) - s0) if all_ev else 0)
        body = '\\N'.join(wrap(esc(text), 26 if st == 'box' else 24))
        if st == 'box':
            ev.append(f"Dialogue: 2,{ts(0)},{ts(end)},TitleBox,,0,0,0,,{{\\pos({W // 2},{ty})}}{body}")
        else:
            ev.append(f"Dialogue: 2,{ts(0)},{ts(end)},TitleOut,,0,0,0,,{{\\pos({W // 2},{ty})\\an8}}{body}")
        hook = ''
    if hook:
        ev.append(f"Dialogue: 1,{ts(0)},{ts(2.6)},Hook,,0,0,0,,{{\\fad(0,180)\\fscx92\\fscy92\\t(0,120,\\fscx100\\fscy100)}}{esc(hook)}")
    return ass_hdr(**{**DEFAULT_PALETTE, **(palette or {})}) + "\n".join(ev) + "\n"


def ts(t):
    t = max(0, t); h = int(t // 3600); m = int(t % 3600 // 60); s = t % 60
    return f"{h}:{m:02d}:{s:05.2f}"


import re as _re
_SLURS = _re.compile(r"\b(nigg\w*|n[i1]gg?a\w*|f[a@]gg?\w*|retard\w*|tranny\w*|chink\w*|spic\w*|kike\w*)\b", _re.I)


_SWEARS = _re.compile(r"\b(fuck\w*|motherfuck\w*|shit\w*|bitch\w*|cunt\w*|dick\w*|pussy\w*|asshole\w*|bastard\w*|whore\w*|slut\w*)\b", _re.I)

# PT-BR: captions can be burned in Portuguese (--captions-lang, or a BR streamer's own
# transcript), so the English-only masks would let these through untouched. Spellings are
# chosen to not collide with English words ("puta" not "put", "viado" but not "viaduct").
_SLURS_PT = _re.compile(r"\b(viad(?:o|os|ao|ão|inho|inhos)\w*|bichas?|travecos?)\b", _re.I)

_SWEARS_PT = _re.compile(r"\b(porr\w*|caralh\w*|krl|kct|pqp|merd\w*|cacet\w*|bucet\w*|puta\w*|puto|fod\w*|fudid\w*|desgraç\w*|arrombad\w*)\b", _re.I)


def _mask(m):
    w = m.group(0)
    return w[0] + '*' * (len(w) - 1)


def clean(s, profanity=False):
    """Slurs are always masked (n-word -> n****); hard swears too unless profanity=True.
    English and PT-BR lists: burned captions may be in either language."""
    s = _SLURS.sub(_mask, s)
    s = _SLURS_PT.sub(_mask, s)
    if not profanity:
        s = _SWEARS.sub(_mask, s)
        s = _SWEARS_PT.sub(_mask, s)
    return s


def esc(s):
    return s.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ")


def wrap(text, width):
    lines, cur = [], ''
    for wd in text.split():
        if cur and len(cur) + 1 + len(wd) > width:
            lines.append(cur); cur = wd
        else:
            cur = (cur + ' ' + wd).strip()
    return lines + ([cur] if cur else [])


def build_ass(words, s0, e0, hook, cap_y, title=None, caps='word', profanity=False, palette=None, cap_x=None):
    p = {**DEFAULT_PALETTE, **(palette or {})}
    cx = cap_x if cap_x is not None else W // 2
    ev = []
    ws = [dict(w, w=clean(w["w"], profanity)) for w in words if s0 <= w["s"] < e0 and w["w"]]
    # groups of up to 3 words, break on punctuation or pauses
    groups, cur = [], []
    for w in ws:
        if cur and (len(cur) == 3 or w["s"] - cur[-1]["e"] > 0.6 or cur[-1]["w"][-1:] in ".?!,"):
            groups.append(cur); cur = []
        cur.append(w)
    if cur:
        groups.append(cur)
    hook_end = 2.6 if hook else 0
    for gi, g in enumerate(groups):
        g_end = groups[gi + 1][0]["s"] if gi + 1 < len(groups) else g[-1]["e"] + 0.4
        g_end = min(g_end, g[-1]["e"] + 0.8)
        for k, w in enumerate(g):
            a = w["s"] - s0; b = (g[k + 1]["s"] - s0) if k + 1 < len(g) else g_end - s0
            if b <= a:
                continue
            parts = []
            for j, x in enumerate(g):
                t = esc(x["w"].upper())
                parts.append("{\\c" + ass_color(p["highlight"]) + "&}" + t + "{\\c" + ass_color(p["cap"]) + "&}" if j == k else t)
            pop = "{\\fscx112\\fscy112\\t(0,90,\\fscx100\\fscy100)}" if k == 0 else ""
            if caps == 'bubble':
                if k == 0:
                    txt = esc(' '.join(x['w'] for x in g)).strip()
                    txt = txt[:1].upper() + txt[1:]
                    ev.append(f"Dialogue: 0,{ts(a)},{ts(g_end - s0)},Bubble,,0,0,0,,{{\\pos({cx},{cap_y})}}{pop}{txt}")
                continue
            ev.append(f"Dialogue: 0,{ts(a)},{ts(b)},Cap,,0,0,0,,{{\\pos({cx},{cap_y})}}{pop}{' '.join(parts)}")
    if title:  # reaction title: (style, text, y, seconds)
        st, text, ty, secs = title
        end = (e0 - s0) if secs <= 0 else secs
        lines = wrap(esc(text), 26 if st == 'box' else 24)
        body = '\\N'.join(lines)
        if st == 'box':
            ev.append(f"Dialogue: 2,{ts(0)},{ts(end)},TitleBox,,0,0,0,,{{\\pos({W // 2},{ty})}}{body}")
        else:
            ev.append(f"Dialogue: 2,{ts(0)},{ts(end)},TitleOut,,0,0,0,,{{\\pos({W // 2},{ty})\\an8}}{body}")
        hook = ''
    if hook:
        ev.append(f"Dialogue: 1,{ts(0)},{ts(hook_end)},Hook,,0,0,0,,{{\\fad(0,180)\\fscx92\\fscy92\\t(0,120,\\fscx100\\fscy100)}}{esc(hook)}")
    return ass_hdr(**p) + "\n".join(ev) + "\n"


CAP_Y = {"fill": 1380, "blur": 1460, "facecam": 1120, "react": 1560}
TOP = 730  # react layout: face panel height = seam y (even)
GAME_H = H - TOP  # 1190
# Horizontal canvas constants (land react/facecam compose natively on 1920x1080):
LAND_CAM_BOX = (500, 288)  # react: corner cam box (w, h), overlaid top-right over the game
LAND_FACECAM_W = 728  # facecam: cam column width = even(1920 * 0.38); game fills the rest


def even(n):
    return int(n) // 2 * 2


def cap_pos(layout, cam_side='right'):
    """(x, y) anchor of the burned captions on the current canvas. On the horizontal facecam
    the text stays centred on the game column so the cam panel never clips it."""
    if LAND:
        if layout == 'facecam':
            gw = W - LAND_FACECAM_W
            return (gw // 2 if cam_side == 'right' else W - gw // 2, 950)
        return W // 2, 950
    return W // 2, CAP_Y[layout]


def react_title_band(style, text, secs):
    """(title_tuple, band) for the react layout's --title. Vertical: band drawn on the
    cam/game seam. Horizontal: band strip along the top edge (the corner cam box overlays
    its right end). band=None for outline titles (text floats, no fill behind)."""
    n = len(wrap(text, 26 if style == 'box' else 24))
    if style == 'box':
        bh = n * 78 + 44
        if LAND:
            return ('box', text, 24 + bh // 2, secs), (24, bh, secs)
        return ('box', text, TOP, secs), (TOP - bh // 2, bh, secs)
    if LAND:
        return ('outline', text, 150, secs), None
    return ('outline', text, TOP + 40, secs), None


ZOOM_FACE, ZOOM_GAME = 1.18, 1.10


def zoom_filter(target, secs, w, h, fps=30):
    """Slow punch-in for one already-sized panel. d=1 keeps the clip length; the per-frame
    increment is computed here from the clip duration so the zoom always lands on `target`.
    fps + setpts wrap zoompan because it allocates against the input timeline: VFR live
    recordings with broken timestamps (segment resets) make it try to buffer the whole run
    and die with 'Cannot allocate memory'."""
    step = (target - 1.0) / max(1.0, float(secs or 1.0) * fps)
    return (f",fps={fps},zoompan=z='min(zoom+{step:.7f},{target:.4f})'"
            f":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s={w}x{h}"
            f",setpts=N/FRAME_RATE/TB")


def video_filter(layout, cam, ass, band=None, cam_side='right', src=None, game_x=None,
                 face_expr=None, zoom="off", zoom_secs=None, band_fill=None):
    """Layout + burned-caption filter graph ending in [vo]. Reused by translate.py.

    zoom: off | face (slow 1.18x punch-in on the cam panel) | game (subtle 1.10x) | both.
    On layouts with no separate cam panel, `face` zooms the whole frame at the face rate.
    On the horizontal canvas (set_orientation('horizontal')) every layout composes natively
    on 1920x1080 — never a pillarboxed vertical edit in the middle.
    """
    if LAND:
        return _video_filter_land(layout, cam, ass, band, cam_side, src, game_x, face_expr,
                                  (zoom or "off").lower(), zoom_secs, band_fill)
    zoom = (zoom or "off").lower()
    zc = zoom in ("face", "both")
    zg = zoom in ("game", "both")
    cam_h = int(H * 0.38)
    if layout == "fill":
        v = f"[0:v]scale=-2:{H},crop={W}:{H}"
        if zoom != "off":
            v += zoom_filter(ZOOM_FACE if zc else ZOOM_GAME, zoom_secs, W, H)
        v += "[v]"
    elif layout == "blur":
        v = (f"[0:v]split[a][b];[a]scale=-2:{H},crop={W}:{H},boxblur=24:2,eq=brightness=-0.08[bg];"
             f"[b]scale={W}:-2[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2-80")
        if zoom != "off":
            v += zoom_filter(ZOOM_FACE if zc else ZOOM_GAME, zoom_secs, W, H)
        v += "[v]"
    elif layout == 'react':
        sw, sh = src
        if face_expr:  # tracked face crop (no cam frame)
            cw, chh, ex, ey = face_expr
            top = f"crop={cw}:{chh}:{ex}:{ey}"
        else:
            x, y, w, h = map(float, cam.split(':'))
            # fraction expressions, not pixels: the decode size can differ from the stream header
            # and can even change mid-stream, so iw/ih keep the crop on the webcam whatever ffmpeg outputs
            top = f"crop=iw*{w:.5f}:ih*{h:.5f}:iw*{x:.5f}:ih*{y:.5f}"
        gw = even(sw * GAME_H / sh) if sw * GAME_H / sh >= W else W
        gx = game_x if game_x is not None else ((gw - W) if cam_side == 'left' else 0)
        cam_chain = f"{top},scale={W}:{TOP}:force_original_aspect_ratio=increase,crop={W}:{TOP},setsar=1"
        game_chain = f"scale={gw}:{GAME_H},crop={W}:{GAME_H}:{even(gx)}:0,setsar=1"
        if zc:
            cam_chain += zoom_filter(ZOOM_FACE, zoom_secs, W, TOP)
        if zg:
            game_chain += zoom_filter(ZOOM_GAME, zoom_secs, W, GAME_H)
        v = (f"[0:v]split[a][b];[a]{cam_chain}[cam];"
             f"[b]{game_chain}[game];[cam][game]vstack[v]")
        if band:
            y0, bh, secs = band
            en = f":enable='lte(t,{secs})'" if secs > 0 else ''
            v = v[:-3] + f"[v0];[v0]drawbox=x=0:y={y0}:w=iw:h={bh}:color={band_fill or 'white'}@1:t=fill{en}[v]"
    else:
        x, y, w, h = map(float, cam.split(":"))
        cam_chain = (f"crop=iw*{w}:ih*{h}:iw*{x}:ih*{y},scale={W}:{cam_h}:force_original_aspect_ratio=increase,"
                     f"crop={W}:{cam_h}")
        game_chain = f"scale=-2:{H - cam_h},crop={W}:{H - cam_h}"
        if zc:
            cam_chain += zoom_filter(ZOOM_FACE, zoom_secs, W, cam_h)
        if zg:
            game_chain += zoom_filter(ZOOM_GAME, zoom_secs, W, H - cam_h)
        v = f"[0:v]split[a][b];[a]{cam_chain}[cam];[b]{game_chain}[game];[cam][game]vstack[v]"
    sub = str(ass).replace("\\", "/").replace(":", "\\:")
    return v + f";[v]fps=30,subtitles='{sub}'[vo]"


def _video_filter_land(layout, cam, ass, band, cam_side, src, game_x, face_expr, zoom, zoom_secs, band_fill):
    """Horizontal 1920x1080 compositions. fill/blur fill the 16:9 frame from the source;
    react keeps the gameplay full-frame with the webcam as a corner box; facecam puts the
    cam in a side column beside the game."""
    zc = zoom in ("face", "both")
    zg = zoom in ("game", "both")
    if layout == "fill":
        v = f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1"
        if zoom != "off":
            v += zoom_filter(ZOOM_FACE if zc else ZOOM_GAME, zoom_secs, W, H)
        v += "[v]"
    elif layout == "blur":
        v = (f"[0:v]split[a][b];[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
             f"boxblur=24:2,eq=brightness=-0.08[bg];"
             f"[b]scale={W}:{H}:force_original_aspect_ratio=decrease[fg];"
             f"[bg][fg]overlay=(W-w)/2:(H-h)/2")
        if zoom != "off":
            v += zoom_filter(ZOOM_FACE if zc else ZOOM_GAME, zoom_secs, W, H)
        v += "[v]"
    elif layout == 'react':
        cw, chh = LAND_CAM_BOX
        if face_expr:  # tracked face crop (no cam frame), sized for the corner box
            cw, chh, ex, ey = face_expr
            top = f"crop={cw}:{chh}:{ex}:{ey}"
        else:
            x, y, w, h = map(float, cam.split(':'))
            # fraction expressions, not pixels: the decode size can differ from the stream header
            top = f"crop=iw*{w:.5f}:ih*{h:.5f}:iw*{x:.5f}:ih*{y:.5f}"
        sw, sh = src
        gw = even(sw * H / sh) if sw * H / sh >= W else W
        gx = game_x if game_x is not None else ((gw - W) if cam_side == 'left' else 0)
        cam_chain = f"{top},scale={cw}:{chh}:force_original_aspect_ratio=increase,crop={cw}:{chh},setsar=1"
        game_chain = f"scale={gw}:{H},crop={W}:{H}:{even(gx)}:0,setsar=1"
        if zc:
            cam_chain += zoom_filter(ZOOM_FACE, zoom_secs, cw, chh)
        if zg:
            game_chain += zoom_filter(ZOOM_GAME, zoom_secs, W, H)
        if band:  # title strip along the top edge, behind the cam box
            y0, bh, secs = band
            en = f":enable='lte(t,{secs})'" if secs > 0 else ''
            game_chain += f",drawbox=x=0:y={y0}:w=iw:h={bh}:color={band_fill or 'white'}@1:t=fill{en}"
        v = (f"[0:v]split[a][b];[a]{cam_chain}[cam];"
             f"[b]{game_chain}[game];[game][cam]overlay={W - cw - 24}:24[v]")
    else:  # facecam: cam column on its detected side, game fills the rest
        cam_w = LAND_FACECAM_W
        game_w = W - cam_w
        x, y, w, h = map(float, cam.split(':'))
        cam_chain = (f"crop=iw*{w}:ih*{h}:iw*{x}:ih*{y},scale={cam_w}:{H}:force_original_aspect_ratio=increase,"
                     f"crop={cam_w}:{H},setsar=1")
        game_chain = f"scale={game_w}:{H}:force_original_aspect_ratio=increase,crop={game_w}:{H},setsar=1"
        if zc:
            cam_chain += zoom_filter(ZOOM_FACE, zoom_secs, cam_w, H)
        if zg:
            game_chain += zoom_filter(ZOOM_GAME, zoom_secs, game_w, H)
        pair = "[cam][game]" if cam_side == 'left' else "[game][cam]"
        v = f"[0:v]split[a][b];[a]{cam_chain}[cam];[b]{game_chain}[game];{pair}hstack[v]"
    sub = str(ass).replace("\\", "/").replace(":", "\\:")
    return v + f";[v]fps=30,subtitles='{sub}'[vo]"


def game_offset(video, s0, e0, src, cam_box, w=None, h=None):
    """Pick the x offset of the game crop that avoids static overlays (viewer counters,
    alerts, chat boxes) and the webcam. Static = pixels that barely change across sampled
    frames but carry edges (text/graphics). Defaults follow the current canvas: full height
    on the horizontal 16:9, the GAME_H panel on the vertical edit."""
    try:
        import cv2, numpy as np
    except ImportError:
        return None
    if w is None:
        w = W
    if h is None:
        h = H if LAND else GAME_H
    sw, sh = src
    gw = even(sw * h / sh)
    if gw <= w:
        return 0
    cap = cv2.VideoCapture(str(video)); frames = []
    for k in range(12):
        cap.set(cv2.CAP_PROP_POS_MSEC, (s0 + (e0 - s0) * (k + 0.5) / 12) * 1000)
        ok, f = cap.read()
        if ok:
            frames.append(cv2.resize(cv2.cvtColor(f, cv2.COLOR_BGR2GRAY), (gw, h)).astype(np.float32))
    cap.release()
    if len(frames) < 4:
        return None
    st = np.stack(frames)
    std = st.std(0); med = np.median(st, 0)
    edges = cv2.Canny(med.astype(np.uint8), 60, 160) > 0
    bad = (std < 6) & cv2.dilate(edges.astype(np.uint8), np.ones((9, 9), np.uint8)).astype(bool)
    x, y, w, h = cam_box  # the cam itself also counts as overlay
    bad[int(y * h):int((y + h) * h), int(x * gw):int((x + w) * gw)] = True
    col = bad.sum(0).astype(np.float64); cs = np.concatenate([[0], np.cumsum(col)])
    best, bx = None, 0
    camcols = np.zeros(gw); camcols[int(x * gw):int((x + w) * gw)] = 1; cc = np.concatenate([[0], np.cumsum(camcols)])
    for ox in range(0, gw - w + 1, 8):
        # never show the cam twice; otherwise stay near the centre (game subtitles and action sit there),
        # moving off-centre only when that clearly removes a static overlay (counters, alerts, chat)
        if cc[ox + w] - cc[ox] > 0 and (cc[-1] < gw - w):
            continue
        cost = (cs[ox + w] - cs[ox]) * 0.2 + abs(ox - (gw - w) / 2) * h * 0.05
        if best is None or cost < best:
            best, bx = cost, ox
    return bx


def main():
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--preset", default="")
    pre_a, _ = pre.parse_known_args()
    _pname, pvals = preset_values(pre_a.preset) if pre_a.preset else (None, {})
    ap = argparse.ArgumentParser(description="Cut one candidate into a finished vertical or horizontal clip.")
    ap.add_argument("--preset", default="", help="editing preset from scripts/presets.json (e.g. react-br)")
    ap.add_argument("job"); ap.add_argument("--pick", type=int, default=1)
    ap.add_argument("--layout", choices=["fill", "blur", "facecam", "react"], default="blur")
    ap.add_argument("--orientation", choices=["vertical", "horizontal"], default="vertical")
    ap.add_argument("--title", default="", help="reaction title, e.g. 'Speed REALIZED why GTA 6 took 13 YEARS'")
    ap.add_argument("--title-style", choices=["box", "outline"], default="box")
    ap.add_argument("--title-secs", type=float, default=0, help="0 = whole clip")
    ap.add_argument("--captions", choices=["word", "bubble", "none", "sentence"], default="word")
    ap.add_argument("--lang", default="",
                    help="render the captions in this language, translated whole-sentence (e.g. pt); native word-by-word when equal to the audio language")
    ap.add_argument("--captions-lang", default="",
                    help="deprecated alias of --lang (whole-sentence captions now)")
    ap.add_argument("--translations-file", default="", help="JSON with cached sentence events [{text,s,e}] to render without network")
    ap.add_argument("--hook", default="")
    ap.add_argument("--allow-profanity", action="store_true", help="keep swear words in captions (slurs are always masked)")
    ap.add_argument("--cam", default="auto", help="auto (find the webcam) or x:y:w:h fractions")
    ap.add_argument("--zoom", choices=["off", "face", "game", "both"], default="off",
                    help="slow punch-in: face on the cam panel (1.18x), game on the gameplay (1.10x), or both")
    ap.add_argument("--start", type=float); ap.add_argument("--end", type=float)
    ap.add_argument("--cap-color", default="#FFFFFF", help="caption text colour, #RRGGBB")
    ap.add_argument("--cap-highlight", default="#FFE500", help="active-word highlight colour, #RRGGBB")
    ap.add_argument("--cap-outline", default="#000000", help="caption outline colour, #RRGGBB")
    ap.add_argument("--box-fill", default="#FFFFFF", help="title/hook/bubble box colour, #RRGGBB")
    ap.add_argument("--box-text", default="#000000", help="text colour on the title/hook/bubble box, #RRGGBB")
    if pvals:
        ap.set_defaults(**pvals)
    a = ap.parse_args()
    set_orientation(a.orientation)  # canvas globals before any ASS/filter building
    palette = {"cap": a.cap_color, "highlight": a.cap_highlight, "outline": a.cap_outline,
               "box_fill": a.box_fill, "box_text": a.box_text}
    d = Path(a.job); c = load_json(d / "candidates.json")
    pick = next(x for x in c["candidates"] if x["n"] == a.pick)
    s0 = a.start if a.start is not None else pick["start"]; e0 = a.end if a.end is not None else pick["end"]
    _lang_source, words = load_transcript(d)
    out = d / "edits"; out.mkdir(exist_ok=True)
    cache_path = out / "translate_cache.json"
    cache = load_json(cache_path) if cache_path.exists() else {}
    layout, cam, side = a.layout, a.cam, 'right'
    face_expr = None
    if layout in ('react', 'facecam') and cam == 'auto':
        from facecam import find_cam
        r = find_cam(c['source'], s0, e0)
        if layout == 'react' and r.get('mode') not in ('cam', 'face'):
            sys.exit(f"skipped: no streamer face over gameplay in this moment ({r.get('mode')}: {r.get('reason', '')}). "
                     "React clips need the game on screen with the cam as a separate box. Pick another moment or stream.")
        if layout == 'react':
            from facecam import has_gameplay, face_crop_expr
            if not has_gameplay(c['source'], s0, e0, r['box']):
                sys.exit("skipped: the area outside the webcam is not gameplay (same scene or no game on screen). "
                         "Pick a moment where GTA 6 is on screen.")
            if r.get('mode') == 'face' and r.get('face_track'):
                from common import ffprobe as _fp
                _vs = [x for x in _fp(c['source'], at=s0)['streams'] if x.get('codec_type') == 'video'][0]
                from facecam import inner_edges
                sw_, sh_ = int(_vs['width']), int(_vs['height'])
                bnd = inner_edges(c['source'], s0, e0, r['face'], sw_, sh_)
                print(f'facecam: cam borders L{bnd[0]} T{bnd[1]} R{bnd[2]} B{bnd[3]} px')
                panel_w, panel_h = LAND_CAM_BOX if LAND else (W, TOP)
                fe = face_crop_expr(r['face_track'], sw_, sh_, panel_w, panel_h, bounds=bnd)
                face_expr = (fe['w'], fe['h'], fe['x'], fe['y'])
                print(f"facecam: no clean cam frame, tracking the face ({r.get('summary', '')})")
        if r.get('mode') in ('cam', 'face'):
            bx = list(r['box'])
            if r['mode'] == 'face':  # no webcam frame: widen the face crop so it is not a blurry close-up
                cx, cy = bx[0] + bx[2] / 2, bx[1] + bx[3] / 2
                bw, bh = min(1.0, bx[2] * 2.6), min(1.0, bx[3] * 2.2)
                bx = [min(max(0.0, cx - bw / 2), 1 - bw), min(max(0.0, cy - bh / 2), 1 - bh), bw, bh]
            cam = ':'.join(f'{v:.4f}' for v in bx)
            side = 'left' if r['box'][0] + r['box'][2] / 2 < 0.5 else 'right'
            print(f"facecam: {r.get('mode')} box {cam} ({r.get('summary', '')})")
        else:
            print(f"facecam: no stable face found ({r.get('reason', '')}), using the blur layout")
            layout = 'blur'
    elif layout in ('react', 'facecam') and cam != 'auto':
        side = 'left' if float(cam.split(':')[0]) + float(cam.split(':')[2]) / 2 < 0.5 else 'right'
    cap_x, cap_y = cap_pos(layout, cam_side=side)
    title, band = None, None
    if a.title and layout == 'react':
        title, band = react_title_band(a.title_style, a.title, a.title_secs)
    ass = out / f"clip_{a.pick}.ass"
    res = resolve_lang(a, _lang_source)
    for w in res["warnings"]:
        print(f"note: {w}", file=sys.stderr)
    sentences_cache = []
    if res["mode"] == "none":
        ass.write_text(build_ass([], s0, e0, a.hook, cap_y, title=title, caps='word', profanity=a.allow_profanity,
                                 palette=palette, cap_x=cap_x), encoding="utf-8")
    elif res["mode"] == "sentence":
        if a.translations_file:
            events = load_json(a.translations_file)
            print(f"captions: {len(events)} sentence events loaded from cache ({res['lang_target']})")
        else:
            events = translate_sentences(words, res["lang_target"], s0, e0, cache=cache)
            save_json(cache_path, cache)
            print(f"captions: {len(events)} sentences machine-translated to '{res['lang_target']}' — proofread before posting")
        sentences_cache = events
        ass.write_text(build_ass_sentences(events, s0, cap_y, e0=e0, hook=a.hook, title=title,
                                           profanity=a.allow_profanity, palette=palette, cap_x=cap_x), encoding="utf-8")
    else:
        if a.captions == "sentence":  # no --lang: native-language sentence bubbles
            events = [{"text": " ".join(w["w"] for w in sent), "s": sent[0]["s"], "e": sent[-1]["e"]}
                      for sent in sentences([w for w in words if s0 <= w["s"] < e0 and w["w"]])]
            sentences_cache = events
            ass.write_text(build_ass_sentences(events, s0, cap_y, e0=e0, hook=a.hook, title=title,
                                               profanity=a.allow_profanity, palette=palette, cap_x=cap_x),
                           encoding="utf-8")
        else:
            ass.write_text(build_ass(words, s0, e0, a.hook, cap_y, title=title, caps=a.captions,
                                     profanity=a.allow_profanity, palette=palette, cap_x=cap_x),
                           encoding="utf-8")
    src = None; gx = None
    if layout == 'react':
        from common import ffprobe
        vs = [x for x in ffprobe(c['source'], at=s0)['streams'] if x.get('codec_type') == 'video'][0]
        src = (int(vs['width']), int(vs['height']))
        gx = game_offset(c['source'], s0, e0, src, [float(t) for t in cam.split(':')])
    v = video_filter(layout, cam, ass, band=band, cam_side=side, src=src, game_x=gx, face_expr=face_expr,
                     zoom=a.zoom, zoom_secs=e0 - s0, band_fill=ff_color(a.box_fill) if band else None)
    mp4 = out / f"clip_{a.pick}.mp4"
    run([tool("ffmpeg"), "-y", "-v", "error", "-ss", f"{s0:.2f}", "-to", f"{e0:.2f}", "-i", c["source"],
         "-filter_complex", v, "-map", "[vo]", "-map", "0:a?", "-af", "loudnorm=I=-14:TP=-1.5:LRA=11",
         # VFR segment-boundary PTS can stretch the decoded window (zoompan then emits extra frames):
         # cap the output to the intended window so video and audio always end together.
         "-t", f"{e0 - s0:.2f}",
         "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k",
         "-ar", "48000", "-movflags", "+faststart", str(mp4)])
    info = c.get("info", {})
    save_json(out / f"clip_{a.pick}.json", {"file": str(mp4), "start": s0, "end": e0, "layout": layout, "hook": a.hook or a.title, "title": a.title, "cam": cam,
                                            "title_style": a.title_style, "title_secs": a.title_secs, "zoom": a.zoom,
                                            "orientation": a.orientation, "palette": palette,
                                            "lang_source": _lang_source, "lang_target": res["lang_target"],
                                            "captions_mode": res["mode"], "captions_style": a.captions,
                                            "allow_profanity": bool(a.allow_profanity),
                                            "sentences_cache": sentences_cache,
                                            "source_url": info.get("webpage_url"), "source_title": info.get("title"),
                                            "source_channel": info.get("channel") or info.get("uploader")})
    log_hook(d.name, a.pick, res["lang_target"], a.hook or a.title)
    print(mp4)


if __name__ == "__main__":
    main()

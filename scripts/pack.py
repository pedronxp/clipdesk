"""Thumbnail + clean export for a finished clip.

Reads jobs/<id>/edits/clip_<n>.mp4 and its .json sidecar, grabs a frame before the first
caption appears, burns the same title band used by cut.py (Arial Black, clip palette) and writes
thumb.jpg at 1080x1920. --export copies only the three post-ready files (video, thumb,
legendas.md) to out/<slug>/; every intermediate stays in jobs/ (gitignored).

usage: python pack.py jobs/<id> --pick 5 [--export out/<slug>/|off] [--thumb on|off]
                          [--title "TEXT"] [--credit "@streamer"]
"""
import argparse, re, shutil, sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from common import ffprobe, load_json, run, tool
from cut import TOP, W, H, clean, esc, ff_color, wrap

# Arial Black is the face used by the ASS title styles. Fall back to other Windows faces and
# finally to ffmpeg's default when the font is not installed.
FONT_CANDIDATES = [
    Path(r"C:\Windows\Fonts\ariblk.ttf"),
    Path(r"C:\Windows\Fonts\arialbd.ttf"),
    Path(r"C:\Windows\Fonts\arial.ttf"),
]
TITLE_SIZE = 66
LINE_H = 78
BAND_PAD = 44
HASHTAGS = ["#GTA6", "#GTAVI", "#gaming", "#rockstargames"]


def _ff_path(p):
    """Escape a path for an ffmpeg filter option value (Windows drive colon included)."""
    return str(Path(p).as_posix()).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")


def find_font():
    for f in FONT_CANDIDATES:
        if f.exists():
            return f
    return None


def first_caption_sec(meta, words, start):
    """Absolute source time of the first burned caption, or None."""
    cache = meta.get("sentences_cache") or []
    starts = [float(e.get("s", 0)) for e in cache if e.get("s") is not None]
    if not starts:
        starts = [float(w.get("s", 0)) for w in (words or []) if w.get("s") is not None and w.get("w")]
    starts = [s for s in starts if s >= start]
    return min(starts) if starts else None


def thumb_time(meta, words, start, end):
    """A frame in the clean window before the first caption (0.05..0.8 s)."""
    first = first_caption_sec(meta, words, start)
    clean = (first - start) if first is not None else (end - start)
    if clean <= 0.1:
        return 0.05
    return max(0.05, min(0.8, clean * 0.5))


def title_band(title, max_lines=None):
    lines = wrap(esc(title or ""), 26)
    if not lines:
        return None
    if max_lines:
        lines = lines[:max_lines]
    # same band math as cut.py's TitleBox: every line fits the band the video itself draws
    bh = len(lines) * LINE_H + BAND_PAD
    return bh, lines


# post images per platform: (width, height, band center y, safe-zone clamp)
PLATFORMS = {
    "tiktok": (W, H, TOP, None),
    "reels": (W, H, TOP, (300, 1300)),  # keep the band inside the 4:5 grid crop and off the bottom UI
    "youtube": (1280, 720, H // 2, None),  # 16:9 cover-crop of the frame, band centered
}


def draw_band_filter(title, height=H, band_center=TOP, safe=None, fill="#FFFFFF", text="#000000"):
    """Title band + the title, one drawtext per line. fill/text follow the clip's palette
    (the same box_fill/box_text the video itself burned), white/black when unspecified.

    ffmpeg's drawtext reports text_h for a multi-line textfile in a way that does not match
    the rendered block, so the lines drift out of the band. Drawing each line separately
    keeps every line inside the box.
    """
    max_lines = None
    if safe:  # never let the band leave the platform's safe zone
        band_center = min(max(band_center, safe[0] + 200), safe[1] - 200)
    band = title_band(title, max_lines=max_lines)
    if not band:
        return ""
    bh, lines = band
    y0 = min(max(0, band_center - bh // 2), max(0, height - bh))
    gap = 10
    total = len(lines) * TITLE_SIZE + (len(lines) - 1) * gap
    y = y0 + max(0, (bh - total) // 2)
    font = find_font()
    font_opt = f":fontfile='{_ff_path(font)}'" if font else ""
    out = f",drawbox=x=0:y={y0}:w=iw:h={bh}:color={ff_color(fill)}@1:t=fill"
    for i, line in enumerate(lines):
        tmp = Path(tempfile.gettempdir()) / f"gtavi_thumb_title_{i}.txt"
        tmp.write_text(line, encoding="utf-8")
        out += (f",drawtext=textfile='{_ff_path(tmp)}'{font_opt}:fontsize={TITLE_SIZE}"
                f":fontcolor={ff_color(text)}:x=(w-text_w)/2:y={y}")
        y += TITLE_SIZE + gap
    return out


def make_thumb(clip, meta, out, title, words=None, w=W, h=H, band_center=TOP, safe=None,
               fill="#FFFFFF", text="#000000"):
    start = float(meta.get("start") or 0)
    end = float(meta.get("end") or (start + 1))
    t = thumb_time(meta, words, start, end)
    if (w, h) == (W, H):
        vf = f"scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2"
    else:  # a different aspect (YouTube 16:9): cover-crop keeps the face/game seam in frame
        vf = f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}"
    vf += draw_band_filter(title, height=h, band_center=band_center, safe=safe, fill=fill, text=text)
    run([tool("ffmpeg"), "-y", "-v", "error", "-ss", f"{t:.3f}", "-i", str(clip),
         "-frames:v", "1", "-vf", vf, "-q:v", "2", str(out)])
    return out, t


def legendas(meta, title, credit=""):
    """PT-BR posting text: title, credit, source and per-platform caption blocks."""
    line = title or meta.get("title") or meta.get("hook") or "GTA 6"
    src = meta.get("source_url") or ""
    channel = credit or meta.get("source_channel") or meta.get("source_title") or ""
    cred = f"Crédito: {channel}" if channel else ""
    tags = " ".join(HASHTAGS)
    x = "\n".join(p for p in [line, cred, src] if p)
    ig = "\n".join(p for p in [line, cred, tags, src] if p)
    tk = "\n".join(p for p in [line, cred, " ".join(HASHTAGS[:3])] if p)
    return (f"# {line}\n\n**X** (sem hashtags)\n\n{x}\n\n"
            f"**Instagram** (4 hashtags)\n\n{ig}\n\n"
            f"**TikTok / YouTube Shorts**\n\n{tk}\n")


def export_clip(dest, mp4, thumb, legendas_path, platform_thumbs=()):
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copy2(mp4, dest / "clip.mp4")
    if thumb:
        shutil.copy2(thumb, dest / "thumb.jpg")
    shutil.copy2(legendas_path, dest / "legendas.md")
    for p, src in platform_thumbs:
        shutil.copy2(src, dest / f"thumb_{p}.jpg")
    return dest


def main():
    ap = argparse.ArgumentParser(description="Make a thumbnail and a clean post-ready export.")
    ap.add_argument("job")
    ap.add_argument("--pick", type=int, default=1)
    ap.add_argument("--title", default="", help="title to burn on the thumb (defaults to clip metadata)")
    ap.add_argument("--credit", default="", help="streamer/channel name for legendas.md")
    ap.add_argument("--thumb", choices=["on", "off"], default="on")
    ap.add_argument("--platforms", default="",
                    help="comma list of post images to render: tiktok,reels,youtube "
                         "(writes thumb_<n>_<platform>.jpg next to the clip)")
    ap.add_argument("--export", default="", metavar="DIR|off",
                    help="copy only clip.mp4, thumb.jpg and legendas.md to DIR; 'off' disables")
    a = ap.parse_args()

    d = Path(a.job)
    out = d / "edits"
    mp4 = out / f"clip_{a.pick}.mp4"
    if not mp4.exists():
        sys.exit(f"{mp4} not found. Cut the clip first with cut.py --pick {a.pick}.")
    side = out / f"clip_{a.pick}.json"
    meta = load_json(side) if side.exists() else {}
    title = a.title or meta.get("title") or meta.get("hook") or ""
    # the thumb band must match the band burned in the video: same palette saved by cut.py
    pal = meta.get("palette") or {}
    band_fill, band_text = pal.get("box_fill", "#FFFFFF"), pal.get("box_text", "#000000")

    words = []
    try:
        from cut import load_transcript
        _lang, words = load_transcript(d)
    except Exception:
        pass

    thumb = out / f"thumb_{a.pick}.jpg"
    if a.thumb == "on":
        make_thumb(mp4, meta, thumb, title, words, fill=band_fill, text=band_text)
        print(thumb)
    else:
        thumb = None

    platform_thumbs = []
    plats = [p.strip().lower() for p in a.platforms.split(",") if p.strip()]
    bad = [p for p in plats if p not in PLATFORMS]
    if bad:
        sys.exit(f"plataforma desconhecida: {', '.join(bad)} (use: {', '.join(PLATFORMS)})")
    # a react clip carries its title band burned at the seam: the 16:9 cover-crop keeps that
    # strip, so the youtube band must sit exactly on it instead of floating elsewhere
    seam = None
    if meta.get("layout") == "react":
        scale = max(1280 / W, 720 / H)
        seam = round(TOP * scale - (round(H * scale) - 720) // 2)
    for p in plats:
        w, h, center, safe = PLATFORMS[p]
        band_at = seam if (p == "youtube" and seam is not None) else center
        img = out / f"thumb_{a.pick}_{p}.jpg"
        make_thumb(mp4, meta, img, title, words, w=w, h=h, band_center=band_at, safe=safe,
                   fill=band_fill, text=band_text)
        platform_thumbs.append((p, img))
        print(img)

    leg = out / f"clip_{a.pick}_legendas.md"
    leg.write_text(legendas(meta, title, a.credit), encoding="utf-8")
    print(leg)

    if a.export and a.export.lower() not in ("off", "none", ""):
        if thumb is None and not platform_thumbs:
            # export always carries a thumbnail: generate it even when --thumb off only
            # silenced the standalone print
            thumb = out / f"thumb_{a.pick}.jpg"
            make_thumb(mp4, meta, thumb, title, words, fill=band_fill, text=band_text)
        dest = export_clip(a.export, mp4, thumb, leg, platform_thumbs)
        print(dest)


if __name__ == "__main__":
    main()

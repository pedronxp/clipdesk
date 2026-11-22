"""Find live streams about a topic, ranked by concurrent viewers.

usage: python streams.py --query "gta 6" --limit 20 --out streams.md
YouTube always. Twitch always (Helix if TWITCH_CLIENT_ID and TWITCH_TOKEN are set, else keyless public GQL). Kick is tried once, skipped silently on failure.
Viewer counts are exactly what the platform returned at run time.
"""
import argparse, json, os, sys, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import quote_plus
sys.path.insert(0, str(Path(__file__).parent))
from common import ytdlp_base, ytdlp_json, run

try:
    import truststore
    truststore.inject_into_ssl()
except Exception:
    pass


BOT_CHECK = "Sign in to confirm"
NOTE_COOKIES = ("bot check do YouTube: lives listadas sem os contadores de espectadores — exporte os cookies "
                "de uma conta logada uma única vez: python scripts/export_cookies.py (passo a passo em PRODUCAO.md)")


def fmt_dur(sec):
    if not sec or sec < 0:
        return ""
    sec = int(sec)
    days, sec = divmod(sec, 86400)
    hours, sec = divmod(sec, 3600)
    minutes = sec // 60
    return f"{days}d {hours:02d}h{minutes:02d}m" if days else f"{hours}h{minutes:02d}m"


def flat_row(e):
    return {"platform": "YouTube", "channel": e.get("channel") or e.get("uploader") or "",
            "title": e.get("title") or "", "url": e.get("url") or f"https://www.youtube.com/watch?v={e['id']}",
            "viewers": None, "live_for": "", "thumbnail": ""}


def youtube(query, limit):
    """(rows, note) — note explains a degraded section instead of leaving it silently empty."""
    url = f"https://www.youtube.com/results?search_query={quote_plus(query)}&sp=EgJAAQ%253D%253D&gl=BR&hl=pt-BR"
    r = run(ytdlp_base() + ["--flat-playlist", "-J", "--playlist-end", str(limit), url], capture=True, check=False)
    if r.returncode != 0 or not r.stdout.strip():
        err = r.stderr or ""
        print(f"youtube search failed: {err[-300:]}", file=sys.stderr)
        if BOT_CHECK in err:
            return [], NOTE_COOKIES
        return [], f"busca do YouTube falhou: {err.strip().splitlines()[-1][:200] if err.strip() else 'sem saída'}"
    entries = [e for e in json.loads(r.stdout).get("entries") or [] if e and e.get("id")]
    ids = [e.get("url") or f"https://www.youtube.com/watch?v={e['id']}" for e in entries]

    first_err = ""
    def one(u):
        nonlocal first_err
        try:
            return ytdlp_json(u)
        except SystemExit as exc:  # run() calls sys.exit on failure; keep the first reason
            first_err = first_err or str(exc)
            return None
        except BaseException:
            return None

    with ThreadPoolExecutor(max_workers=8) as ex:
        infos = list(ex.map(one, ids))
    now = time.time(); out = []
    for i in infos:
        if not i or not i.get("is_live"):
            continue
        start = i.get("release_timestamp") or i.get("timestamp")
        out.append({"platform": "YouTube", "channel": i.get("channel") or i.get("uploader") or "",
                    "title": i.get("title") or "", "url": i.get("webpage_url") or "",
                    "viewers": i.get("concurrent_view_count"), "live_for": fmt_dur(now - start) if start else "",
                    "pt": pt_flag({"title": i.get("title") or "", "language": i.get("language")}),
                    "thumbnail": thumbnail(i)})
    if not out and BOT_CHECK in first_err:
        # The search already answered; the per-video bot check only blocks viewer counts.
        # List the flat entries so the section stays useful, and say exactly why it is degraded.
        print(f"youtube bot check: {first_err.strip().splitlines()[0][:200]}", file=sys.stderr)
        return [flat_row(e) for e in entries if e.get("live_status") == "is_live"], NOTE_COOKIES
    return out, ""


def thumbnail(info):
    images = info.get("thumbnails") or []
    return info.get("thumbnail") or (images[-1].get("url") if images else "")


def timestamp(value):
    if not value:
        return None
    try:
        import datetime as dt
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


def twitch_nokey(game="Grand Theft Auto VI", limit=20):
    """No account/API key: Twitch public web GQL (see twitch.py)."""
    import twitch as tw
    try:
        rows = tw.top_streams(game, max(limit, 100))  # wide fetch: pt_sort promotes PT-BR from beyond the top viewers
    except Exception as e:
        print(f"twitch (no key) failed: {e}", file=sys.stderr)
        return []
    now = time.time()
    return [{"platform": "Twitch", "channel": r["login"], "title": r["title"], "url": r["url"], "viewers": r["viewers"],
             "pt": pt_flag({"title": r.get("title") or "", "language": r.get("language")}),
             "live_for": fmt_dur(now - tw.started_ts(r["created_at"])) if tw.started_ts(r["created_at"]) else "",
             "thumbnail": r.get("preview") or ""} for r in rows]


def twitch(query):
    cid, tok = os.environ.get("TWITCH_CLIENT_ID"), os.environ.get("TWITCH_TOKEN")
    if not (cid and tok):
        return twitch_nokey()
    import requests
    h = {"Client-Id": cid, "Authorization": f"Bearer {tok}"}
    try:
        g = requests.get("https://api.twitch.tv/helix/games", params={"name": "Grand Theft Auto VI"}, headers=h, timeout=15).json()
        if not g.get("data"):
            return []
        gid = g["data"][0]["id"]
        s = requests.get("https://api.twitch.tv/helix/streams", params={"game_id": gid, "first": 100, "language": "pt"}, headers=h, timeout=15).json()
    except Exception as e:
        print(f"twitch failed: {e}", file=sys.stderr)
        return []
    now = time.time(); out = []
    for x in s.get("data", []):
        try:
            st = time.mktime(time.strptime(x["started_at"], "%Y-%m-%dT%H:%M:%SZ")) - time.timezone
        except Exception:
            st = None
        out.append({"platform": "Twitch", "channel": x.get("user_name", ""), "title": x.get("title", ""), "pt": True,
                    "url": f"https://twitch.tv/{x.get('user_login', '')}", "viewers": x.get("viewer_count"),
                    "live_for": fmt_dur(now - st) if st else "",
                    "thumbnail": (x.get("thumbnail_url") or "").replace("{width}", "640").replace("{height}", "360")})
    return out


PT_MARKERS = ("pt-br", "ptbr", "pt br", "português", "portugues", "gameplay pt", "dublado")


def pt_flag(row):
    """PT-BR detection: metadata language first, then title markers (fuzzy on purpose)."""
    lang = str(row.get("language") or "").lower()
    if lang.startswith("pt"):  # Twitch GQL returns uppercase ("PT"); metadata may be "pt-BR"
        return True
    title = (row.get("title") or "").lower()
    return any(m in title for m in PT_MARKERS)


def pt_sort(rows):
    """PT-BR streamers first, then by viewers (spec: foca nas PT-BR, 2026-11-21)."""
    return sorted(rows, key=lambda r: (0 if r.get("pt") else 1, -(r.get("viewers") or -1)))

def kick(query):
    """Kick's public API sits behind Cloudflare and has been returning 404 to any
    automated client since 2026-11 — one try, then say honestly that it is blocked."""
    try:
        import requests
        r = requests.get("https://kick.com/api/v2/channels", params={"query": query}, timeout=8,
                         headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code != 200:
            return [], ("A Kick bloqueia acesso automatizado no momento (busca devolveu "
                        f"HTTP {r.status_code}). Cole o link da live da Kick direto no campo acima.")
        data = r.json()
        items = data if isinstance(data, list) else data.get("data", [])
        now = time.time()
        rows = []
        for c in items:
            if not isinstance(c, dict):
                continue
            live = c.get("livestream") or {}
            if not live or live.get("is_live") is False:
                continue
            thumb = live.get("thumbnail") or c.get("thumbnail") or c.get("profile_pic") or ""
            if isinstance(thumb, dict):
                thumb = thumb.get("url") or thumb.get("src") or ""
            started = timestamp(live.get("created_at") or live.get("start_time"))
            rows.append({"platform": "Kick", "channel": c.get("slug", ""),
                         "title": live.get("session_title") or "", "url": f"https://kick.com/{c.get('slug', '')}",
                         "viewers": live.get("viewer_count"),
                         "live_for": fmt_dur(now - started) if started else "", "thumbnail": thumb})
        return rows, ""
    except Exception:
        return [], ("A Kick bloqueia acesso automatizado no momento. "
                    "Cole o link da live da Kick direto no campo acima.")


def main():
    # Windows consoles still default to cp1252 and stream titles carry emojis: never let a
    # print blow up the run (the .md file must be written even when the console cannot show it).
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--query", default="gta 6"); ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--out", default="streams.md")
    a = ap.parse_args()
    yt_rows, yt_note = youtube(a.query, a.limit)
    sections = [("YouTube", yt_rows, yt_note)]
    t = twitch(a.query)
    if t:
        t = pt_sort(t)[:a.limit]  # sort PT-BR first, THEN cut — never cut a PT streamer out
    if t is not None:
        sections.append(("Twitch", t, ""))
    k_rows, k_note = kick(a.query)
    sections.append(("Kick", k_rows, k_note))
    stamp = time.strftime("%Y-%m-%d %H:%M")
    md = [f"# Live streams: {a.query}", f"Checked {stamp}. Viewer counts are as returned at that moment.", ""]
    for name, rows, note in sections:
        rows = pt_sort(rows)
        md += [f"## {name}", ""]
        if note:
            md += [f"> ⚠️ {note}", ""]
        if not rows:
            md += ["No live streams found.", ""]
            continue
        md += ["| # | Channel | Title | Viewers | Live for | URL |", "|---|---|---|---|---|---|"]
        for n, r in enumerate(rows, 1):
            v = "" if r["viewers"] is None else r["viewers"]
            md.append(f"| {n} | {r['channel']} | {r['title'].replace('|', '/')} | {v} | {r['live_for']} | {r['url']} |")
        md.append("")
    Path(a.out).write_text("\n".join(md), encoding="utf-8")
    Path(a.out).with_suffix(".json").write_text(json.dumps({
        "query": a.query, "checked": stamp,
        "sections": [{"platform": name, "note": note,
                      "rows": pt_sort(rows)}
                     for name, rows, note in sections]}, ensure_ascii=False, indent=2), encoding="utf-8")
    for name, rows, _note in sections:
        for r in sorted(rows, key=lambda r: r["viewers"] or -1, reverse=True):
            v = "" if r["viewers"] is None else r["viewers"]
            print(f"{name:8} {str(v):>7}  {r['channel'][:24]:24} {r['title'][:50]:50} {r['url']}")
    print(f"wrote {a.out}")


if __name__ == "__main__":
    main()

"""Coherent titles for every take, in the target language.

Reads jobs/<id>/candidates.json, picks the strongest spoken sentence inside each take
(hype words, exclamation, length) and builds 3 title options from what was actually said —
never an invented quote or opinion — then machine-translates the fragments and writes
titles.md + title_suggestions back into candidates.json.

usage: python titles.py jobs/<id> [--langs pt] [--limit 80]
"""
import argparse, json, re, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from common import load_json, save_json
from cut import clean, tr, translator  # tr never caches failures, so reruns retry

try:
    from moments import HYPE  # same hype vocabulary used to rank the moments
except Exception:
    HYPE = re.compile(r"\b(no way|oh my god|omg|what|wait|bro|let'?s go|holy|insane|crazy|yo+|wtf|dude|look at|nah|run|go go)\b", re.I)

CONNECTORS = {  # neutral bridges: they add no claim, only framing
    "pt": ("O momento em que ", " — olha o que aconteceu"),
    "en": ("The moment when ", " — watch what happened"),
    "es": ("El momento en que ", " — mira lo que pasó"),
}


def sentences_of(text):
    parts = re.split(r"(?<=[.!?])\s+", " ".join((text or "").split()))
    return [p.strip() for p in parts if len(p.strip()) >= 12]


def sentence_score(s):
    """Long enough to carry context, short enough for a title, hype or excitement wins."""
    n = len(s)
    score = min(n, 110) / 110
    if HYPE.search(s):
        score += 0.6
    if re.search(r"[!?]", s):
        score += 0.3
    if n < 20 or n > 140:
        score -= 1.0
    return score


def key_fragment(s, limit):
    """The first sentence-like chunk, cleaned, within the title length budget."""
    s = clean(" ".join(s.split()), profanity=False)
    chunk = re.split(r"(?<=[.!?])\s+", s)[0] if s else ""
    chunk = chunk.rstrip(" .,;:-—")
    if len(chunk) > limit:
        cut_at = chunk.rfind(" ", 0, limit)
        chunk = chunk[:cut_at if cut_at > limit * 0.6 else limit].rstrip(" ,;:-") + "…"
    return chunk


def build_titles(text, channel, lang, limit, t, cache):
    """3 options: the direct fact, a curiosity frame, the channel frame. Everything comes
    from the take's own speech (cleaned + masked) plus neutral connectors."""
    sents = sorted(sentences_of(text), key=sentence_score, reverse=True)
    if not sents:
        sents = [" ".join((text or "").split())]
    frag = key_fragment(sents[0], limit)
    if not frag:
        return []
    direct = tr(t, frag, cache, lang=lang) if t else frag
    out = [direct[:limit]]
    conn_in, conn_out = CONNECTORS.get(lang, CONNECTORS["en"])
    tail = key_fragment(sents[1], limit) if len(sents) > 1 else frag
    tail_tr = tr(t, tail, cache, lang=lang) if t and tail != frag else tail
    if t and tail_tr == tail:
        return out[:1]  # the tail did not translate: a mixed-language frame is worse than fewer options
    body = ((tail_tr or tail) or "").rstrip(".!? ")
    body = body[:1].lower() + body[1:]  # the bridge reads as one clause, so lowercase it
    out.append(f"{conn_in}{body[:limit - len(conn_in)]}{conn_out}")
    if channel:
        out.append(f"{direct[:limit - 4]} — {channel}")
    return [x.strip(" -—") for x in dict.fromkeys(o for o in out if o and len(o) >= 8)][:3]


def main():
    ap = argparse.ArgumentParser(description="Draft coherent titles for every take.")
    ap.add_argument("job")
    ap.add_argument("--langs", default="pt", help="title language (default pt)")
    ap.add_argument("--limit", type=int, default=80, help="max title length")
    a = ap.parse_args()
    d = Path(a.job)
    cands_path = d / "candidates.json"
    if not cands_path.exists():
        sys.exit(f"{cands_path} not found. Run moments.py first.")
    data = load_json(cands_path)
    info = data.get("info") or {}
    channel = str(info.get("channel") or info.get("uploader") or "").lstrip("@")
    lang = a.langs.strip().lower()
    cache_path = d / "edits" / "translate_cache.json"
    cache_path.parent.mkdir(parents=True, exist_ok=True)  # a fresh job has no edits/ yet
    cache = load_json(cache_path) if cache_path.exists() else {}
    try:
        t = translator(lang)
    except SystemExit:
        raise
    md = [f"# Títulos sugeridos — {info.get('title') or d.name}",
          f"Derivados da própria fala de cada take (nada inventado), traduzidos para '{lang}'.",
          "Revise antes de postar: tradução de máquina pode escorregar.", ""]
    for c in data.get("candidates", []):
        opts = build_titles(c.get("text", ""), channel, lang, a.limit, t, cache)
        c["title_suggestions"] = opts
        tier = f" [{c['tier']}]" if c.get("tier") else ""
        md.append(f"## Take {c['n']}{tier} ({c['start']:.0f}s → {c['end']:.0f}s)")
        for i, o in enumerate(opts, 1):
            md.append(f"{i}. {o}")
        md.append("")
    save_json(cache_path, cache)
    save_json(cands_path, data)
    out = d / "titles.md"
    out.write_text("\n".join(md), encoding="utf-8")
    print(out)


if __name__ == "__main__":
    main()

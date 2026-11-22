"""Re-rank a job's candidate moments with an LLM and score them for virality.

Idea reimplemented from supoclip (never its code): our heuristics find candidate windows with
high recall (loudness spikes, hype words, chat reactions, "most replayed" heat); the LLM adds
precision — it reads each candidate's transcript, scores hook / engagement / value /
shareability (0-25 each, total recomputed here as the plain sum, never trusted from the model),
labels the hook type and writes a PT-BR hook title. Results are stored back into
candidates.json as candidates[n].llm plus a top-level llm_summary, and the pipeline
(monitor.py, analyze_creation.py, dashboard) reorders picks by that total when present.

Boundaries (skill-clips idea): each candidate also carries a timestamped transcript of the
window ±15 s, and the model may return adj_start/adj_end to move an edge (start 1-2 s before the
hook line, end right after the payoff). Refinements are clamped hard before touching
candidates.json: each edge ±15 s, 0.5x–(dur+45 s) length, and the original window is kept in
signal_start/signal_end.

LLM access: any OpenAI-compatible chat endpoint. Configure it in the dashboard (Ajustes tab,
dashboard/state/settings.json) or via env GTAVI_LLM_BASE/KEY/MODEL. The base URL must include
the version path (e.g. https://api.openai.com/v1, https://open.bigmodel.cn/api/paas/v4).

usage: python rank_llm.py <job> [--top 5] [--max 12] [--dry-run]
Exit codes: 0 ranked (or nothing to do), 3 LLM not configured (callers skip silently),
1 request/parse failure (candidates.json untouched).
"""
import argparse, json, re, sys, time, urllib.error, urllib.request
from datetime import datetime, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
import common
import performance
import spike_feedback

HOOK_TYPES = ("pergunta", "afirmação", "estatística", "história", "contraste", "nenhum")
MAX_CANDIDATES_SENT = 12

SYSTEM = """You are a senior short-form editor picking the most viral moments from a gaming stream's transcript.

You receive pre-cut candidate windows chosen by signal analysis (audio spikes, hype words, chat
reactions, "most replayed" heat). Their signal scores are hints only. Judge every candidate as a
standalone clip.

CORE RULES
- Judge only what is inside each candidate. Never invent facts. Never merge or shift windows.
- Bad picks: intros, sponsor reads, CTAs, questions with no payoff, contextless quotes.
- Good picks: a complete mini-story (setup, tension, payoff), a strong emotional beat, surprising info.

SCORING (integers 0-25 per dimension; be demanding — 20+ only for truly viral material)
- hook_score: does the first second grab attention (surprising claim, bold question, tension)?
- engagement_score: entertaining, emotional, dramatic?
- value_score: actionable insight, unique knowledge, or a memorable moment?
- shareability_score: "I need to send this to someone"?

HOOK
- hook_type: exactly one of "pergunta", "afirmação", "estatística", "história", "contraste", "nenhum".
- hook_title: 3-9 words in Brazilian Portuguese, grounded in what is actually said (translate when the
  speech is not PT-BR), no hashtags, no emojis, no quotes, no clickbait that betrays the clip.

BOUNDARIES
- Candidates come with a timestamped transcript of the window plus 15 s of slack on each side.
  When the transcript shows a better place to cut, return "adj_start"/"adj_end" ("mm:ss", or null
  to keep that edge): start 1-2 s before the hook line, never mid-word; end right after the
  reaction or payoff. Trim dead air, greetings and sponsor reads. When the window is already
  tight, return null for both.

OUTPUT: JSON only, exactly:
{"candidates": [{"id": "<echo the candidate id>", "hook_score": 0, "engagement_score": 0, "value_score": 0,
  "shareability_score": 0, "hook_type": "<type>", "hook_title": "<3-9 words>", "reasoning": "<one short sentence>",
  "adj_start": null, "adj_end": null}],
  "summary": "<one sentence about this source>"}
Score every listed candidate. Do not add candidates."""


def mmss(t):
    t = max(0, int(t or 0))
    return f"{t // 60}:{t % 60:02d}"


def clamp(v, lo, hi):
    try:
        v = int(round(float(v)))
    except (TypeError, ValueError):
        return lo
    return max(lo, min(hi, v))


def clean_hook(title):
    title = re.sub(r'[#*"“”‘’\u201c\u201d]', "", str(title or "")).strip()
    words = title.split()
    if len(words) > 10:
        words = words[:10]
    title = " ".join(words)[:64].strip()
    return title


def to_secs(v):
    """'h:mm:ss' | 'mm:ss' | seconds -> float seconds, else None (model-suggested edges)."""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    parts = str(v).strip().split(":")
    try:
        vals = [float(p.strip().replace(",", ".")) for p in parts]
    except ValueError:
        return None
    if not vals or any(v < 0 for v in vals):
        return None
    return sum(v * 60 ** i for i, v in enumerate(reversed(vals)))


def refine_window(c, adj_s, adj_e):
    """(start, end) tightened by the model, or None to keep the signal window. Each edge may move
    ±15 s around the signal window (5 s tolerance before rejecting a confused proposal outright);
    the result must keep at least 15 s and 0.5x-(dur+45 s) of length (duration tiers included)."""
    s0, e0 = float(c.get("start") or 0), float(c.get("end") or 0)
    dur = e0 - s0
    if dur <= 0 or (adj_s is None and adj_e is None):
        return None
    for v, lo, hi in ((adj_s, s0 - 15, e0 - 15), (adj_e, s0 + 15, e0 + 15)):
        if v is not None and not lo - 5 <= v <= hi + 5:
            return None  # hallucinated clock / wrong units: keep the signal window
    s = s0 if adj_s is None else max(s0 - 15, min(adj_s, e0 - 15))
    e = e0 if adj_e is None else max(s0 + 15, min(adj_e, e0 + 15))
    if e - s < max(15.0, 0.5 * dur) or e - s > min(dur + 45.0, 180.0):
        return None
    return round(s, 2), round(e, 2)


def stamp_lines(words, s0, e0, max_lines=30):
    '''"[mm:ss] text" per spoken sentence inside [s0, e0] — real boundaries for the model to aim at.'''
    inside = [w for w in words if w.get("w") and s0 <= w.get("s", 0) < e0]
    sents, cur = [], []
    for w in inside:
        if cur and (w["s"] - cur[-1]["e"] > 0.8 or cur[-1]["w"][-1:] in ".?!"):
            sents.append(cur)
            cur = []
        cur.append(w)
    if cur:
        sents.append(cur)
    lines = []
    for sent in sents[:max_lines]:
        text = re.sub(r"\s+", " ", " ".join(x["w"] for x in sent)).strip()
        if text:
            lines.append(f"  [{mmss(sent[0]['s'])}] {text[:110]}")
    return lines


def build_user_prompt(cands, info, perf_ctx, fb_ctx=None, words=None):
    src = str((info or {}).get("title") or "GTA 6 stream")
    chan = str((info or {}).get("channel") or (info or {}).get("uploader") or "")
    lines = [f"Source: {src}" + (f" — {chan}" if chan else ""), ""]
    if perf_ctx:
        lines += [perf_ctx, ""]
    if fb_ctx:
        lines += [fb_ctx, ""]
    lines.append("Candidates (id | window | signal score | signal reasons | spoken text):")
    seen_peaks = set()
    for c in cands:
        text = re.sub(r"\s+", " ", str(c.get("text") or "")).strip()
        dur = int(float(c.get("end") or 0) - float(c.get("start") or 0))
        why = ", ".join(str(w) for w in (c.get("why") or [])[:4])
        lines.append(f'{c["_cid"]} | {mmss(c.get("start"))}-{mmss(c.get("end"))} ({dur}s) | '
                     f'{c.get("score") or 0:g} | {why} | "{text[:220]}"')
        # duration tiers share a peak, so one transcript per moment: the first take carries it
        peak = c.get("peak")
        fresh = peak is None or peak not in seen_peaks
        if peak is not None:
            seen_peaks.add(peak)
        if words and fresh:
            lines += stamp_lines(words, float(c.get("start") or 0) - 15, float(c.get("end") or 0) + 15)
        elif not fresh:
            lines.append("  (same moment as another take above — its transcript applies)")
    lines.append("")
    lines.append("Rank these candidates for short-form virality. Return the JSON contract now.")
    return "\n".join(lines)


def call_llm(cfg, user_prompt, timeout=120):
    body = json.dumps({"model": cfg["model"], "temperature": 0.3, "max_tokens": 2000,
                       "messages": [{"role": "system", "content": SYSTEM},
                                    {"role": "user", "content": user_prompt}]}).encode("utf-8")
    req = urllib.request.Request(cfg["base"] + "/chat/completions", data=body, method="POST",
                                 headers={"Content-Type": "application/json",
                                          "Authorization": f"Bearer {cfg['key']}"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return str(((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "")


def _lenient_json(raw):
    """Second chance at the JSON small models emit: // line comments and trailing commas out."""
    t = re.sub(r"//[^\n]*", "", raw)
    t = re.sub(r",\s*([\]}])", r"\1", t)
    return json.loads(t)


def parse_reply(text):
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("no JSON object in the reply")
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:  # small endpoints fence, comment and leave trailing commas
        data = _lenient_json(m.group(0))
    rows = data.get("candidates")
    if not isinstance(rows, list):
        raise ValueError("reply has no candidates list")
    return rows, str(data.get("summary") or "")


def main():
    ap = argparse.ArgumentParser(description="LLM re-rank of a job's candidate moments.")
    ap.add_argument("job")
    ap.add_argument("--top", type=int, default=5, help="how many candidates end up marked as selected")
    ap.add_argument("--max", type=int, default=MAX_CANDIDATES_SENT, help="max candidates sent to the model")
    ap.add_argument("--dry-run", action="store_true", help="print the prompt and exit without calling the LLM")
    a = ap.parse_args()

    d = Path(a.job)
    cj = d / "candidates.json"
    if not cj.exists():
        print(f"no candidates.json in {d}", file=sys.stderr)
        return 0
    data = common.load_json(cj)
    cands = [c for c in (data.get("candidates") or []) if c.get("n") is not None]
    if not cands:
        print("no candidates to rank", file=sys.stderr)
        return 0

    ordered = sorted(cands, key=lambda c: float(c.get("score") or 0), reverse=True)[: max(1, min(a.max, MAX_CANDIDATES_SENT))]
    for i, c in enumerate(ordered, 1):
        c["_cid"] = f"c{i}"

    cfg = common.llm_config()
    if cfg is None and not a.dry_run:
        print("LLM not configured (dashboard Ajustes or env GTAVI_LLM_BASE/KEY/MODEL); skipping re-rank", file=sys.stderr)
        return 3
    perf_ctx = performance.context()
    fb_ctx = spike_feedback.context()  # operator's fp/ok verdicts on spikes (spec D5)
    tr = common.load_json(d / "transcript.json") if (d / "transcript.json").exists() else None
    words = (tr or {}).get("words") if isinstance(tr, dict) else tr  # legacy transcripts were a bare list
    user = build_user_prompt(ordered, data.get("info"), perf_ctx, fb_ctx, words=words)
    if a.dry_run:
        print("=== system ===\n" + SYSTEM + "\n=== user ===\n" + user)
        return 0

    rows, summary = [], ""
    last_err = None
    for attempt in (1, 2):
        try:
            rows, summary = parse_reply(call_llm(cfg, user))
            break
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError, KeyError, json.JSONDecodeError) as e:
            last_err = e
            print(f"attempt {attempt} failed: {e}", file=sys.stderr)
            time.sleep(2)
    if not rows:
        print(f"re-rank failed after retries: {last_err}", file=sys.stderr)
        return 1

    by_cid = {c["_cid"]: c for c in ordered}
    scored = {}
    for row in rows:
        c = by_cid.get(str(row.get("id")))
        if not c:
            continue
        sub = {k: clamp(row.get(k), 0, 25) for k in ("hook_score", "engagement_score", "value_score", "shareability_score")}
        htype = str(row.get("hook_type") or "").strip().lower()
        if htype not in HOOK_TYPES:
            htype = "nenhum"
        scored[c["n"]] = {"hook_score": sub["hook_score"], "engagement_score": sub["engagement_score"],
                          "value_score": sub["value_score"], "shareability_score": sub["shareability_score"],
                          "total_score": sum(sub.values()),  # plain sum, recomputed here (supoclip idea)
                          "hook_type": htype, "hook_title": clean_hook(row.get("hook_title")),
                          "reasoning": str(row.get("reasoning") or "")[:200],
                          "_adj": (to_secs(row.get("adj_start")), to_secs(row.get("adj_end")))}
    if not scored:
        print("model returned no valid candidate ids; candidates.json untouched", file=sys.stderr)
        return 1

    selected = sorted(scored, key=lambda n: scored[n]["total_score"], reverse=True)[: max(1, a.top)]
    refined = 0
    for c in data.get("candidates") or []:
        c.pop("_cid", None)
        sc = scored.get(c["n"])
        if not sc:
            continue
        adj_s, adj_e = sc.pop("_adj")
        c["llm"] = sc
        win = refine_window(c, adj_s, adj_e)
        if win:
            c["signal_start"], c["signal_end"] = c["start"], c["end"]
            c["start"], c["end"] = win
            refined += 1
    data["llm_summary"] = {"model": cfg["model"], "ranked_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
                           "selected": selected, "windows_refined": refined, "summary": summary[:300]}
    common.save_json(cj, data)
    print(f"re-rank: {len(scored)} candidates scored (model {cfg['model']}), selected {selected}"
          + (f", {refined} windows refined" if refined else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())

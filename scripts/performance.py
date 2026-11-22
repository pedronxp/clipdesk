"""Performance feedback loop: real metrics from posted clips steer the next ranking.

Reimplements supoclip's loop idea with our own storage: every clip registered here keeps its
selection snapshot (hook type, LLM total score, duration) plus the audience metrics typed into
the dashboard (or via CLI). Aggregates (median views per hook type, duration buckets, top posts)
feed both the dashboard's Desempenho tab and the LLM re-rank prompt as a tie-breaker context —
active only once MIN_POSTS posts have real numbers, so early runs stay signal-driven.

usage:
  python performance.py add --job <id> --n 2 --platform tiktok [--title T] [--url U]
                            [--hook-type pergunta] [--total-score 88] [--dur 28.4]
                            [--views 12000 --likes 900 --comments 40 --shares 120]
  python performance.py list
  python performance.py context
"""
import argparse, json, sys, uuid
from datetime import datetime, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from common import ROOT, load_json

STORE = ROOT / "dashboard" / "state" / "performance.json"
PLATFORMS = ("tiktok", "reels", "shorts", "x", "youtube")
HOOK_TYPES = ("pergunta", "afirmação", "estatística", "história", "contraste", "nenhum")
DURATION_BUCKETS = (("<20s", 0.0, 20.0), ("20-35s", 20.0, 35.0), ("35-50s", 35.0, 50.0), (">50s", 50.0, float("inf")))
MIN_POSTS = 3
CONTEXT_MAX_CHARS = 2000


def load_rows():
    try:
        rows = load_json(STORE)
    except Exception:
        return []
    return rows if isinstance(rows, list) else []


def save_rows(rows):
    STORE.parent.mkdir(parents=True, exist_ok=True)
    STORE.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def add(job, n, platform, title="", url="", hook_type="", total_score=None, dur=None,
        posted_at="", metrics=None):
    """Register (or replace, same job+n+platform) one posted clip and its metrics snapshot."""
    rows = load_rows()
    row = {"id": uuid.uuid4().hex[:8], "job": str(job), "n": int(n), "platform": platform,
           "title": str(title or "")[:120], "url": str(url or ""), "hook_type": hook_type if hook_type in HOOK_TYPES else "",
           "total_score": None if total_score is None else float(total_score),
           "dur": None if dur is None else float(dur),
           "posted_at": posted_at or _now(), "metrics": _metrics(metrics), "updated_at": _now()}
    rows = [r for r in rows if not (r.get("job") == row["job"] and r.get("n") == row["n"] and r.get("platform") == platform)]
    rows.append(row)
    save_rows(rows)
    return row


def _metrics(m):
    m = m or {}
    out = {}
    for k in ("views", "likes", "comments", "shares"):
        try:
            out[k] = max(0, int(float(m.get(k) or 0)))
        except (TypeError, ValueError):
            out[k] = 0
    return out


def update_metrics(post_id, metrics):
    rows = load_rows()
    for r in rows:
        if r.get("id") == post_id:
            r["metrics"] = _metrics(metrics)
            r["updated_at"] = _now()
            save_rows(rows)
            return r
    return None


def delete(post_id):
    rows = load_rows()
    keep = [r for r in rows if r.get("id") != post_id]
    save_rows(keep)
    return len(keep) != len(rows)


def _median(values):
    vs = sorted(values)
    if not vs:
        return 0
    return vs[len(vs) // 2] if len(vs) % 2 else (vs[len(vs) // 2 - 1] + vs[len(vs) // 2]) / 2


def _engagement(row):
    m = row.get("metrics") or {}
    views = int(m.get("views") or 0)
    if views <= 0:
        return None
    return (int(m.get("likes") or 0) + int(m.get("comments") or 0) + int(m.get("shares") or 0)) / views


def aggregate(rows=None):
    """Dashboard-shaped summary: totals, per hook type, duration buckets, top posts."""
    rows = load_rows() if rows is None else rows
    measured = [r for r in rows if (r.get("metrics") or {}).get("views")]
    engs = [e for e in (_engagement(r) for r in measured) if e is not None]

    by_hook = []
    for htype in HOOK_TYPES:
        grp = [r for r in measured if (r.get("hook_type") or "nenhum") == htype]
        views = [int(r["metrics"].get("views") or 0) for r in grp]
        if not grp:
            continue
        engs_h = [e for e in (_engagement(r) for r in grp) if e is not None]
        by_hook.append({"hook_type": htype, "posts": len(grp), "median_views": int(_median(views)),
                        "engagement": round(sum(engs_h) / len(engs_h) * 100, 1) if engs_h else None,
                        "avg_score": round(sum(float(r.get("total_score") or 0) for r in grp) / len(grp), 1)})
    by_hook.sort(key=lambda x: x["median_views"], reverse=True)

    by_dur = []
    for label, lo, hi in DURATION_BUCKETS:
        grp = [r for r in measured if r.get("dur") is not None and lo <= float(r["dur"]) < hi]
        views = [int(r["metrics"].get("views") or 0) for r in grp]
        if grp:
            by_dur.append({"bucket": label, "posts": len(grp), "median_views": int(_median(views))})
    by_dur.sort(key=lambda x: x["median_views"], reverse=True)

    top = sorted(measured, key=lambda r: int((r.get("metrics") or {}).get("views") or 0), reverse=True)[:10]
    return {"posts": len(rows), "measured": len(measured),
            "total_views": sum(int((r.get("metrics") or {}).get("views") or 0) for r in measured),
            "engagement": round(sum(engs) / len(engs) * 100, 1) if engs else None,
            "by_hook_type": by_hook, "by_duration": by_dur,
            "top": [{"id": r.get("id"), "title": r.get("title"), "platform": r.get("platform"),
                     "hook_type": r.get("hook_type"), "views": int((r.get("metrics") or {}).get("views") or 0),
                     "url": r.get("url")} for r in top]}


def _fmt(v):
    return f"{v / 1000:.1f}k" if v >= 1000 else str(v)


def context():
    """Tie-breaker context for the LLM re-rank prompt, or None before MIN_POSTS measured posts."""
    agg = aggregate()
    if agg["measured"] < MIN_POSTS or not agg["by_hook_type"]:
        return None
    lines = ["Histórico de performance do criador (dados reais de clipes já publicados; use apenas "
             "para desempatar e enviesar o tipo de hook e a duração — nunca para inventar conteúdo):"]
    lines.append("Por tipo de hook (melhor primeiro): " + " | ".join(
        f"{h['hook_type']}: {h['posts']} clipes, {_fmt(h['median_views'])} views mediana"
        + (f", {h['engagement']:.1f}% engajamento".replace(".", ",") if h["engagement"] is not None else "")
        for h in agg["by_hook_type"][:5]))
    if agg["by_duration"]:
        lines.append("Por duração (melhor primeiro): " + " | ".join(
            f"{b['bucket']}: {b['posts']} clipes, {_fmt(b['median_views'])} views mediana" for b in agg["by_duration"]))
    if agg["top"]:
        lines.append("Melhores clipes: " + " | ".join(
            f'"{t["title"] or "sem título"}" ({t["hook_type"] or "nenhum"}, {t["platform"]}, {_fmt(t["views"])} views)'
            for t in agg["top"][:5]))
    out = "\n".join(lines)
    return out[:CONTEXT_MAX_CHARS]


def main():
    ap = argparse.ArgumentParser(description="Posted-clip performance store and aggregates.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    ap_add = sub.add_parser("add")
    ap_add.add_argument("--job", required=True); ap_add.add_argument("--n", type=int, required=True)
    ap_add.add_argument("--platform", required=True, choices=PLATFORMS)
    ap_add.add_argument("--title", default=""); ap_add.add_argument("--url", default="")
    ap_add.add_argument("--hook-type", default="", choices=HOOK_TYPES)
    ap_add.add_argument("--total-score", type=float, default=None)
    ap_add.add_argument("--dur", type=float, default=None)
    ap_add.add_argument("--views", type=int, default=0); ap_add.add_argument("--likes", type=int, default=0)
    ap_add.add_argument("--comments", type=int, default=0); ap_add.add_argument("--shares", type=int, default=0)
    sub.add_parser("list")
    sub.add_parser("context")
    a = ap.parse_args()
    if a.cmd == "add":
        row = add(a.job, a.n, a.platform, title=a.title, url=a.url, hook_type=a.hook_type,
                  total_score=a.total_score, dur=a.dur,
                  metrics={"views": a.views, "likes": a.likes, "comments": a.comments, "shares": a.shares})
        print(f"registrado: {row['id']} ({row['job']} clip_{row['n']} em {row['platform']})")
    elif a.cmd == "list":
        for r in load_rows():
            m = r.get("metrics") or {}
            print(f"{r['id']}  {r['job']} clip_{r['n']}  {r['platform']:7s}  views={m.get('views', 0)}  "
                  f"hook={r.get('hook_type') or '-'}  {r.get('title', '')[:60]}")
    else:
        ctx = context()
        print(ctx if ctx else f"contexto inativo: menos de {MIN_POSTS} posts com métricas")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Operator verdicts on live-chat spikes: false positives and confirmed goods.

The Lives tab lets the operator judge each spike ("ok" / "falso positivo"). Those
verdicts land here and are injected into the LLM re-rank prompt as tie-breaker
context — never as scores, the heuristic ranking stays untouched (spec D5).
"""
import json
from datetime import datetime, timezone
from pathlib import Path

STATE = Path(__file__).resolve().parent.parent / "dashboard" / "state"
FEEDBACK_PATH = STATE / "spike_feedback.json"
VERDICTS = ("fp", "ok")  # fp = false positive (sieve lookalikes), ok = confirmed good
KEEP_ROWS = 500


def load():
    try:
        rows = json.loads(FEEDBACK_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return rows if isinstance(rows, list) else []


def record(job, utc, offset, verdict):
    """Persist one verdict; same job+utc+offset collapses to a single row (the latest)."""
    if verdict not in VERDICTS:
        raise ValueError(f"verdict inválido: {verdict!r} (use {', '.join(VERDICTS)})")
    row = {"job": str(job), "utc": str(utc), "offset": str(offset), "verdict": verdict,
           "ts": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    rows = [r for r in load() if not (r.get("job") == row["job"] and r.get("utc") == row["utc"]
                                      and r.get("offset") == row["offset"])]
    rows.insert(0, row)
    STATE.mkdir(parents=True, exist_ok=True)
    FEEDBACK_PATH.write_text(json.dumps(rows[:KEEP_ROWS], ensure_ascii=False, indent=1), encoding="utf-8")
    return rows


def context(top_n=10):
    """Tie-breaker context for rank_llm, or None when the operator judged nothing yet."""
    rows = load()
    if not rows:
        return None
    fp = [r for r in rows if r["verdict"] == "fp"][:top_n]
    ok = [r for r in rows if r["verdict"] == "ok"][:top_n]
    lines = []
    if fp:
        lines.append("Picos marcados como FALSO POSITIVO pelo operador (reações de chat que não viraram "
                     "momento bom; peneire candidatos semelhantes a estes): "
                     + " | ".join(f"{r['utc']} ({r['offset']}, job {r['job'][:8]})" for r in fp))
    if ok:
        lines.append("Picos CONFIRMADOS bons pelo operador (padrões semelhantes são desejáveis): "
                     + " | ".join(f"{r['utc']} ({r['offset']}, job {r['job'][:8]})" for r in ok))
    if not lines:
        return None
    return "\n".join(lines)

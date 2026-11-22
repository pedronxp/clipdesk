"""One command to monitor a live and deliver post-ready clips.

Pipeline:
  live.py (record + chat + spikes) -> post_capture.run() (CFR rebuild -> chat.json ->
  moments.py --chat -> rank_llm.py re-rank -> cut.py --preset top N -> translate ->
  pack.py --export). The post-record half lives in post_capture.py so the dashboard
  queue (spec D0) and this CLI share one implementation.

usage: python monitor.py <live-url> --minutes 3 --top 2 --preset react-br
                         [--legendas original|pt|pt,en] [--out out] [--export on|off]
"""
import argparse, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from common import job_dir, preset_values
import post_capture
from post_capture import discard_void, parse_legendas, run_step, PY, SCRIPTS
from post_capture import blur_cut_cmd, draft_title  # re-exported: tests and callers import from monitor


def main():
    ap = argparse.ArgumentParser(description="Monitor a live and deliver post-ready clips.")
    ap.add_argument("url")
    ap.add_argument("--minutes", type=float, default=3)
    ap.add_argument("--top", type=int, default=2)
    ap.add_argument("--preset", default="", help="editing preset (default: presets.json default)")
    ap.add_argument("--legendas", default="", help="original | pt | pt,en (overrides the preset)")
    ap.add_argument("--out", default="out", help="export root (one folder per clip)")
    ap.add_argument("--export", choices=["on", "off"], default="on")
    ap.add_argument("--slug", default="", help="export folder name (default: live job + clip number)")
    ap.add_argument("--keep-void", action="store_true",
                    help="keep the capture when it ends without a single clip (default: delete it)")
    a = ap.parse_args()

    pname, pvals = preset_values(a.preset)
    pname = pname or ""
    base = (pvals.get("lang") or "").strip()
    langs = (pvals.get("langs") or "").strip()
    base, extras = parse_legendas(a.legendas, base, langs)
    print(f"preset: {pname} | base captions: {base or 'original (word)'} | variants: {', '.join(extras) or '-'}")

    live_jd = job_dir(a.url)
    # live.py must not delete its own capture: a run with no chat spike can still yield
    # candidates from loudness/heat, so the void is decided after the full pipeline.
    rc = run_step([PY, SCRIPTS / "live.py", a.url, "--minutes", a.minutes, "--keep-void"], "record live")
    if rc != 0:
        print("live.py failed; stopping", file=sys.stderr)
        discard_void(a.keep_void, live_jd)
        return 1

    return post_capture.run(live_jd, top=a.top, preset=a.preset, legendas=a.legendas,
                            out=a.out, export=(a.export == "on"), keep_void=a.keep_void,
                            slug=a.slug)


if __name__ == "__main__":
    sys.exit(main())

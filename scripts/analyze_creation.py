"""Analyze a source and optionally render its highest-ranked moments automatically.

Pipeline: moments.py (download -> transcribe -> score) -> rank_llm.py (LLM re-rank, only when
options carry rerank=true AND an LLM is configured; rank_llm exits 3 otherwise and that is
"skip", not a failure) -> cut.py per candidate. Stage progress is patched into the job's
creation.json so the dashboard's Criar cortes panel can show live progress.

Quick-clips ao vivo: live.py spawns this on each spike take with options carrying
durations="30" (takes are ~35s; the default --min 60 window cannot fit). A failed react/
facecam cut retries once with blur (no webcam in the take), mirroring post_capture.
"""
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import ROOT, job_dir, load_json, save_json

PY = sys.executable
DURATIONS_RE = re.compile(r"^\d+(,\d+)*$")
CAM_LAYOUTS = {"react", "facecam"}


def set_stage(job, stage):
    """Patch the stage text into creation.json; best-effort, never blocks the pipeline."""
    path = Path(job) / "creation.json"
    try:
        data = load_json(path) if path.exists() else {}
    except Exception:
        data = {}
    if isinstance(data, dict):
        data["stage"] = stage
        try:
            save_json(path, data)
        except Exception:
            pass


def build_moments_cmd(source, top, max_minutes, durations=None):
    """moments.py command; valid --durations tiers override the default --min/--max window
    (needed for spike takes of ~35s, where a 60s floor cannot fit). Malformed values are
    silently dropped so a bad option never breaks the pipeline."""
    cmd = [PY, str(ROOT / "scripts" / "moments.py"), str(source),
           "--top", str(int(top)), "--max-minutes", str(int(max_minutes))]
    raw = str(durations or "").strip()
    if raw and DURATIONS_RE.match(raw):
        cmd += ["--durations", raw]
    return cmd


def should_retry_blur(layout):
    """Only the webcam layouts benefit from the blur fallback (gameplay is always there)."""
    return layout in CAM_LAYOUTS


def blur_retry_cmd(cut_cmd):
    """The blur fallback: same command with the layout swapped (original untouched)."""
    cmd = list(cut_cmd)
    cmd[cmd.index("--layout") + 1] = "blur"
    return cmd


def main():
    if len(sys.argv) != 5:
        print("usage: analyze_creation.py <source> <top> <max_minutes> <options-json>", file=sys.stderr)
        return 2
    source, top, max_minutes, raw_options = sys.argv[1:]
    options = json.loads(raw_options)
    job = job_dir(source)
    set_stage(job, "transcrevendo e analisando o vídeo")
    result = subprocess.run(build_moments_cmd(source, top, max_minutes, options.get("durations")), cwd=str(ROOT))
    if result.returncode:
        return result.returncode
    set_stage(job, "re-rank dos momentos")
    if options.get("rerank"):
        rerank = subprocess.run([PY, str(ROOT / "scripts" / "rank_llm.py"), str(job), "--top", str(int(top))],
                                cwd=str(ROOT))
        if rerank.returncode:
            print("re-rank indisponível; seguindo com a heurística", file=sys.stderr)
    else:
        print("re-rank LLM não solicitado (configure o LLM na aba Ajustes)", file=sys.stderr)
    if options.get("mode") != "auto":
        return 0

    set_stage(job, "gerando os cortes")
    data = load_json(job / "candidates.json")
    candidates = data.get("candidates") or []
    if not candidates:
        print("A análise não encontrou momentos para cortar automaticamente.", file=sys.stderr)
        return 1
    duration = str(options.get("duration") or "auto")
    layout = options.get("layout") if options.get("layout") in {"blur", "fill", "react", "facecam"} else "blur"
    captions = options.get("captions") if options.get("captions") in {"word", "bubble", "sentence", "none"} else "word"
    target_lang = str(options.get("lang") or "pt").lower()
    rendered_count = 0
    # LLM total wins over the heuristic score when a re-rank happened (supoclip idea)
    for candidate in sorted(candidates, key=lambda row: float((row.get("llm") or {}).get("total_score") or row.get("score") or 0), reverse=True):
        pick = int(candidate["n"])
        llm = candidate.get("llm") or {}
        title = str(llm.get("hook_title") or "").strip()
        if not title:
            text = re.sub(r"\s+", " ", str(candidate.get("text") or "")).strip()
            title = (text.split(". ", 1)[0][:80] or f"Momento {pick}").strip()
        cut = [PY, str(ROOT / "scripts" / "cut.py"), str(job), "--pick", str(pick),
               "--layout", layout, "--orientation", options.get("orientation", "vertical"),
               "--hook", title, "--captions", captions]
        if captions == "sentence":
            cut += ["--lang", target_lang]
        if duration in {"60", "180"}:
            target = float(duration)
            source_duration = float(data.get("duration") or candidate.get("end") or 0)
            center = float(candidate.get("peak") if candidate.get("peak") is not None else
                           (float(candidate.get("start") or 0) + float(candidate.get("end") or 0)) / 2)
            start = max(0.0, center - target / 2)
            end = min(source_duration, start + target)
            start = max(0.0, end - target)
            cut += ["--start", f"{start:.2f}", "--end", f"{end:.2f}"]
        rendered = subprocess.run(cut, cwd=str(ROOT))
        if rendered.returncode and should_retry_blur(layout):
            print(f"take {pick}: {layout} falhou; repetindo com blur (fallback sem webcam)", file=sys.stderr)
            rendered = subprocess.run(blur_retry_cmd(cut), cwd=str(ROOT))
        if rendered.returncode:
            print(f"Falha ao gerar o take {pick}; seguindo para o próximo.", file=sys.stderr)
            continue
        sidecar = job / "edits" / f"clip_{pick}.json"
        if sidecar.exists():
            meta = load_json(sidecar)
            meta.update({"title_review": True, "title_draft": title, "auto_generated": True,
                         "hook_type": llm.get("hook_type") or "", "llm_total": llm.get("total_score")})
            save_json(sidecar, meta)
        rendered_count += 1
    if not rendered_count:
        return 1
    print(f"{rendered_count} corte(s) automático(s) criado(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())

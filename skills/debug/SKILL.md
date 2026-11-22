---
name: debug
description: Diagnose and fix failures in the clip pipeline (cut.py, live.py, monitor.py, translate.py, dashboard). Use when a command fails, a clip looks wrong, the dashboard shows an error, monitoring stops, or the user says "debug", "está quebrado", "não funciona".
---

# Debug the clip pipeline

Reproduce first, then read the exact error, then change ONE thing and re-run. Never hide an error or report a step you did not run.

## 1. Reproduce and capture

Run the failing command again and keep the last 15 lines of output (stderr included). Errors here are designed to explain themselves — the `skipped:` messages state the reason and the fix.

## 2. Known failures and their fixes

| Error (exact start) | Cause | Fix |
|---|---|---|
| `ffmpeg not found. Run the setup skill first` | ffmpeg/ffprobe not on PATH of this shell | Windows: `PATH="$PATH:$(cygpath -u "$LOCALAPPDATA")/Microsoft/WinGet/Links:/c/ffmpeg/bin"`, verify with `ffmpeg -version`. The dashboard already appends these dirs to child processes (`child_env()` in `dashboard/server.py`) |
| `could not get a stream URL (is the video live?)` | live.py target ended or region-blocked | Confirm with `python scripts/streams.py --query "gta 6"`; pick another live |
| `skipped: no streamer face over gameplay in this moment (...)` | react layout needs a separate webcam box over gameplay | Pick another moment, use `--layout blur`/`facecam`, or pass `--cam x:y:w:h` manually |
| `skipped: the area outside the webcam is not gameplay (...)` | menu/screen-share scene | Pick a moment where the game is on screen |
| `StopIteration` at `pick = next(...)` | candidates.json was regenerated; the pick number no longer exists | List picks: `python -c "import json;print([(c['n'],c['start'],c['end']) for c in json.load(open('jobs/<id>/candidates.json'))['candidates']])"` and re-run with a valid `--pick` |
| `note: google translate blocked, used the MyMemory fallback` | Google rate-limited/blocked | Not a failure — sentences still translate or fall back to the original text. Proofread the cache (`sentences_cache`) for leftover source-language sentences |
| `warning: translate failed (...); keeping original text` | network down | Captions keep the original language; re-run later. Never mask this as success |
| `ERROR: [youtube] ...: Sign in to confirm you're not a bot` | YouTube now requires cookies for stream extraction | Configure cookies once: `dashboard/state/config.json` `{"ytdlp_cookies": "chrome"}` (or env `GTAVI_YTDLP_COOKIES`). `--cookies-from-browser chrome/edge` fails while that browser is OPEN — close it for a moment, or export a `cookies.txt` and set the file path instead. Twitch/Kick need no cookies |
| `UnicodeEncodeError: 'charmap' codec can't encode ...` in a watch log | emoji in chat spike words printed to a cp1252 log — crashed live.py before cutting the spike | Fixed: live.py reconfigures stdout to UTF-8 and the dashboard sets `PYTHONIOENCODING=utf-8` for children. If you still see it, the server predates the fix — restart it and the monitors |
| `facecam: no stable face found (...), using the blur layout` | no cam in this window | Expected fallback, not a bug — cut continues |
| dashboard `{"error": "clip_3.json não existe"}` | wrong job/n or the clip was never cut | Check `jobs/<id>/edits/` contents; the dashboard only lists jobs under `jobs/` |
| monitor stops after ~0 segments | stream URL resolved but ffmpeg died (region, 403) | See `dashboard/logs/watch_<jobid>.log` tail |
| `attempt 1 failed: HTTP Error 410: Gone` in rank_llm | the configured LLM model reached end of life on the NVIDIA API (`meta/llama-3.3-70b-instruct` died 2026-08-26) | Pick a live model in the dashboard Ajustes tab (`/api/settings/test` validates) — verified working 2026-11-20: `google/gemma-4-31b-it`. Reasoning models (glm/minimax "flash") return empty `content` — avoid them for re-rank |
| `Please make sure that the server is reachable at http://127.0.0.1:4416` + `No video formats found!` | YouTube EJS challenge needs a JS runtime; the standalone `yt-dlp.exe` auto-detects Deno only | Always call yt-dlp through `common.ytdlp_base()` — it adds `--js-runtimes node` when `yt_dlp_ejs` is installed and node exists. Raw `yt-dlp.exe` + cookies alone is NOT the pipeline path |
| `NameError: name 'time' is not defined` in the rank_llm retry | `time.sleep(2)` on the retry path without the import — crashed on any transient LLM error | Fixed: `import time` in `rank_llm.py` + regression test `tests/test_rank_llm_retry.py` |

## 3. Where the state lives

- `jobs/<id>/` — `candidates.json`, `transcript.json` (dict `{"language","words"}`; legacy jobs: bare list), `edits/clip_<n>.{mp4,json,ass}`, `live/` (segments, `spikes.md`, `live.json`), `info.json`
- `dashboard/logs/watch_<jobid>.log` — live.py output; `dashboard/state/watchlist.json` — monitors and pids
- `log.md` — every cut appends a row (date, job, pick, lang, hook/title)
- `out/<slug>/` — pack.py export only

## 4. Fix loop

One change per iteration, in this order: wrong flag/arguments → stale generated file (delete transcript.json/candidates.json only if you accept a re-transcribe) → environment (PATH, venv) → code. Re-run the exact command; confirm the expected output line (e.g. `facecam: cam box ...`, `captions: N sentences machine-translated to 'pt'`) before declaring fixed.

## Rules

- Report outcomes faithfully: if a step failed, say so with the error line.
- Do not delete user data (clips, recordings, chat logs) to "fix" a problem — move it aside to `test/` instead.
- If the same failure happens twice, record it in this skill's table (append a row) so the next session fixes it in seconds.

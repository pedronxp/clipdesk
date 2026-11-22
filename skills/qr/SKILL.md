---
name: qr
description: Final quality review of a clip with judgment, before posting — watch the clip and give a POST/FIX/DISCARD verdict. Use after qa passes; when the user asks "pode postar?", "revisa esse clipe", "review".
---

# QR — the judgment pass

QA proves the clip is technically correct. QR decides whether it is *good* and *honest*. Watch the clip (dashboard player or the mp4 directly) before any verdict — a review of a clip you did not watch is invalid.

## What to judge, in order

1. **Hook (first second).** Does the title/hook promise what the clip delivers? Rule from `hook-writer`: the clip must deliver the hook — if the title says 3 things, 3 things appear.
2. **Captions sync.** Sentence captions sit on the sentence's spoken window (no drifting bubbles, no sentence from another moment). Word captions highlight the spoken word.
3. **Title truth.** Never invents a quote or an opinion the streamer did not express; CAPS on 2-3 key words; describes what happens on screen. PT-BR natural, not machine-translation word order — rewrite yourself, do not machine-translate titles (spec D5).
4. **Language mix.** Caption language matches `lang_target`; any leftover source-language sentence is a FAIL (send back to `qa` step 3). Audio stays in the streamer's language — that is intentional.
5. **Pacing.** Starts clean (no dead air), ends on the payoff, no mid-clip dead segments.
6. **Safety and rules.** Slurs masked (`n****`), no hard swears unless `--allow-profanity` was intentional, no spoilers in the title during launch week, no fake urgency ("wait for it"), at most one emoji, credit goes in the post caption — never burned into the video.

## Verdict

- **POST** — everything above passes. Show the human the clip, the thumbnail, the platform caption, and wait for their "yes". Nothing uploads by itself.
- **FIX** — name the single problem, who fixes it (`cut.py` re-render with new title via the dashboard ✎, re-cut another moment, pack.py thumbnail), then run `qa` again and come back.
- **DISCARD** — the moment itself is weak (no payoff, unreadable scene). Say why in one line and pick another moment.

One verdict per clip, written as: `QR: POST|FIX|DISCARD — <one-line reason>`.

---
name: qa
description: Technical quality assurance of a finished clip or pipeline output — run the automated checks and verify the metadata. Use after cutting, translating or exporting, before the qr review or posting; when the user says "QA", "checar qualidade", "verificar o clipe".
---

# QA — technical checks before review

All checks are commands with evidence. A step without output you actually read is not a check. Fix every FAIL (see the `debug` skill) before moving to `qr`.

## 1. Spec checks (automated)

```
python scripts/check.py jobs/<id>/edits/clip_<n>.mp4 --campaign campaigns.md   # length, format, hashtags, banned content
python -c "import sys; sys.path.insert(0,'scripts'); from common import ffprobe; import json; print(json.dumps(ffprobe('jobs/<id>/edits/clip_<n>.mp4'),indent=1))" | grep -E "height|width|duration"
```

Expect: 1080x1920 (or 1920x1080 when `orientation: horizontal`), 30 fps, duration 7-30 s (react clips to 60 s), audio 48 kHz.

## 2. Metadata is truthful (clip_<n>.json)

| Field | Must be |
|---|---|
| `lang_source` / `lang_target` | matches the audio and the captions actually burned (badge in the dashboard shows the same) |
| `captions_mode` | `sentence` when translated, `word`/`bubble` when native, `none` only if requested |
| `sentences_cache` | filled for `sentence` mode; empty list is a bug in a translated clip |
| `source_url` / `source_channel` | real streamer URL/channel for credit — `None` on spike-derived clips is a finding |
| `sentences_cache[].s/e` | each `e > s`, windows inside [start, end] of the clip |

## 3. Captions proofread (translated clips)

Machine translation falls back to the original text when the network fails — leftover source-language sentences burn straight into the video:

```
python -c "import json; [print(e['s'], e['text'][:80]) for e in json.load(open('jobs/<id>/edits/clip_<n>.json'))['sentences_cache']]"
```

Flag every sentence still in `lang_source` while `lang_target` is different. A clip with 2+ leftovers goes back through `cut.py --translations-file` after fixing the cache text, or is re-translated later.

## 4. Assets complete

- `edits/clip_<n>.ass` exists (captions were burned from it)
- `pack.py --thumb` output (`thumb_<n>.jpg`): title band shows every title line, nothing cut at the edges
- `legendas.md` / `clip_<n>_captions.md` present when translate.py ran
- `log.md` has a row for this cut (proves the hook is registered for the no-repeat rule)

## 5. Report format

A PASS/FAIL table, one row per check above, each PASS with the value you observed and each FAIL with the fix applied. A QA with unseen output is not a QA. Only a full PASS moves the clip to `qr`.

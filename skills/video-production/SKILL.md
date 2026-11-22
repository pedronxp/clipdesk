---
name: video-production
description: The correct end-to-end flow to produce one post-ready clip from a live stream, in order, with quality gates between steps. Use when producing a clip from scratch ("produzir um clipe", "fazer um vídeo do zero", "monitorar e entregar"), or when the user asks how the pipeline works.
---

# Video production — the correct order

Every step has an expected output. A step's output feeds the next step's input; skipping a gate (qa/qr) produces clips that fail later. Nothing uploads without the human's "yes".

## The fast path (one command)

```
python scripts/monitor.py <live-url> --minutes 60 --top 2 --preset react-br --legendas pt --out out/<slug> --export on
```

`monitor.py` chains: live.py (record + chat + spikes) → CFR rebuild → moments.py `--chat` → cut.py `--preset` for the top N → translate.py for extra languages → pack.py `--export`. Draft titles come from the moment's own speech and are marked for human review. Run `qa` + `qr` on each delivered clip before posting.

## The step-by-step path (control at each gate)

1. **Discover** — `python scripts/streams.py --query "gta 6"` (or dashboard → 📡 + Lives). Pick a live you may clip (campaign or permission).
2. **Monitor** — dashboard ▶ monitorar, or `python scripts/live.py <url> --minutes 60`. Output: `jobs/<id>/live/` with `spikes.md` (one clip per chat explosion) and `live.json`.
3. **Moments** — `python scripts/moments.py <vod-or-spike> --top 8 --chat <chat.json>`. Output: `candidates.json/md` with scores and `why`. Transcript records the detected language (`{"language","words"}`).
4. **Title** — `hook-writer` skill: 3 options, pick one, PT-BR reaction-title formula ("illojuan REALIZOU ..."). Never machine-translate titles.
5. **Cut** — `python scripts/cut.py jobs/<id> --pick <n> --preset react-br --title "..."`. Expected: `facecam: cam box ...`, `captions: N sentences machine-translated to 'pt'`, then the mp4 path. `--translations-file` re-renders offline from a clip's cache.
6. **QA gate** — run the `qa` skill on the clip. All PASS or fix first.
7. **Multilang (optional)** — `python scripts/translate.py jobs/<id> --pick <n> --langs es,de,ru`. One variant per language as cards in the dashboard; QA each.
8. **Export** — `python scripts/pack.py jobs/<id> --pick <n> --export out/<slug>/ --credit "@streamer"`. Output: video + thumbnail + legendas.md, intermediates stay in jobs/.
9. **QR gate** — run the `qr` skill. Verdict POST / FIX / DISCARD.
10. **Post** — human posts (credit in the caption, platform captions from legendas.md), then records real numbers in the `learning-loop`.

## Rules

- One clip per moment; `log.md` prevents repeating hooks within 7 days.
- qa between cut and export; qr between export and post. Never both skipped.
- Any failure: `debug` skill first, then resume the step — do not skip steps to "get past" an error.
- Monitor for 60 min max per run; spikes are the moments worth cutting.

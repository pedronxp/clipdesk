---
name: clip-cutter
description: Cut a picked moment into a finished 1080x1920 vertical clip with word-by-word captions, and a hook box. Use after moment-finder, or when the user says "cut it", "make the clip", "make it vertical".
---

# Clip cutter

Run:

`python "${CLAUDE_PLUGIN_ROOT}/scripts/cut.py" jobs/<id> --pick <n> --preset react-br --title "TITLE"`

Or without a preset:

`python "${CLAUDE_PLUGIN_ROOT}/scripts/cut.py" jobs/<id> --pick <n> --layout fill|blur|facecam|react --hook "TEXT"`

- `fill`: crop the game video to fill the frame. Default for gameplay.
- `blur`: full video centered over a blurred copy. Use when cropping would cut off the action.
- `facecam`: gameplay with the streamer's camera shown. Use for reactions.
- `react`: streamer face on top, game below, big title on the seam.

Presets from `scripts/presets.json`:

- `react-br`: react layout, PT-BR whole-sentence captions (`--lang pt`), face zoom, title box, thumbnail. The default for Brazilian pages.
- `original`: no translation, native word-by-word captions.
- `blur-multi`: blur layout + PT/EN variants.

Explicit flags override the preset.

Language and zoom:

- `--lang pt` translates the captions by sentence: one bubble per spoken sentence, each filling that sentence's time window. Same language as the audio keeps native word-by-word captions. `--captions none` wins.
- `--zoom off|face|game|both`: slow punch-in on the cam panel (1.18x), the game (1.10x), or both. Default `off` when no preset.
- The title and hook are never machine translated: write them in the target language (PT-BR by default for `react-br`).

Before running:

1. Get 3 hook options from the `hook-writer` skill and use the best one. For PT-BR use the reaction-title formula.

After running:

1. Output is an mp4 in `jobs/<id>/edits/`. Check it is 1080x1920, the hook/title shows in the first 2 seconds, captions are readable and match the spoken timing. No credit text is burned into the video; the poster credits the creator in the post.
2. Run `rule-check` on it. Fix and re-cut until it passes.
3. Make the thumbnail and clean export: `python "${CLAUDE_PLUGIN_ROOT}/scripts/pack.py" jobs/<id> --pick <n> --export out/<slug>/`. The export folder contains only `clip.mp4`, `thumb.jpg` and `legendas.md`.
4. Trim 7 to 30 seconds for most clips. End on the payoff.
5. Show the human the file, the thumbnail, the caption text, and ask for "yes". Do not upload anything.

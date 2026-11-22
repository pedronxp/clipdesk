---
name: twitch-reaction
description: >-
  Watch the biggest GTA 6 Twitch streamer and make reaction clips: face on top, game below, big title. Use when the user wants streamer reaction clips, "face on top" clips, Twitch clips, or asks who is streaming GTA 6 right now.
---

# Twitch reaction clips

Face on top, game below, a big title on the seam. The format of most viral GTA 6 streamer clips.

## 1. Find the streamer (no Twitch account or key needed)

`python "${CLAUDE_PLUGIN_ROOT}/scripts/streams.py" --query "gta 6"` lists the top live Grand Theft Auto VI streams on Twitch and YouTube, ranked by viewers. Pick a streamer who runs a clipping campaign (check `campaigns.md`), or one whose clips you have permission to post.

## 2. Get the moment

- Live: `python "${CLAUDE_PLUGIN_ROOT}/scripts/live.py" https://twitch.tv/<login> --minutes 60` records the stream, reads Twitch chat anonymously and cuts a clip every time chat explodes.
- After the fact: `python "${CLAUDE_PLUGIN_ROOT}/scripts/moments.py" <twitch VOD or clip URL> --top 8` scores the VOD and writes `candidates.md`.

## 3. Cut the reaction clip

```
python "${CLAUDE_PLUGIN_ROOT}/scripts/cut.py" jobs/<id> --pick 1 --preset react-br \
  --title "Speed PERCEBEU por que o GTA 6 levou 13 ANOS"
```

- The `react-br` preset sets react layout, auto cam, title box, PT-BR sentence captions and face zoom. Without a preset, use `--layout react --cam auto --title "..." --title-style box --lang pt --zoom face`.
- `--cam auto` finds the streamer's webcam itself (face detection plus the webcam frame around it). It prints where it found the cam. If it finds no stable face it falls back to the blur layout and says so. Check the first frame once; if the crop is wrong, pass `--cam x:y:w:h` fractions.
- `--title-style box`: white band with bold black text on the seam. `--title-style outline`: white text with a thick black outline across the top of the game, for short titles ("Fighting Cops in GTA 6").
- `--captions bubble`: white speech bubble ("Oh my god!"). `word`: word-by-word captions. `none`: title only. `--lang pt` switches to whole-sentence PT-BR bubbles with the sentence's real timing.
- `--lang pt` is the current flag (`--captions-lang` is a deprecated alias). It translates captions only: the title and hook stay exactly as typed, so write them in PT-BR and proofread before posting.
- `--zoom face` adds a slow punch-in on the cam panel; `game` is subtler on the gameplay; `both` does both; `off` keeps today's framing.
- Write the title with `hook-writer` (PT-BR reaction titles section).
- For one command from live URL to clean export, use `monitor.py <url> --minutes 3 --top 2 --preset react-br`.

## Rules

- Clip a streamer only inside their campaign or with permission. Credit them in the post caption, not on the video.
- Titles describe what happened. Never invent a quote or an opinion.
- Run `rule-check` before posting. No spoilers in the title during launch week.

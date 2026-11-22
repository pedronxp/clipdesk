---
name: hook-writer
description: Write on-screen hooks for GTA 6 clips. Use whenever a clip needs a hook, a title or a caption opener, or when the user asks for hook options.
---

# Hook writer

Write 3 options per clip. Sentence case. Short: 3 to 9 words fit the hook box.

## What works

- Number promise: "3 things you missed in this trailer"
- Shock fact: "This car is hidden in plain sight"
- Situation: "When the cops chase the wrong player"
- Negation: "You can't do this in GTA 5"
- Question: "Would you survive this chase?"
- Naming the streamer or creator when they are known: "Even [name] didn't see this coming"

## Reaction titles (twitch-reaction layout)

For a streamer's reaction clip the title sits on the seam between face and game for the whole clip. Formula: streamer name + reaction verb + what they reacted to, with CAPS on 2 or 3 key words.

- "Speed REALIZED why GTA 6 took ROCKSTAR 13 YEARS"
- "Kai LOST IT when the cops showed up in GTA 6"
- "Fighting Cops in GTA 6" (outlined style: short, 3 to 6 words)

Describe what happens on screen. Never put words in the streamer's mouth: no quotes they did not say, no made-up opinions. Emojis are optional, at most 2.

## PT-BR reaction titles

Brazilian GTA pages respond to the same formula in Portuguese: streamer + reaction verb + what happened, with CAPS on 2 or 3 key words.

- "Speed PERCEBEU por que o GTA 6 levou 13 ANOS"
- "Kai ENLOUQUECEU quando a polícia chegou"
- "Illojuan FLIPA com o GTA 6 jogando sinuca"

Keep the streamer's name exactly as they write it. Portuguese titles can be a little longer than English ones, but stay under ~70 characters for the box.

`cut.py --lang pt` translates captions only. The title and hook stay exactly as typed: write them in PT-BR yourself and proofread before posting.

## What does not work

- Bare labels: "GTA 6 clip", "Funny moment"
- "Wait for it", "You won't believe this", "Watch till the end"
- Fake urgency or CAPS everywhere

## Rules

1. The clip must deliver the hook. If the hook says 3 things, show 3. No clickbait.
2. No spoilers for story moments in launch week. Label such clips SPOILER in the caption, not in the hook.
3. Never imply the page is Rockstar. Never write "official" for fan content.
4. Use "GTA 6". One emoji at most.
5. Do not reuse the same hook within a week. Check the repo's real `log.md` (cut.py appends job, pick, language and hook/title after every cut) and the dashboard "Últimos hooks" list.
6. Facts in the hook must be true and verified. Numbers must match the clip.

Return the 3 options and mark your pick.

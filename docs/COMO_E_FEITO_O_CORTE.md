# Como é feito o corte — do live ao MP4 vertical

Este documento explica, passo a passo, **como o motor transforma um momento da live em um
corte pronto para postar**. Usa como exemplo real o clip:

```
jobs/spike-6-d040390e/edits/clip_1.mp4
```

Fonte: `alanzoka` · spike 6 · janela final `3.0s → 30.77s` · layout `react`.

> Importante: existem **dois cortes** no caminho.
>
> 1. **Corte temporal do spike** — `live.py` recorta um trecho do meio da gravação.
> 2. **Corte visual final** — `cut.py` monta o vertical 1080x1920, com webcam em cima e
>    gameplay embaixo (a “costura no meio”), legenda, hook e áudio normalizado.

---

## 1. Visão geral

```
LIVE
 │
 ├─ live.py ──────────────► grava segmentos de 60 s + lê o chat
 │                              │
 │                              ├─ detecta pico de chat
 │                              └─ cut() recorta spike_6.mp4 (15 s antes + 20 s depois)
 │
 ├─ analyze_creation.py ───► orquestra o take
 │       │
 │       ├─ moments.py ─────► transcreve, pontua e escolhe a janela
 │       ├─ rank_llm.py ────► reordena, dá notas e cria o hook PT-BR
 │       └─ cut.py ─────────► renderiza o vertical final
 │
 └─ pack.py ───────────────► thumb + legendas + pasta out/
```

O exemplo percorreu exatamente esse caminho:

| Etapa | Resultado |
|---|---|
| Captura | `jobs/alanzoka-37680da5/live/seg_*.ts` |
| Spike | `jobs/alanzoka-37680da5/live/spike_6.mp4` |
| Análise | `jobs/spike-6-d040390e/candidates.json` |
| Re-rank | scores + hook `Procurando por Benedict Halifax` |
| Corte | `jobs/spike-6-d040390e/edits/clip_1.mp4` |
| Pacote | `thumb_1.jpg`, `clip_1.json`, `translate_cache.json` |

---

## 2. Etapa 1 — Captura da live (`live.py`)

### 2.1 Gravação em blocos de 60 s

O `live.py` resolve a URL de mídia com `yt-dlp -g` e entrega ao `ffmpeg` com `-c copy`
(sem re-encode). A gravação é dividida em segmentos:

```
jobs/<live>/live/
├── seg_00000.ts
├── seg_00001.ts
├── seg_00002.ts
├── ...
├── segments.csv      ← nome, início e fim de cada bloco
├── chat.twitch.jsonl ← chat cru (Twitch)
└── recorder.json     ← pid + início (o painel usa para saber se está vivo)
```

Por que em blocos:

- a gravação pode ser interrompida sem perder tudo;
- o painel mostra MB/segmentos em tempo real;
- o corte do spike pode concatenar só os blocos que interessam.

### 2.2 Leitura do chat

| Plataforma | Como lê |
|---|---|
| Twitch | IRC anônimo (`justinfanNNNN`, TLS 6697) |
| YouTube | `yt-dlp --write-subs --sub-langs live_chat` |

O motor mantém, por segundo:

- `raw` — quantidade de mensagens;
- `wgt` — peso (2 para mensagens de hype, 1 para o resto);
- `words` — texto das mensagens.

Ruído é filtrado: giveaway, raid, sponsor, “brb”, links e promoções não contam como spike.

### 2.3 Quando um spike dispara

A janela é julgada no **segundo da mensagem mais recente**, não no relógio da máquina
(o chat do YouTube chega 10–40 s atrasado).

Condições padrão:

| Parâmetro | Padrão | Significado |
|---|---|---|
| `--window` | 10 s | janela de mensagens avaliada |
| `--baseline` | 300 s | histórico anterior, por segundo ponderado |
| `--ratio` | 3.0 | a janela precisa ser ≥ 3× o baseline |
| `--min-msgs` | 8 | mínimo de mensagens cruas na janela |
| `--cooldown` | 45 s | distância mínima entre dois spikes |
| `--lag` | 8 s | deslocamento do corte (delay do stream + reação do chat) |
| `--before` | 15 s | quanto pegar antes do pico |
| `--after` | 20 s | quanto pegar depois do pico |

Trecho real do `live.py`:

```python
raw = tail.total(tail.raw, e - a.window + 1, e)
w   = tail.total(tail.wgt, e - a.window + 1, e)
nb  = min(a.baseline, elapsed_chat - a.window)
bw  = tail.total(tail.wgt, e - a.window - nb + 1, e - a.window) / nb
rate_w = w / a.window

if raw >= a.min_msgs and rate_w >= a.ratio * max(bw, 0.02):
    n += 1
    pending.append((n, float(e), raw, words, t + a.after + max(a.lag, 0)))
```

Quando dispara, o take **não é cortado na hora**: ele espera `after + lag` segundos para
que o trecho já exista na gravação.

---

## 3. Etapa 2 — O corte do spike (o “corte no meio da gravação”)

A função `cut()` do `live.py` converte o horário de parede do pico em posição no stream:

```python
f, st, en, mt, _ = comp[-1]          # último segmento completo
center = en - (mt - wall) + lag      # posição do evento no stream
t0, t1 = max(0.0, center - before), center + after
pick = [s for s in segs if s[2] > t0 and s[1] < t1]
```

O que acontece:

1. `mt` é o mtime do último segmento completo.
2. `wall` é o horário do pico no chat.
3. `center` corrige a diferença e aplica o `lag`.
4. `t0` e `t1` definem a janela: **15 s antes + 20 s depois**.
5. `pick` seleciona todos os segmentos que cruzam essa janela.
6. O `ffmpeg` concatena os blocos com `-c copy` e corta no ponto exato.

Linha do tempo:

```
gravação contínua
0s                                                              60min
|---- seg_00000 ----|---- seg_00001 ----| ... |---- seg_00059 ----|
                                          ▲
                                    pico no chat
                         t0 = center - 15s      t1 = center + 20s
                                  |---- spike_6.mp4 ----|
```

Comando equivalente:

```bash
python scripts/live.py "https://twitch.tv/alanzoka" --minutes 60 \
  --before 15 --after 20 --lag 8 \
  --window 10 --baseline 300 --ratio 3 \
  --min-msgs 8 --cooldown 45
```

Saída:

```
jobs/<live>/live/spike_6.mp4
jobs/<live>/live/spikes.md
```

O `spike_6.mp4` do exemplo tem **35,01 s**. É desse arquivo que sai o corte final.

---

## 4. Etapa 3 — Análise do take (`moments.py`)

O `analyze_creation.py` roda o `moments.py` no take. Para um take de ~35 s, ele usa
`--durations 30`, porque a janela padrão de 60–90 s não caberia.

### 4.1 Sinais usados

| Sinal | Como é medido | Peso |
|---|---|---|
| Loudness | dB acima da mediana de fala | `1,0 × z(excesso)` |
| Hype words | regex no transcript (EN + PT-BR) | `0,8 × z(hype)` |
| Most replayed | heatmap do YouTube | `2,0 × z(heat)` |
| Chat spike | mensagens/s com peso 2 para hype | `1,5 × z(chat)` |

```
score = 1,0·loud + 0,8·z(hype) + w_heat·z(heat) + w_chat·z(chat)
```

No take do exemplo, os sinais foram:

```json
"score": 3.99,
"why": ["loudness spike", "hype words"]
```

### 4.2 Como a janela é escolhida

`window_for()` coloca o pico a ~40% do início (hook cedo) e encaixa as bordas em fala real:

```python
s0 = max(0.0, peak - 0.4 * target)
e0 = min(dur, s0 + target)
# encaixa no início/fim das palavras
s0 = max(0.0, inside[0]["s"] - 0.3)
e0 = min(dur, inside[-1]["e"] + 0.6)
```

No exemplo, a janela heurística ficou:

```
signal_start: 15.77
signal_end:   35.01
```

### 4.3 O que o `candidates.json` guarda

```json
{
  "start": 3.0,
  "end": 30.77,
  "peak": 27,
  "tier": "30s",
  "score": 3.99,
  "why": ["loudness spike", "hype words"],
  "signal_start": 15.77,
  "signal_end": 35.01,
  "llm": {
    "hook_score": 8,
    "engagement_score": 10,
    "value_score": 5,
    "shareability_score": 7,
    "total_score": 30,
    "hook_type": "nenhum",
    "hook_title": "Procurando por Benedict Halifax"
  }
}
```

Repare: `start/end` são a janela **final**, e `signal_start/signal_end` preservam a janela
original da heurística. A diferença foi o re-rank.

---

## 5. Etapa 4 — Re-rank por IA (`rank_llm.py`)

As heurísticas dão **recall**; o LLM dá **precisão**.

O modelo recebe até 12 candidatos com janela, score, motivos e o transcript por sentença.
Ele devolve quatro notas de 0 a 25:

| Nota | Pergunta que responde |
|---|---|
| `hook_score` | o primeiro segundo prende? |
| `engagement_score` | o meio mantém a atenção? |
| `value_score` | entrega algo útil/interessante? |
| `shareability_score` | dá vontade de compartilhar? |

O `total_score` é recalculado localmente como **soma simples** — o número do modelo nunca
é confiado.

O LLM também pode mover as bordas em até ±15 s (`adj_start` / `adj_end`). Foi isso que
transformou:

```
heurística: 15.77 → 35.01
LLM:         3.00 → 30.77
```

O script valida o ajuste e impõe limites de duração antes de gravar.

Além disso, ele cria o **hook em PT-BR**:

```
Procurando por Benedict Halifax
```

Esse texto é o que aparece na caixa branca da imagem.

---

## 6. Etapa 5 — O corte final (`cut.py`)

Agora o take vira o MP4 vertical. O comando real foi:

```bash
python scripts/cut.py jobs/spike-6-d040390e --pick 1 \
  --layout react \
  --cam 0.7750:0.4528:0.2250:0.2833 \
  --start 3.0 --end 30.77 \
  --hook "Procurando por Benedict Halifax" \
  --lang pt \
  --captions sentence \
  --title-style box --title-secs 0 \
  --zoom off
```

### 6.1 Janela

`--pick 1` lê o candidato `n=1` do `candidates.json`. `--start/--end` sobrescrevem a janela.

```
spike_6.mp4 (35,01 s)
0      3,0s                          30,77s   35,01s
|-------|===============================|--------|
        janela final = 27,77 s
```

### 6.2 Layout `react` — a “costura no meio”

O vertical é montado com `vstack`:

```
1080 x 1920
┌──────────────────┐
│                  │  webcam  → 1080 x 730
│   rosto / cam    │           (TOP = 730)
│                  │
├──────────────────┤  ← costura y = 730
│                  │
│    gameplay      │  gameplay → 1080 x 1190
│                  │           (GAME_H = 1190)
└──────────────────┘
```

Constantes no `cut.py`:

```python
TOP = 730        # altura do painel da webcam = y da costura
GAME_H = H - TOP # 1190
```

O filtro real é:

```text
[0:v]split[a][b];
[a]crop=iw*0.2250:ih*0.2833:iw*0.7750:ih*0.4528,
   scale=1080:730:force_original_aspect_ratio=increase,
   crop=1080:730,setsar=1[cam];
[b]scale=<gw>:1190,crop=1080:1190:<gx>:0,setsar=1[game];
[cam][game]vstack[v];
[v]fps=30,subtitles='clip_1.ass'[vo]
```

O `game_offset()` escolhe o `gx` do gameplay para **não mostrar a webcam duas vezes**.

### 6.3 Como a webcam é encontrada

Com `--cam auto`, o `facecam.py`:

1. usa **YuNet (ONNX)** em ~12 frames;
2. mantém o rosto que fica no mesmo lugar na maioria dos frames;
3. cresce um retângulo ao redor do rosto com mapa de bordas;
4. valida que **fora da webcam existe gameplay** (histograma HSV + borda reta).

Se não houver rosto estável, o `react` pula o momento ou cai para `blur`.

No exemplo, o resultado foi:

```
cam = 0.7750:0.4528:0.2250:0.2833
```

Ou seja: `x=77,50%`, `y=45,28%`, `w=22,50%`, `h=28,33%` do frame original.

### 6.4 Legendas

`--lang pt` + `--captions sentence`:

- cada sentença falada vira uma bolha;
- o texto é traduzido como um todo;
- o tempo da bolha é o tempo real da fala;
- **não há sincronia de palavra falsa**.

Exemplo do cache:

```
"We're looking for Benedict Halifax."
→ "Estamos procurando por Benedict Halifax."
```

Palavrões e slurs são mascarados por padrão. `--allow-profanity` libera os palavrões
(slurs continuam mascarados).

### 6.5 Hook x título

| Flag | Onde aparece | Duração |
|---|---|---|
| `--hook` | caixa branca no topo, sobre a webcam | primeiros 2,6 s |
| `--title` | faixa branca na costura y=730 | `--title-secs` (0 = clip inteiro) |

No clip do exemplo, o texto foi passado como `--hook`, por isso ele aparece nos primeiros
segundos no topo. Se você quiser o texto fixo na costura, use:

```bash
--title "Procurando por Benedict Halifax" --title-style box --title-secs 0
```

### 6.6 Paleta

O preset/CLI define as cores:

| Cor | Padrão | Onde |
|---|---|---|
| `cap_color` | `#FFFFFF` | texto da legenda |
| `cap_highlight` | `#FFE500` | palavra ativa |
| `cap_outline` | `#000000` | contorno |
| `box_fill` | `#FFFFFF` | caixa do hook/título/bolha |
| `box_text` | `#000000` | texto sobre a caixa |

### 6.7 Áudio e encode

O comando final do `ffmpeg`:

```bash
ffmpeg -y -v error \
  -ss 3.00 -to 30.77 -i spike_6.mp4 \
  -filter_complex "<filtro acima>" \
  -map "[vo]" -map "0:a?" \
  -af "loudnorm=I=-14:TP=-1.5:LRA=11" \
  -t 27.77 \
  -c:v libx264 -preset medium -crf 20 -pix_fmt yuv420p \
  -c:a aac -b:a 160k -ar 48000 \
  -movflags +faststart \
  edits/clip_1.mp4
```

| Parâmetro | Por quê |
|---|---|
| `-ss` antes de `-i` | busca rápida no arquivo |
| `-t` explícito | corrige PTS esticado de gravações VFR |
| `loudnorm -14 LUFS` | padrão das plataformas |
| `libx264 medium crf20` | qualidade x tamanho |
| `+faststart` | MP4 começa a tocar antes de baixar tudo |

### 6.8 Saídas

```
jobs/spike-6-d040390e/edits/
├── clip_1.mp4        o corte final
├── clip_1.ass        legendas queimadas (referência)
├── clip_1.json       sidecar com todos os parâmetros
├── thumb_1.jpg       capa
└── translate_cache.json
```

O `clip_1.json` é o que permite re-cortar, traduzir e gerar thumb sem adivinhar nada:

```json
{
  "start": 3.0,
  "end": 30.77,
  "layout": "react",
  "hook": "Procurando por Benedict Halifax",
  "title": "",
  "cam": "0.7750:0.4528:0.2250:0.2833",
  "title_style": "box",
  "title_secs": 0,
  "zoom": "off",
  "orientation": "vertical",
  "lang_source": "en",
  "lang_target": "pt",
  "captions_mode": "sentence",
  "captions_style": "sentence",
  "allow_profanity": false
}
```

---

## 7. Etapa 6 — Thumb e pacote (`pack.py`)

```bash
python scripts/pack.py jobs/spike-6-d040390e --pick 1 \
  --platforms tiktok,reels,youtube \
  --export out/benedict-halifax/
```

O `pack.py`:

1. pega um frame **antes da primeira legenda queimada**;
2. queima a mesma faixa branca do título;
3. gera `thumb.jpg` 1080x1920 e versões por plataforma;
4. escreve `legendas.md` com crédito, URL e texto de post;
5. copia para `out/<slug>/` só o que você posta:

```
out/<slug>/
├── clip.mp4
├── thumb.jpg
└── legendas.md
```

---

## 8. Reproduzindo do zero

### Fluxo completo em um comando

```bash
python scripts/monitor.py "https://twitch.tv/alanzoka" --minutes 60 --top 3 --preset react-br
```

Esse comando faz:

```
live.py → post_capture.py → moments.py → rank_llm.py → cut.py → translate.py → pack.py
```

### Passo a passo manual

```bash
# 1. capturar e cortar spikes
python scripts/live.py "https://twitch.tv/alanzoka" --minutes 60

# 2. analisar o take
python scripts/moments.py jobs/<live>/live/spike_6.mp4 --durations 30 --top 3

# 3. re-rank com IA
python scripts/rank_llm.py jobs/spike-6-d040390e --top 3

# 4. cortar o vertical
python scripts/cut.py jobs/spike-6-d040390e --pick 1 \
  --layout react --lang pt --captions sentence \
  --hook "Procurando por Benedict Halifax"

# 5. thumb + pacote
python scripts/pack.py jobs/spike-6-d040390e --pick 1 \
  --platforms tiktok,reels,youtube --export out/benedict-halifax/
```

---

## 9. Resumo das decisões

| Decisão | Quem decide | Onde |
|---|---|---|
| O que é um spike | chat (janela, baseline, ratio) | `live.py` |
| Onde cortar o spike | `before` + `after` + `lag` | `live.py::cut()` |
| Qual momento vale a pena | loudness, hype, heatmap, chat | `moments.py` |
| Qual janela final | heurística + ajuste do LLM | `moments.py` / `rank_llm.py` |
| Qual hook | LLM (PT-BR) | `rank_llm.py` |
| Layout, legendas, cores | preset + flags | `presets.json` / `cut.py` |
| Onde fica a webcam | YuNet + OpenCV | `facecam.py` |
| Como renderizar | ffmpeg (filtros, ASS, x264) | `cut.py` |
| Thumb e texto de post | frame + banda + template | `pack.py` |

---

## 10. Por que o corte saiu em `blur` em vez de `react`?

O arquivo continua **1080x1920**; o que muda é a composição. O pipeline tenta o layout
`react` e, se o `cut.py` falhar, repete automaticamente com `blur` para não perder o momento:

```
react cut skipped (no facecam): retrying with the blur layout
```

No caso dos cortes do LipaoGamer, o `react` não falhou por falta de webcam. Ele quebrou com:

```
TypeError: 'float' object cannot be interpreted as an integer
  File "scripts/cut.py", line 614, in game_offset
    for ox in range(0, gw - w + 1, 8):
```

A causa: `game_offset()` recebia `cam_box` como frações `[x, y, w, h]`, mas reutilizava os
nomes `w` e `h` — que já eram o tamanho do painel em pixels. O `range()` então recebia um
float. Correção aplicada:

```python
cx, cy, cw, ch = cam_box          # frações
# w/h continuam sendo o painel em pixels (W/GAME_H)
```

Como diagnosticar:

1. Abra `dashboard/logs/create_<job>.log`.
2. Procure por `react falhou; repetindo com blur` e leia o traceback acima.
3. Rode o corte manualmente com `--layout react` para ver o erro completo.
4. Se a webcam não for detectada, o log mostra `no streamer face over gameplay` — aí o
   fallback `blur` é o comportamento correto.

Para forçar o layout esperado:

```bash
python scripts/cut.py jobs/<id> --pick 1 --layout react --lang pt --captions sentence
```

---

## 11. Limites e avisos

- O corte é **manual para postar**: nada é publicado automaticamente.
- A captura original pode ser apagada pela retenção de 24 h; o corte final é preservado.
- Sem LLM, o pipeline segue com a heurística e usa um título-rascunho da fala
  (`title_review: true`).
- Render é CPU: um take de 60 s leva ~4–6 min.
- Tradução depende de serviço externo (Google → MyMemory).
- Direitos: só corte dentro de campanha ou com permissão do criador; o crédito é gerado
  automaticamente, mas usá-lo é sua responsabilidade.

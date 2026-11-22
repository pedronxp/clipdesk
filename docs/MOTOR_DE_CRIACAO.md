# Motor de criação — o pipeline completo, fase por fase

Este documento explica **cada passo e fase do motor de criação de clips** do gtavi-claudecode:
do momento em que você descobre uma live até a pasta `out/` com o clip pronto para postar —
incluindo os arquivos intermediários que cada fase produz, os critérios numéricos que cada
decisão usa e onde cada fase pode ser configurada.

> Nada é publicado automaticamente. O motor entrega **clip + legendas + thumb + texto de post**;
> a revisão humana e o "postar" são sempre seus (decisão de projeto, ver `PRODUCAO.md`).

---

## 1. Visão geral: um motor, duas entradas

O motor tem um **núcleo de análise e render** compartilhado e duas formas de alimentá-lo:

```
 FLUXO A — live acontecendo agora (clipping em tempo real)
 ┌──────────┐   ┌────────┐   ┌───────────────┐   ┌──────────────────────────────────┐
 │ streams  │──►│ live.py│──►│ post_capture  │──►│ moments → rank_llm → cut         │
 │ (achar)  │   │ gravar │   │ (orquestra)   │   │ → translate → pack               │
 └──────────┘   └────────┘   └───────────────┘   └──────────────────────────────────┘

 FLUXO B — vídeo pronto (VOD do YouTube/Twitch/Kick ou arquivo em jobs/)
 ┌──────────────────────┐   ┌─────────────────────────────────────────────────┐
 │ moments.py direto    │──►│ rank_llm → cut → titles → pack                  │
 │ no URL               │   │ (via painel: analyze_creation.py orquestra)     │
 └──────────────────────┘   └─────────────────────────────────────────────────┘
```

As fases do motor, na ordem em que aparecem:

| # | Fase | Script(s) | Entrada | Saída |
|---|---|---|---|---|
| 0 | Descoberta | `streams.py`, `trends.py`, `twitch.py` | query | `streams.md/json`, `trends.md` |
| 1 | Captura ao vivo | `live.py` (+ `monitor.py`) | URL da live | `jobs/<id>/live/` (segmentos, chat, spikes) |
| 2 | Pós-captura / orquestração | `post_capture.py`, fila do dashboard | captura encerrada | CFR rebuild, `chat.json`, `monitor.json` |
| 3 | Análise de momentos | `moments.py` | vídeo fonte | `candidates.json` / `candidates.md` |
| 4 | Re-rank por IA | `rank_llm.py` (+ `performance.py`, `spike_feedback.py`) | `candidates.json` | scores LLM, hook PT-BR, janelas refinadas |
| 5 | Corte vertical | `cut.py` (+ `facecam.py`) | momento escolhido | `edits/clip_<n>.mp4` 1080x1920 |
| 6 | Multilang | `translate.py` | clip cortado | `edits/clip_<n>_<lang>.mp4` |
| 7 | Empacotamento | `pack.py` | clip final | `thumb.jpg`, `legendas.md`, `out/<slug>/` |
| 8 | Títulos | `titles.py` | fala do momento | `titles.md` (3 opções por take) |
| 9 | Portão de qualidade | `check.py` | clip + campanha | veredito PASS/FAIL |

Todo o trabalho vive em `jobs/<id>/` (nome = `<slug até 24 chars>-<sha1 da fonte, 8 chars>`,
definido por `common.job_dir`). `CLIP_DESK_HOME` muda a raiz do projeto; por padrão é o cwd.

---

## 2. Fase 0 — Descoberta (o que gravar / analisar)

**Scripts:** `scripts/streams.py`, `scripts/trends.py`, `scripts/twitch.py`
**Comando:** `python scripts/streams.py --query "gta 6" --limit 20 --out streams.md`

O objetivo desta fase é responder "o que existe agora, grande e em PT-BR, para eu monitorar?".

### 2.1 YouTube (`streams.py::youtube`)
- Busca em `youtube.com/results?...&sp=EgJAAQ%253D%253D&gl=BR&hl=pt-BR` — o `sp` é o filtro
  de **transmissões ao vivo**; `gl=BR` e `hl=pt-BR` dão o viés Brasil/português.
- Lista com `yt-dlp --flat-playlist -J`, depois resolve cada vídeo individualmente em paralelo
  (`ThreadPoolExecutor`, 8 workers) e mantém só `is_live`.
- Caminho degradado: se o YouTube responder o bot check ("Sign in to confirm…"), a própria
  busca (que já funcionou) é aproveitada sem viewers, e uma nota orienta exportar cookies
  (`scripts/export_cookies.py`, passo a passo no `PRODUCAO.md` §0.1).

### 2.2 Twitch (`streams.py::twitch` / `twitch_nokey`, `scripts/twitch.py`)
- **Com chave** (`TWITCH_CLIENT_ID` + `TWITCH_TOKEN` no ambiente): Helix oficial —
  `game_id` de "Grand Theft Auto VI" e `streams?first=100&language=pt` (filtro de idioma
  aplicado **no servidor** da Twitch).
- **Sem chave** (padrão): `twitch.py` usa o GQL público da Twitch (o mesmo Client-Id que o
  yt-dlp usa, sem conta). O fetch é **largo** — `max(limit, 100)` streams — de propósito:
  streamers PT-BR fora do top absoluto de viewers precisam existir na lista para o ranking
  abaixo poder promovê-los.
- O `top_streams` também tenta nomes alternativos de categoria ("Grand Theft Auto VI" ↔
  "GTA VI") e resultados de `searchCategories`, até achar uma categoria com lives.

### 2.3 Ranking PT-BR primeiro (`pt_flag` + `pt_sort`)
- `pt_flag`: True se o metadado `language` começa com "pt" (`pt`, `PT`, `pt-BR`) **ou** se o
  título contém um marcador (`pt-br`, `ptbr`, `português`, `gameplay pt`, `dublado`…).
- `pt_sort`: PT-BR primeiro, depois viewers desc — e **a ordenação acontece antes do corte
  do limite** (`t = pt_sort(t)[:limit]`), para nunca descartar um streamer PT pelo `--limit`.
- Kick é tentado uma única vez; quando a plataforma bloqueia clientes automatizados
  (Cloudflare), a seção sai com uma nota honesta e a dica de colar o link direto no painel.

### 2.4 Saídas
- `streams.md` — tabela legível por plataforma (`| # | Channel | Title | Viewers | Live for | URL |`).
- `streams.json` — mesmo conteúdo estruturado (`{"query", "checked", "sections": [...]}`); é o
  que o painel lê preferencialmente.
- `trends.py` é a versão para vídeos: ranqueia por **views por hora** (velocidade de viralização)
  e marca quais têm heatmap "most replayed" — útil no dia de lançamento.

---

## 3. Fase 1 — Captura ao vivo (`live.py`, `monitor.py`)

**Comando:** `python scripts/live.py <url> --minutes 60` (só gravar + spikes)
**Comando completo:** `python scripts/monitor.py <url> --minutes 60 --top 3 --preset react-br`
(um comando = grava + roda todo o resto do motor; `monitor.py` delega a segunda metade ao
`post_capture.run()`, a mesma implementação que a fila do painel usa).

### 3.1 Gravação (`Recorder`)
- Resolve as URLs de mídia com `yt-dlp -g` (vídeo ≤720p + áudio), entrega ao **ffmpeg** com
  reconexão automática (`-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5`) e `-c copy`
  (sem re-encode: leve, sem perda).
- Grava em **segmentos de 60 s** (`-f segment -segment_time 60`, mpegts): `jobs/<id>/live/seg_00000.ts`,
  `seg_00001.ts`, … com uma lista `segments.csv` (nome, início, fim) que serve de linha do tempo.
- Motivo dos blocos: gravar de uma live é interrompível — o painel mostra MB/segmentos ao vivo,
  e o processamento pode recomeçar a qualquer momento sem perder tudo.
- `live/recorder.json` guarda `pid + started`: é assim que o painel e o `voids.py` sabem que a
  captura está viva (com guard de atividade < 120 s, porque **Windows reusa PIDs**).

### 3.2 Leitura do chat em tempo real
- **Twitch** → thread com IRC anônimo (`twitch.py::irc_chat`, nick `justinfanNNNN`, TLS 6697 com
  fallback 6667): grava JSON-lines em `chat.twitch.jsonl` (`{"t": epoch, "user", "text"}`).
- **YouTube** → subprocesso `yt-dlp --write-subs --sub-langs live_chat`, que baixa o replay do
  chat conforme chega (`chat.live_chat.json*`).
- As classes `ChatTail` (YouTube) e `TwitchTail` (Twitch) fazem **tail incremental** desses
  arquivos e mantêm, por segundo: `raw` (contagem de mensagens), `wgt` (peso) e `words` (texto).
  - Mensagens com ID repetido são deduplicadas; **ruído é filtrado** (`NOISE`: giveaway, raid,
    sponsor, "brb", links, promo) para não virar falso spike.
  - Peso: **2** se a mensagem casa com `HYPE` (LMAO, KEKW, LOL, CLIP, HOLY, NO WAY, "W", kkkk,
    hahaha, rsrs, 💀😂🔥🤣), senão 1.

### 3.3 Detecção de spike (a lógica que decide "agora!")
A janela é julgada no **segundo da mensagem mais recente** (`last_ts - 1`), não no "agora" da
máquina — o replay do chat do YouTube chega 10–40 s atrasado com timestamp do servidor; julgar
no relógio local deixaria a janela vazia e o baseline inflado (nenhum spike dispararia).

Condições para um spike (tudo configurável por flag):

| Parâmetro | Padrão | Significado |
|---|---|---|
| `--window` | 10 s | janela de mensagens julgada |
| `--baseline` | 300 s | histórico imediatamente anterior (por segundo, ponderado) |
| `--ratio` | 3.0 | taxa da janela ≥ 3× a média do baseline |
| `--min-msgs` | 8 | e pelo menos 8 mensagens cruas na janela |
| `--cooldown` | 45 s | mínimo entre dois spikes |
| `--lag` | 8 s | deslocamento do corte: delay do stream + tempo de reação do chat |

Quando dispara: o spike entra numa fila `pending` e o take só é cortado `after` segundos depois
(o clip precisa existir na gravação). O corte (`cut()`) usa o **fim do último segmento
completo como âncora do relógio** para converter horário de parede em posição do stream, concatena
os segmentos que intersectam `[spike-before, spike+after]` (padrão 15 s antes / 20 s depois) com
`-c copy` e grava `live/spike_<n>.mp4` + linha em `live/spikes.md` (UTC, offset, msgs/s, palavras
mais ditas na janela — `top_words` ignora stopwords).

### 3.4 Encerramento e voids
- Ctrl+C ou fim de `--minutes`: o recorder recebe `q` no stdin (grace 20 s → terminate → kill),
  os spikes pendentes são cortados, e `live/live.json` registra `{url, spikes, minutes, ended, ended_ts}`.
- **Void**: captura que terminou **sem nenhum vídeo** (nenhum spike, nenhum corte). `voids.py`
  a classifica e apaga o job (duas tentativas com 2 s de espera se um arquivo estiver bloqueado
  por um player), registrando `{job, mb, files, reason, url, channel, title}` em
  `dashboard/state/discarded.json`. `--keep-void` preserva.
- Um run **com** chat mas sem spike pode ainda gerar candidatos por loudness/heatmap — por isso
  a decisão de void fica no fim do pipeline (`post_capture`), nunca no `live.py`.

### 3.5 Quick-clips ao vivo (clipping em tempo real)

Cada take de pico que sai do `cut()` com sucesso dispara, **na hora**, o motor completo sobre
esse take de ~35s — sem esperar o fim da gravação:

1. `live.py` faz spawn **detached** de `analyze_creation.py spike_N.mp4 1 2 <options>` (log em
   `live/quick_clips.log`); gravação e chat continuam rodando.
2. `moments.py --durations 30` transcreve o take (segundos no CPU) e forma 1 candidato;
   `rank_llm.py` re-nota com IA e escreve o hook PT-BR — **sem LLM ou com falha, segue na
   heurística** (título-rascunho da fala, `title_review: true`).
3. `cut.py` renderiza o vertical com o preset de captura (react-br → react + PT por sentença).
   **React/facecam sem webcam no frame? Um retry com layout blur** salva o clipe.
4. O clipe aparece na aba Cortes em ~3–5 min após o pico, num job próprio
   (`spike-N-<sha>`, com progresso visível na aba Criar cortes via `creation.json`).

Off-switch: env `GTAVI_QUICK_CLIPS=0`. O preset vem do que você salvou na aba **Ajustes**
(`capture.preset`); preset renomeado degrada para o default. A pós-captura de fim de gravação
continua integral — quick-clips são aditivos (velocidade), não substitutos (análise global).

---

## 4. Fase 2 — Pós-captura e orquestração (`post_capture.py` + fila do painel)

É o maestro da segunda metade do Fluxo A. `post_capture.run(live_jd, ...)` executa, em ordem:

1. **CFR rebuild** (`common.rebuild_cfr`): concatena todos os `seg_*.ts` em
   `live/source_cfr.mp4` — `fps=30`, `setpts=PTS-STARTPTS`, áudio `aresample=async=1:first_pts=0`,
   x264 `veryfast crf18`. Motivo: segmentos HLS resetam timestamps e podem mudar de resolução no
   meio; um timeline CFR estável é pré-requisito para todo crop/face-track/zoom seguinte.
2. **Reconstrução do chat** (`build_chat`): converte os timestamps de parede do
   `chat.twitch.jsonl` para a **posição na timeline reconstruída**, usando `segments.csv`
   (fallback YouTube: reuse do `videoOffsetTimeMsec`). Resultado: `live/chat.json`, formato que
   o `moments.py --chat` entende.
3. **Análise**: `moments.py source_cfr.mp4 --top N --min 60 --max 90 --chat chat.json` (fase 3).
4. **Re-rank**: `rank_llm.py` (fase 4) — se não houver LLM configurado, exit 3 é tratado como
   *skip silencioso* ("seguindo com a heurística"), nunca como falha.
5. **Ordenação**: candidatos por `llm.total_score` (quando houver re-rank) ou `score` heurístico;
   pega o top N.
6. **Cortes**: `cut.py --pick <n> --preset <preset> --title <hook_title ou draft> --zoom/--title-style
   do preset --lang <base>` para cada pick. Se um corte `react` falha, há retry automático com
   layout `blur` (troca zoom `face/both` → `game`).
7. **Marcação para revisão**: cada `clip_<n>.json` recebe `title_review: true` e `title_draft` —
   o título-rascunho vem da própria fala (`draft_title`: primeira frase, ofensas mascaradas,
   8–86 chars; mínimo genérico "Momento N de GTA 6").
8. **Variantes**: `translate.py --pick n --langs <extras>` (fase 6) e **export**:
   `pack.py --pick n --export out/<slug>/` (fase 7).
9. **Estado**: grava `jobs/<live>/monitor.json` — sucesso:
   `{"preset", "source", "moments_job", "clips": [{n, title, review, variants, export, clip}]}`;
   falha: `{"preset", "error", "failed_ts"}`. Registros de falha **continuam re-executáveis**;
   sucesso fresco (monitor.json mais novo que o último segmento) faz `already_processed` pular.

A fila do painel (abaixo, §11) roda exatamente esta função, uma captura por vez.

---

## 5. Fase 3 — Análise de momentos (`moments.py`)

**Comando:** `python scripts/moments.py <url-ou-arquivo> --durations 30,60,150 --top 8`
(modo tiers) ou `--top 8 --min 60 --max 90` (janela livre). `--max-minutes 90` limita o
orçamento de download/transcrição aos primeiros N minutos.

### 5.1 Download inteligente (`fetch`)
- Duração ≤ `--max-minutes`: download integral (formato `bv*≤1080 mp4 + ba m4a`).
- Senão: `--download-sections "*0-N"` com `Referer: https://www.youtube.com/` e User-Agent de
  navegador (sem esses headers o googlevideo assinado responde 403). Fallback: baixa o vídeo
  inteiro e corta localmente com `-c copy`.

### 5.2 Loudness por segundo (`audio_rms`)
ffmpeg extrai PCM mono 16 kHz (`audio.wav`); o script lê em **chunks de 10 min** (um VOD de 4 h
tem ~900 MB de PCM — carregar inteiro derrubava máquinas com 8 GB em swap). Por segundo:
`dB = 20·log10(rms)`. O teste de unidade garante que o chunking é **exatamente** equivalente à
matemática de array inteiro.

### 5.3 Transcrição com retomada (`transcribe`)
- **Whisper** (`faster-whisper`): modelo de `GTAVI_WHISPER_MODEL` (default `small`; whitelist
  `tiny/base/small/medium/large-v3`), `cuda float16` se houver GPU (fallback CPU int8),
  `word_timestamps=True`, `vad_filter=True`, `beam_size=1`.
- O áudio é fatiado em **peças de 600 s** com 2 s de overlap; cada peça salva
  `transcribe/NNNN.json` — uma análise interrompida **retoma de onde parou** sem re-chamar o
  modelo, e o pico de RAM é uma peça, não o áudio todo.
- Palavras na fronteira das peças são descartadas do span que não é delas (nem duplica nem corta).
- O idioma é fixado na primeira peça confiante (`language_probability > 0.8`).
- Consolidado: `transcript.json` `{"language", "prob", "words": [{"w","s","e"}]}`.

### 5.4 Os quatro sinais de momento

| Sinal | Como é medido | Peso no score |
|---|---|---|
| **Loudness** | excesso de dB sobre a mediana de **fala** numa janela ±150 s (o 20% mais baixo é tratado como silêncio e não entra no baseline — evita "grito falso" depois de silêncio longo) | `1.0 × z(excesso).clip(0)`, suavizado |
| **Hype words** | regex sobre o transcript — lista EN (`no way, omg, let's go, insane…`) + **PT-BR** (`meu deus, não acredito, caralh*, nossa, mano, pqp, kkk, rsrs…`) — somando +1 no segundo da palavra | `0.8 × z(hype).clip(0)` |
| **Most replayed** | heatmap oficial do YouTube (`info["heatmap"]` do yt-dlp) pintado segundo a segundo | `2.0 × z(heat).clip(0)` (peso 0 se não há heatmap) |
| **Chat spikes** | mensagens por segundo (do `--chat`: replay do YouTube ou `chat.json` reconstruído), com peso 2 para `CHAT_HYPE` (LMAO, KEKW, CLIP, 💀😂🔥) | `1.5 × z(chat).clip(0)` (peso 0 se não há chat) |

```
score = 1.0·loud + 0.8·z(hype) + w_heat·z(heat) + w_chat·chatz   →  dampen_edges(score, n)
```

**`dampen_edges`**: em vídeos ≥ 10 min, os primeiros/últimos `min(240, n/10)` segundos têm o
score reduzido a 30% — cumprimentos e despedidas ("boa noite!" explodindo o chat) não são momentos.

### 5.5 Formação dos candidatos
- **Modo `--durations` (tiers)**: um take por tier para cada pico escolhido. Picos a menos de
  `1.1 × maior_tier` um do outro são "o mesmo momento" — só o melhor fica. `--top` aqui conta
  **momentos**, não takes.
  - **`window_for`**: o pico fica a **~40% do início da janela** (o hook cedo), as bordas são
    encaixadas em fala real (início = 1ª palavra −0.3 s, fim = última +0.6 s), uma abertura de
    sentença limpa (pausa > 0.8 s) perto do início é preferida, e toda janela garante no mínimo
    70% do target.
- **Modo padrão (`--min/--max`)**: janela `[pico − 0.55·max, + max]` encaixada em fala, com
  dedup por ocupação (um array booleano de segundos usados — nenhum momento sobreposto).
- Cada candidato ganha um campo **`why`** — "loudness spike", "hype words", "most replayed 0.83",
  "chat spike" ou "overall energy" — para o `candidates.md` explicar o porquê de cada pick.

### 5.6 Saída
- `jobs/<id>/candidates.json`: `{"source", "info", "duration", "candidates": [{n, start, end,
  peak, tier?, score, why, text}], "signals": {heatmap, chat, words}, "tiers"}`.
- `jobs/<id>/candidates.md`: versão legível (minuto inicial/final, score, motivo, trecho de fala).

---

## 6. Fase 4 — Re-rank por IA (`rank_llm.py`)

Ideia central (reimplementada do supoclip): **as heurísticas dão recall, o LLM dá precisão**.
O script nunca reordena `candidates.json` — grava os scores por `n` e os consumidores
(`post_capture`, `analyze_creation`, painel) reordenam por `total_score`.

**Configuração:** qualquer endpoint OpenAI-compatível (GLM/Zhipu, OpenAI, DeepSeek, NVIDIA NIM,
Ollama local…). `env GTAVI_LLM_BASE/KEY/MODEL` vence o `dashboard/state/settings.json` (aba
**Ajustes** do painel). O `base` precisa incluir o path de versão (`https://api.openai.com/v1`).
Exit codes: `0` ranked; `3` sem LLM configurado (skip); `1` falha (arquivo intocado).

### 6.1 O que é enviado
Até 12 candidatos (`--max`, default 12) ordenados pelo score heurístico, com ids `c1..cN`. O
prompt do usuário, na ordem:
1. `Source: <título> — <canal>`
2. **Contexto de performance** (`performance.context()`) — histórico real de clips que *você*
   postou (ver §12). Só ativo com **≥ 3 posts com métricas**; antes disso o ranking fica
   signal-driven.
3. **Contexto de feedback do operador** (`spike_feedback.context()`) — picos que você marcou
   como "falso positivo" ou "confirmado bom" nos cards da aba Lives. Ambos entram **apenas como
   desempate em texto — nunca como score**; a ordenação heurística não é tocada.
4. Lista de candidatos (`id | janela | score de sinal | motivos | texto falado`).
5. **Transcript com timestamps por sentença** (janela ±15 s de folga, máx. 30 linhas; sentenças
   separadas por pausa > 0.8 s ou pontuação). Takes do mesmo pico compartilham o transcript uma
   única vez.

### 6.2 O que o modelo devolve (contrato JSON)
- **4 notas de 0 a 25**: `hook_score` (o primeiro segundo prende?), `engagement_score`,
  `value_score`, `shareability_score`. O **`total_score` é recomputado localmente como soma
  simples** dos quatro (clamped 0–25) — o número total do modelo nunca é confiado.
- **`hook_type`**: exatamente um de `pergunta, afirmação, estatística, história, contraste, nenhum`.
- **`hook_title`**: 3–9 palavras **em PT-BR**, alicerçado no que é dito de verdade (traduzido se
  a fala não é PT), sem hashtag/emoji/clickbait que traia o clipe. Limpo para ≤ 10 palavras / 64 chars.
- **`adj_start`/`adj_end`** (refino de janela): o modelo pode mover cada borda até ±15 s — mas o
  script valida (tolerância de 5 s para rejeitar relógio alucinado) e impõe limites de duração
  (`≥ max(15 s, 0.5·dur)` e `≤ min(dur+45, 180 s)`). A janela original é preservada em
  `signal_start`/`signal_end`.
- **`summary`**: uma frase sobre a fonte, gravada no `llm_summary` do `candidates.json`
  (`{model, ranked_at, selected, windows_refined, summary}`).

**Robustez:** retry ×2 com 2 s de espera entre tentativas; parser tolerante (fences ```json,
comentários `//`, vírgulas pendentes); ids desconhecidos são ignorados (nenhum id válido → exit 1
sem tocar no arquivo). `--dry-run` imprime o prompt e não chama o LLM.

---

## 7. Fase 5 — Corte vertical (`cut.py`)

**Comando:** `python scripts/cut.py jobs/<id> --pick 2 --layout react --cam auto --lang pt --zoom face --title "…"`

Aqui o momento escolhido vira um MP4 1080x1920. Constantes fixas: tela 1080x1920; no layout
`react` o painel da webcam ocupa `TOP=730` px e o gameplay `GAME_H=1190` px (a "costura" fica em
y=730); zoom de rosto `1.18x`, de gameplay `1.10x`.

### 7.1 Escolha da janela
`--pick N` lê o candidato `n=N` do `candidates.json`; `--start/--end` sobrescrevem a janela.

### 7.2 Facecam automática (`facecam.py`)
Para `react`/`facecam` com `--cam auto`: o detector **YuNet** (ONNX baixado uma vez para
`%LOCALAPPDATA%/gtavi-claudecode/models/`) acha o rosto em 12 frames; o retângulo da webcam é
deduzido por mapa de bordas mediano (snap de até 80 px), com saneamento de proporção. Modos:
- `cam` — moldura da webcam detectada (crop por **frações** `x:y:w:h`, imune a mudanças de
  resolução no meio do stream);
- `face` — sem moldura visível: crop que **segue o rosto** frame a frame;
- `none` — sem rosto: o `react` **pula** o momento (é o layout que perde), o `facecam` cai
  silenciosamente para `blur`.
- O `react` ainda valida que **fora da webcam há gameplay** (histograma HSV + borda reta); se
  não há, o momento é pulado com motivo.

### 7.3 Os quatro layouts
| Layout | Composição |
|---|---|
| `fill` | jogo em tela cheia (crop central) |
| `blur` | fundo do próprio jogo borrado (`boxblur 24:2`) + jogo centralizado 80 px acima do centro |
| `react` | **vstack**: webcam no topo (730 px) + gameplay embaixo (1190 px); `game_offset` escolhe o x do crop do gameplay por análise de pixels estáticos (nunca mostrar a webcam duas vezes) |
| `facecam` | cam no topo (38% da altura) + jogo embaixo |

### 7.4 Legendas karaoke (`--captions word|bubble|sentence|none`)
- **`word`** (padrão): grupos de até 3 palavras (quebra em pontuação ou pausa > 0.6 s), Arial
  Black 92 em maiúsculas; a **palavra ativa** ganha cor de destaque (`cap_highlight`,
  amarelo `#FFE500` por padrão) e a primeira palavra do grupo tem um "pop" (`fscx 112→100`).
- **`bubble`**: caixa com o grupo inteiro por vez.
- **`sentence`**: uma bolha por sentença (pausa > 0.8 s ou pontuação). Com `--lang` diferente do
  áudio, cada sentença é **traduzida como um todo** e quebrada em pedaços de ≤ 4 s / ≤ 48 chars
  com timing proporcional — sem sincronia de palavra falsa.
- `resolve_lang`: `--lang pt` normaliza `pt-br/ptbr→pt`; `--lang` igual ao idioma do áudio
  mantém o word-by-word nativo; `--captions none` vence tudo.
- **Mascaramento** (sempre, salvo `--allow-profanity`): slurs e palavrões EN+PT trocados por
  `p****` (lista PT: `porr*`, `caralh*`, `merd*`, `fod*`, slurs sempre mascarados).

### 7.5 Título, hook e banda
- **`--title`** (layout react): caixa Arial Black centrada na costura y=730 com **banda branca**
  (drawbox de `linhas×78+44` px) — o visual de reaction clássico. `--title-style outline` desenha
  sem banda. `--title-secs 0` = o clipe inteiro.
- **`--hook`**: texto de abertura de 0 a 2.6 s com fade e zoom-in suave (descartado se houver título).
- A paleta (`--cap-color/--cap-highlight/--cap-outline/--box-fill/--box-text`) vem do preset
  (§presets.json) e é salva no sidecar para o `pack.py` reproduzir a banda na thumb.

### 7.6 Zoom, áudio e encode
- **`--zoom face|game|both`**: punch-in lento via `zoompan` (`face` 1.18x no painel da cam,
  `game` 1.10x no gameplay) com `fps=30` explícito — contorna gravações VFR que estouravam
  "Cannot allocate memory".
- **Áudio**: `loudnorm=I=-14:TP=-1.5:LRA=11` — o padrão das plataformas (–14 LUFS).
- **Encode**: x264 `medium crf20 yuv420p`, AAC 160k 48 kHz, `+faststart`; `-t` explícito corrige
  o esticamento de PTS de segmentos VFR.

### 7.7 Saída
- `edits/clip_<n>.mp4` — o clip final.
- `edits/clip_<n>.ass` — legendas queimadas (arquivo de referência).
- `edits/clip_<n>.json` — sidecar completo (`start, end, layout, hook, title, cam, zoom, palette,
  lang_source/target, captions_mode, sentences_cache, source_url/channel/title`): é o que o
  painel e `translate.py`/`pack.py` leem para re-cortar sem adivinhar.
- `log.md` (raiz) — append do hook/título usado (regra: não repetir hook em 7 dias; o painel
  mostra as "frases de abertura recentes").

---

## 8. Fase 6 — Multilang (`translate.py`)

**Comando:** `python scripts/translate.py jobs/<id> --pick 2 --langs en,es --voice`

Requer `edits/clip_<n>.json` (corte primeiro) — herda layout, cam, hook e paleta do sidecar.

1. **Tradução por sentença com timing**: cada sentença da janela vira um evento
   `{"text","s","e"}` traduzido como um todo (nada de espalhar palavra por palavra). Provedores,
   em cascata: **Google** (4 tentativas com 5 s de espera) → **MyMemory** (fallback) → texto
   original. Cache em `edits/translate_cache.json` **isolado por idioma**, e fallback não traduzido
   **nunca é cacheado** (um dia o Google volta e a qualidade melhora sem sujar o cache).
2. **Render**: mesmo filtro de vídeo do `cut.py`; grava `edits/clip_<n>_<lang>.mp4/.ass/.json`
   (sidecar com `variant_of: "clip_<n>"` para o painel agrupar variantes).
3. **`--voice` (hook de voz stock)**: `edge-tts` sintetiza o hook na voz padrão do idioma
   (`pt-BR-AntonioNeural`, `es-ES-AlvaroNeural`, …) — **nunca clona a voz do streamer**. O
   original vira bed a 20% de volume nos primeiros 2.5 s, o TTS entra acelerado até 2× se
   preciso, com fade-out, e tudo passa pelo mesmo `loudnorm -14 LUFS`.
4. **`--dub` (dublagem completa)**: um MP3 por sentença (`edits/dub_<lang>_<i>.mp3`); cada fala
   é acelerada (máx 2×) para caber no slot até a próxima sentença, o original é reduzido a 35%
   de volume (`duck`) e o mix é re-normalizado.
5. **`edits/clip_<n>_captions.md`**: bloco `## <lang>` por idioma com o texto de post para
   **X** (sem hashtags), **Instagram** (4 hashtags) e **TikTok/Shorts** (3 hashtags).

---

## 9. Fase 7 — Empacotamento (`pack.py`)

**Comando:** `python scripts/pack.py jobs/<id> --pick 2 --platforms tiktok,reels,youtube --export out/<slug>/`

1. **`thumb_<n>.jpg` (1080x1920)**: pega um **frame antes da primeira legenda queimada** (usa o
   `sentences_cache` do sidecar; entre 0.05 e 0.8 s dentro do clip) para a capa não sair com
   texto duplicado, e queima a **mesma banda branca do título** (mesma matemática: 78 px/linha +
   44 px, fonte Arial Black → Arial), com a paleta salva pelo `cut.py`.
2. **Thumbs por plataforma** (`--platforms`): `tiktok` 1080x1920 · `reels` 1080x1920 com a banda
   travada na **zona segura 4:5** · `youtube` 1280x720 (cover). Para clips react no YouTube, a
   posição da costura **queimada no vídeo** é recalculada por escala para a banda cobrir a faixa certa.
3. **`legendas.md`**: título, `Crédito: <canal>`, URL da fonte e **bloco de post por plataforma**
   (X, Instagram, TikTok, YouTube Shorts — com hashtags e crédito prontos para colar).
4. **`--export DIR`**: copia **apenas** `clip.mp4`, `thumb.jpg`, `legendas.md` (+ thumbs de
   plataforma) para `out/<slug>/` — a pasta limpa que você posta. Intermediários ficam em `jobs/`.

---

## 10. Fases 8 e 9 — Títulos (`titles.py`) e portão de qualidade (`check.py`)

### 10.1 Títulos
`python scripts/titles.py jobs/<id>` gera até **3 opções por take**, todas derivadas da própria
fala (nada inventado): (a) **fato direto** — a frase mais forte traduzida; (b) **frame de
curiosidade** — "O momento em que … — olha o que aconteceu"; (c) **frame do canal** —
`fato — <canal>`. Cada opção é ranqueada por comprimento, hype words e pontuação expressiva.
O `titles.md` abre com o aviso *"Revise antes de postar: tradução de máquina pode escorregar"*
e as sugestões são gravadas em `candidates.json` (`title_suggestions`).

### 10.2 Portão de qualidade
`python scripts/check.py jobs/<id>/edits/clip_1.mp4 --campaign campaigns.md --caption "legenda do post"`

Checagens PASS/FAIL (qualquer FAIL → exit 1):
resolução **1080x1920**, fps entre 29–31, tem áudio, duração dentro da campanha, **termos
banidos** no hook/legenda (leak, play early, free download, crack, piracy…), **hashtags
exigidas** e **mention exigida** presentes. O formato de `campaigns.md`:

```
min_seconds: 15
max_seconds: 60
required_hashtags: #GTA6 #fyp
required_mention: @canal
```

Sem `--campaign`, o check roda só o técnico e avisa que não é uma checagem de regras.

---

## 11. O painel como maestro (`dashboard/server.py`)

`python dashboard/server.py` abre [http://127.0.0.1:8787](http://127.0.0.1:8787) (local only).
É um servidor HTTP da stdlib que **lê os mesmos arquivos do motor** e adiciona orquestração.

### 11.1 Três threads de fundo
| Thread | Intervalo | Papel |
|---|---|---|
| `capture_watcher` | 10 s | capturas encerradas há < 48 h com segmentos e sem `monitor.json` fresco entram na fila de pós-captura (com preset/top de `settings.json`) |
| `pipeline_worker` | 5 s | reconcilia a fila (processo morto / `monitor.json` escrito / 6 h de stale) e spawna o próximo `post_capture.py` — **uma captura por vez** |
| `void_pruner` | 60 s | descarta **voids** após grace de **15 min** (card "sem-nada" com contagem regressiva antes) e apaga capturas já processadas após **24 h** de retenção; tudo logado em `discarded.json` |

Processos filhos (`live.py`, `post_capture.py`, `analyze_creation.py`) são spawados **detached**
(no Windows, `CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS | CREATE_BREAKAWAY_FROM_JOB`) —
fechar o painel não mata uma gravação.

### 11.2 Abas e rotas principais
Sidebar em grupos (redesign v2): **Início** › Visão geral · **Conteúdo** › Cortes (`#/conteudo/cortes`),
Criar cortes, Desempenho · **Lives** › Monitoradas, Encontrar lives · **Publicar** › YouTube ·
**Sistema** › Armazenamento, Ajustes. Rotas em hash-path com alias legado (`#clips` →
`#/conteudo/cortes`); modo recolhido (ícones) e drawer no celular. Auto-refresh: estado a cada
**4 s**, audiência a cada **30 s**, armazenamento a cada **60 s**, busca de lives a cada **5 min**
(só com a aba visível). Spec de design e auditoria de acessibilidade em `docs/DESIGN_SYSTEM.md`
e `docs/AUDITORIA_ACESSIBILIDADE.md`.

Rotas que tocam o motor:
- `POST /api/watch` — spawna `live.py` na URL (monitorar uma live).
- `POST /api/streams/refresh` — roda `streams.py` e devolve as seções ordenadas PT primeiro.
- `POST /api/create/analyze` / `analyze-file` — spawna `analyze_creation.py`
  (Fluxo B orquestrado: `moments → rank_llm → cut` com progresso por estágio em
  `creation.json`: "transcrevendo e analisando o vídeo" → "re-rank dos momentos" →
  "gerando os cortes"; modo `auto` corta os melhores, `manual` só analisa).
- `POST /api/rerank` ("Reordenar com IA") — roda `rank_llm.py`; sem LLM configurado devolve 400
  com "configure o LLM na aba Ajustes".
- `POST /api/recut` (botão **Ajustar**) — re-corta um clip com novo título/hook/start/end
  (trim livre; extensão máxima de 60 s além de cada borda original).
- `POST /api/split-moment` — divide um momento ao segundo (`at`) ou salva um trecho como novo
  momento (`start/end`) direto no `candidates.json`.
- `POST /api/lang` — variantes multilang (`translate.py`).
- `POST /api/perf` — registra métricas de clips postados (ver §12).
- `POST /api/settings` — merge **parcial** (`llm`, `whisper_model` com perfis fast/balanced/
  accurate, `ytdlp_cookies`, `capture: {preset, top 1–12}`); POST parcial nunca apaga config salva.
- `GET /api/state` — todo o estado que a SPA renderiza (cards de lives, cortes, criação).

Os cards da aba **Lives** derivam o estado no servidor: `gravando → suspeito → processando →
pronto` (ou `erro` / `sem-nada` / `offline`), com anomalias ("sem bloco novo há mais de 90 s",
"chat sem msgs novas", "sem pico marcado") para você perceber captura zumbi antes de perder horas.

### 11.3 `analyze_creation.py` (a rota interativa do Fluxo B)
Recebe `source top max_minutes options-json`; escreve progresso em `creation.json` (best-effort,
nunca bloqueia); roda `moments.py`, depois `rank_llm.py` (se `options.rerank` e LLM configurado),
e no modo `auto` corta cada candidato ordenado por `llm.total_score || score` com o layout/
captions/lang escolhidos no painel — um take falho não aborta os demais.

---

## 12. O loop de aprendizado (por que o motor melhora com o uso)

Dois arquivos de estado alimentam o prompt do re-rank como **desempate em texto**:

1. **`dashboard/state/performance.json`** (`performance.py`): cada clip que você posta é
   registrado com plataforma e métricas reais (views/likes/comentários/compartilhamentos).
   A partir de **3 posts com métricas**, `performance.context()` monta um histórico PT-BR —
   mediana de views por tipo de hook, por faixa de duração (`<20s`, `20-35s`, `35-50s`, `>50s`)
   e os 5 melhores clips — injetado no topo do prompt (máx. 2000 chars, com a instrução explícita
   "use apenas para desempatar e enviesar o tipo de hook e a duração — nunca para inventar conteúdo").
2. **`dashboard/state/spike_feedback.json`** (`spike_feedback.py`): seus vereditos `ok` / `fp`
   (falso positivo) sobre spikes nas cards da aba Lives (últimos 500) entram como "peneire
   candidatos semelhantes a estes / padrões semelhantes são desejáveis".

Nenhum dos dois altera scores heurísticos — a ordenação por sinais fica intocada.

---

## 13. Presets (`scripts/presets.json`)

`--preset` injeta defaults no `cut.py`/`translate.py`; flags explícitas sempre vencem. O default
do projeto é `react-br`.

| Preset | layout | legendas base | variantes | zoom | para quê |
|---|---|---|---|---|---|
| `react-br` | react | `pt` (sentença traduzida) | `en` | face | reaction PT-BR com zoom no rosto — o preset padrão de captura |
| `original` | react | original (word nativo) | — | off | sem tradução nenhuma |
| `blur-multi` | blur | `pt` | `pt,en` | game | fundo borrado + múltiplos idiomas |

Todos com `cam auto`, `title_style box`, paleta branca/amarela padrão (`#FFFFFF` / `#FFE500` /
`#000000`), `thumb on`, `allow_profanity off`.

---

## 14. Melhores momentos — compilação horizontal para YouTube (`compile.py`)

O motor produz clipes **verticais** individuais; o YouTube também quer o formato clássico de
canal: **um vídeo horizontal único** reunindo os melhores momentos da live. O `compile.py` faz
exatamente isso a partir do `candidates.json` que já existe — sem nova transcrição, render
direto na fonte analisada.

### O que ele produz

```
jobs/<id>/compile/plan.json        momentos escolhidos, em ordem cronológica
jobs/<id>/compile/melhores.mp4     o vídeo (1920x1080, -14 LUFS, faststart) — sob jobs/ de
                                   propósito: é o caminho que o editor de envio do YouTube aceita
jobs/<id>/compile/thumb.jpg        capa 1280x720 com a banda de título (Arial Black)
jobs/<id>/compile/legendas.md      título + crédito + descrição YouTube com timestamps
out/<id>-melhores/                 kit postável (cópia dos 3 arquivos acima)
jobs/<id>/compile.json             progresso {status, stage, done, total, output, error}
```

### Como ele escolhe e monta

1. **Seleção** — manual (`"moments": "3,5,8"`) ou automática: ordena por
   `llm.total_score || score` e usa um *greedy fit* contra a duração alvo (5/10/15/20 min);
   o primeiro momento sempre entra, então fonte curta ainda compila. Janela de cada momento é
   aparada a **120 s em torno do pico**. O vídeo final é sempre **cronológico**.
2. **Render por momento** — um ffmpeg por momento, codificação única (`medium crf 20`,
   aac 160k 48 kHz, parâmetros idênticos em todos): escala/corta para 1920x1080, `fade`
   in/out de 0,5 s pelo preto (vídeo e áudio), `loudnorm -14 LUFS`.
3. **Webcam** — a webcam do streamer **já vem queimada no vídeo da stream** (OBS compõe, em
   Twitch, YouTube e Kick). Um PiP solto duplicaria a pessoa na tela; por isso o PiP do
   `compile.py` **cobre e destaca a região da câmera** detectada pelo `facecam.find_cam()`
   (mínimo 480x270, borda clara, cresce se a cam original for maior). Sem face estável = só
   gameplay.
4. **Concat** — demuxer `concat` com `-c copy` (sem segunda passada de encode) + `+faststart`.
   Um momento que falha é **pulado** e a compilação segue com os demais.
5. **Capa** — frame a 40% do momento de melhor score (perto do pico) + banda de título
   (`pack.draw_band_filter`). Título: opção `--title` ou fallback
   `MELHORES MOMENTOS — <canal>`.

### Como dispara

- **Painel** (caminho principal): aba **Criar cortes** → painel do projeto (ou o registro do
  job no histórico) → **“🎬 Melhores momentos (vídeo de YouTube)”** → duração alvo, destacar
  webcam, título opcional, momentos específicos opcionais → barra de progresso
  (`renderizando momento 3/8`) → quando fica **pronto**, `▶ Prévia` e **Enviar ao YouTube**
  (pula para a aba YouTube com o arquivo, título e descrição pré-preenchidos como *vídeo*).
- **CLI**:
  ```
  python scripts/compile.py jobs/<id> '{"target_min": 10, "pip": true}'
  python scripts/compile.py jobs/<id> '{"moments": "3,5,8", "title": "OS MELHORES DO DIA"}'
  ```

Rota do painel: `POST /api/create/compile` → spawn destacado do `compile.py` (sobrevive a
restart, log em `dashboard/logs/compile_<job>.log`); o estado vem no `/api/state` junto do
resto (`creation[].compile`), reconciliado por pid-vivo como o `creation.json`.

Custo honesto: ~1 min de render por minuto compilado (encode único, sem transcrição).

---

## 15. Mapa completo dos artefatos

| Caminho | Quem produz | O que é |
|---|---|---|
| `jobs/<id>/live/seg_*.ts` + `segments.csv` | `live.py` | captura em blocos de 60 s |
| `jobs/<id>/live/chat.twitch.jsonl` / `chat.live_chat.json*` | `live.py` | chat cru (IRC / replay) |
| `jobs/<id>/live/spike_<n>.mp4`, `spikes.md` | `live.py` | takes de spike + tabela |
| `jobs/<id>/live/live.json`, `recorder.json` | `live.py` | fim da captura / pid vivo |
| `jobs/<id>/live/source_cfr.mp4` | `post_capture` | CFR rebuild (fonte estável) |
| `jobs/<id>/live/chat.json` | `post_capture` | chat mapeado à timeline |
| `jobs/<id>/info.json`, `audio.wav`, `transcribe/*.json`, `transcript.json` | `moments.py` | metadados, PCM, transcrição retomável |
| `jobs/<id>/candidates.json` / `candidates.md` | `moments.py` (+`rank_llm.py`) | momentos, scores, `why`, LLM |
| `jobs/<id>/edits/clip_<n>.mp4/.ass/.json` | `cut.py` | clip 1080x1920 + legendas + sidecar |
| `jobs/<id>/edits/clip_<n>_<lang>.mp4/.ass/.json` | `translate.py` | variantes multilang |
| `jobs/<id>/edits/clip_<n>_captions.md` | `translate.py` | post text por idioma/plataforma |
| `jobs/<id>/edits/thumb_<n>*.jpg` | `pack.py` | thumb vertical + por plataforma |
| `jobs/<id>/edits/clip_<n>_legendas.md` | `pack.py` | crédito + post por plataforma |
| `jobs/<id>/edits/translate_cache.json` | cut/translate/titles | cache de tradução por idioma |
| `jobs/<id>/titles.md` | `titles.py` | 3 títulos por take |
| `jobs/<live>/monitor.json` | `post_capture` | estado do pipeline pós-captura |
| `jobs/<id>/creation.json` | painel/`analyze_creation` | estágio da análise interativa |
| `jobs/<id>/compile/plan.json` + `melhores.mp4` + `thumb.jpg` + `legendas.md` | `compile.py` | compilação horizontal dos melhores momentos |
| `jobs/<id>/compile.json` | painel/`compile.py` | progresso da compilação |
| `out/<slug>/clip.mp4 + thumb.jpg + legendas.md` | `pack.py --export` | **o que você posta** |
| `out/<id>-melhores/` | `compile.py` | kit postável da compilação |
| `log.md` (raiz) | `cut.py` | histórico de hooks (não repetir em 7 dias) |
| `dashboard/state/settings.json` | painel | LLM, Whisper, cookies, preset/top de captura |
| `dashboard/state/performance.json` | `performance.py` | métricas de clips postados |
| `dashboard/state/spike_feedback.json` | `spike_feedback.py` | seus vereditos de spike |
| `dashboard/state/discarded.json` | `voids.py` | log de voids/retenção (últimos 60) |
| `dashboard/state/watchlist.json`, `pipeline_queue.json` | painel | monitoramentos ativos, fila sequencial |

---

## 16. Walkthroughs end-to-end

### Fluxo A — live agora, um comando
```
python scripts/monitor.py "https://twitch.tv/<canal>" --minutes 60 --top 3 --preset react-br
```
1. `live.py` grava em blocos de 60 s e lê o chat via IRC anônimo.
2. Cada spike (janela 10 s ≥ 3× baseline de 300 s, ≥ 8 msgs, cooldown 45 s) gera um
   `spike_<n>.mp4` [15 s antes, 20 s depois, +8 s de lag].
3. A captura termina → `post_capture.run()`: CFR rebuild → `chat.json` → momentos
   (loudness + hype + chat) → re-rank LLM (se configurado) → corta os top 3 com preset
   `react-br` (webcam + gameplay, legendas PT por sentença, zoom no rosto, título na costura,
   –14 LUFS) → variante EN → thumb + `legendas.md` → exporta em `out/<live-do-canal-clipN>/`.
4. Sem cortes? O job é void e é descartado (log em `discarded.json`).
5. Títulos vêm da própria fala, **marcados `title_review: true`** — revise antes de postar.

### Fluxo B — vídeo pronto, pela CLI
```
python scripts/moments.py "https://www.youtube.com/watch?v=..." --durations 30,60,150 --top 4
python scripts/rank_llm.py jobs/<id> --top 5            # re-rank IA (opcional)
python scripts/cut.py jobs/<id> --pick 2 --layout react --lang pt --zoom face
python scripts/titles.py jobs/<id>                      # 3 títulos por take
python scripts/pack.py jobs/<id> --pick 2 --platforms tiktok,youtube,reels --export out/<slug>
python scripts/check.py jobs/<id>/edits/clip_2.mp4 --campaign campaigns.md --caption "..."
```

### Fluxo B — pelo painel
Aba **Criar cortes** → cole a URL (ou escolha um vídeo já em `jobs/`) → escolha layout/
legendas/idioma/duração → `analyze_creation.py` mostra o progresso por estágio → grade de
momentos com score e hook → **Reordenar com IA**, **Dividir ao meio**, **Salvar como novo
momento**, **Ajustar** (re-corte fino) → render → cortes aparecem na aba **Cortes** com
rascunho de post para TikTok/Shorts/Reels.

### Compilação — melhores momentos para YouTube
Depois de qualquer análise (Fluxo A ou B): painel do projeto → **“🎬 Melhores momentos”** →
duração alvo 10 min → gerar → prévia → **Enviar ao YouTube** (vídeo, título e descrição com
timestamps pré-preenchidos). Pela CLI: `python scripts/compile.py jobs/<id> '{"target_min": 10}'`.

---

## 17. Limites honestos do motor

- **Post é manual** por decisão de projeto — nada sai da sua máquina sem você.
- Renders são CPU (x264 `medium`): um take de 60 s leva ~4–6 min; transcrição de vídeo longo
  é pesada (`GTAVI_WHISPER_MODEL=base` acelera, `accurate` → medium quando a qualidade importa).
- Tradução depende de serviço externo (Google → MyMemory de reserva); quando o Google está
  bloqueado, a qualidade cai um degrau até liberar (cache não é poluído pelo fallback).
- Direitos: só corte dentro de campanha ou com permissão do criador — crédito no post é
  gerado automaticamente, mas usar é sua responsabilidade.

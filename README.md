# clipdesk

**Do live ao corte pronto, sem Docker.**
Um motor local de clipping que assiste à live, entende o chat, encontra os melhores momentos,
corta no vertical, legenda, traduz e entrega o pacote de post — tudo com arquivos na sua máquina.

> Projeto original: `gtavi-claudecode`. Este README documenta o motor como ele funciona hoje,
> incluindo o sistema de cortes, a compilação de melhores momentos e os presets.
>
> **Este repositório distribui o motor e o backend.** O frontend web (`dashboard/static/`) não é
> versionado aqui: o sistema é CLI-first e funciona sem interface gráfica.

---

## Índice

- [O que é](#o-que-é)
- [Sem Docker: como o sistema roda](#sem-docker-como-o-sistema-roda)
- [Instalação](#instalação)
- [Qual motor o sistema usa](#qual-motor-o-sistema-usa)
- [Como o pipeline funciona](#como-o-pipeline-funciona)
- [Sistema de cortes](#sistema-de-cortes)
- [Melhores momentos](#melhores-momentos)
- [Presets](#presets)
- [Usando pela CLI](#usando-pela-cli)
- [Backend e API local](#backend-e-api-local)
- [Configurando o LLM](#configurando-o-llm)
- [Onde ficam os arquivos](#onde-ficam-os-arquivos)
- [Limites honestos](#limites-honestos)
- [Licença](#licença)

---

## O que é

O clipdesk é um **motor de criação de clips** com duas entradas:

1. **Live acontecendo agora** — grava a transmissão em blocos, lê o chat em tempo real,
   detecta picos de audiência e corta o momento enquanto a live continua.
2. **Vídeo pronto** — um VOD do YouTube, Twitch ou Kick (ou um arquivo local): transcreve,
   pontua os momentos, reordena com IA e renderiza os cortes.

A saída é sempre um pacote postável:

```
clip.mp4          vertical 1080x1920 (ou horizontal 1920x1080)
thumb.jpg         capa com a mesma identidade visual do corte
legendas.md       crédito + texto de post para TikTok, Reels, Shorts e X
```

Nada é publicado automaticamente. O motor entrega o material; a revisão e o post são seus.

---

## Sem Docker: como o sistema roda

**Não existe container, imagem, `docker-compose` nem daemon.** O projeto é um conjunto de
scripts Python + dois binários de sistema:

| Peça | Como roda |
|---|---|
| Motor de análise e corte | Python 3.10+ executando `scripts/*.py` |
| Gravação e render | `ffmpeg` instalado no sistema |
| Download de vídeo/live | `yt-dlp` instalado via `pip` |
| Transcrição | `faster-whisper` rodando localmente (CPU ou CUDA) |
| Backend do painel (opcional) | servidor HTTP da stdlib do Python em `127.0.0.1:8787` |
| Estado | arquivos JSON/Markdown em `jobs/` e `dashboard/state/` |

Por que isso importa:

- **Zero overhead de container.** O ffmpeg usa a GPU/CPU direto, sem camada de virtualização.
- **Estado visível e editável.** Cada job é uma pasta; cada decisão do motor é um JSON.
- **Funciona offline** para análise e render (só download, tradução e LLM usam rede).
- **Portátil.** Windows, macOS e Linux com os mesmos scripts.
- **Fácil de depurar.** Se algo falha, você abre o arquivo e vê o que aconteceu.

O único processo opcional é o backend do painel (`dashboard/server.py`), e a CLI faz o mesmo
trabalho sem ele. O frontend web não faz parte deste repositório.

---

## Instalação

### Requisitos

- Python **3.10+**
- `ffmpeg` no `PATH`
- ~2 GB livres para os modelos do Whisper (o `small` é baixado no primeiro uso)
- Opcional: GPU NVIDIA para transcrever mais rápido

### Windows

```powershell
git clone https://github.com/pedronxp/clipdesk.git
cd clipdesk
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U -r requirements.txt
winget install --id Gyan.FFmpeg -e --source winget
```

Confira a instalação:

```powershell
ffmpeg -version
python -c "import faster_whisper, numpy, cv2; print('ok')"
```

### macOS

```bash
git clone https://github.com/pedronxp/clipdesk.git
cd clipdesk
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -U -r requirements.txt
brew install ffmpeg
```

### Linux

```bash
git clone https://github.com/pedronxp/clipdesk.git
cd clipdesk
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -U -r requirements.txt
sudo apt install -y ffmpeg
```

### Verificação rápida

```bash
ffmpeg -version
python -c "import faster_whisper, numpy, cv2; print('ok')"
python scripts/moments.py --help
```

Se o YouTube pedir login/cookies, exporte-os uma vez com `python scripts/export_cookies.py`
e aponte o arquivo em `dashboard/state/settings.json` (chave `ytdlp_cookies`).

---

## Qual motor o sistema usa

O clipdesk **não depende de um único modelo**. Ele usa um motor híbrido: heurísticas para
encontrar candidatos, um LLM opcional para dar precisão, e ferramentas determinísticas para
capturar e renderizar.

### Tabela de motores

| Etapa | Motor | Papel |
|---|---|---|
| Descoberta | `yt-dlp` + APIs públicas (YouTube, Twitch GQL, Kick) | Achar lives e vídeos |
| Captura | `ffmpeg` (`-c copy`, segmentos de 60 s) | Gravar sem re-encode |
| Chat | IRC anônimo da Twitch / replay do YouTube | Medir reação em tempo real |
| Transcrição | **faster-whisper** (Whisper local) | Palavras com timestamps |
| Sinais de momento | NumPy + heurísticas próprias | Loudness, hype, heatmap, chat |
| Re-rank | **qualquer LLM OpenAI-compatível** | Hook, engajamento, valor, compartilhamento |
| Webcam | **YuNet (ONNX)** + OpenCV | Achar e enquadrar o rosto |
| Corte/render | **ffmpeg** (filtros, ASS, x264, loudnorm) | Produzir o MP4 final |
| Tradução | Google → MyMemory (fallback) | Legendas por sentença |
| Voz | `edge-tts` (opcional) | Hook falado em outro idioma |
| Backend do painel | `http.server` da stdlib | API/orquestração local (opcional) |

### O LLM é plugável

O re-rank usa **qualquer endpoint compatível com a API OpenAI**. Exemplos que funcionam:

| Provedor | Base URL | Modelo de exemplo |
|---|---|---|
| OpenAI | `https://api.openai.com/v1` | `gpt-4o-mini` |
| DeepSeek | `https://api.deepseek.com/v1` | `deepseek-chat` |
| GLM / Zhipu | `https://open.bigmodel.cn/api/paas/v4` | `glm-4-flash` |
| NVIDIA NIM | `https://integrate.api.nvidia.com/v1` | conforme o catálogo |
| Ollama local | `http://localhost:11434/v1` | `llama3.1` |

Configuração por ambiente (vence o arquivo de settings):

```bash
export GTAVI_LLM_BASE="https://api.openai.com/v1"
export GTAVI_LLM_KEY="sk-..."
export GTAVI_LLM_MODEL="gpt-4o-mini"
```

Ou direto em `dashboard/state/settings.json` (fora do Git):

```json
{
  "llm": {
    "base": "https://api.openai.com/v1",
    "key": "sk-...",
    "model": "gpt-4o-mini"
  }
}
```

**Sem LLM o sistema continua funcionando.** O `rank_llm.py` sai com código 3, o pipeline
trata como "pular re-rank" e segue com a pontuação heurística. O corte sai igual; só perde
a camada de julgamento semântico.

---

## Como o pipeline funciona

```
FLUXO A — live agora
┌──────────┐   ┌─────────┐   ┌──────────────┐   ┌──────────────────────────────┐
│ streams  │──►│ live.py │──►│ post_capture │──►│ moments → rank_llm → cut     │
│ (achar)  │   │ gravar  │   │ (orquestra)  │   │ → translate → pack → export  │
└──────────┘   └─────────┘   └──────────────┘   └──────────────────────────────┘

FLUXO B — vídeo pronto (YouTube/Twitch/Kick ou arquivo em jobs/)
┌────────────────────┐   ┌─────────────────────────────────────────────┐
│ moments.py no URL  │──►│ rank_llm → cut → titles → pack → export     │
└────────────────────┘   └─────────────────────────────────────────────┘
```

| # | Fase | Script | Entrada | Saída |
|---|---|---|---|---|
| 0 | Descoberta | `streams.py`, `trends.py`, `twitch.py` | busca | `streams.md/json`, `trends.md` |
| 1 | Captura | `live.py` / `monitor.py` | URL da live | `jobs/<id>/live/` |
| 2 | Pós-captura | `post_capture.py` | captura encerrada | CFR, `chat.json`, `monitor.json` |
| 3 | Momentos | `moments.py` | vídeo fonte | `candidates.json` |
| 4 | Re-rank IA | `rank_llm.py` | candidatos | scores + hook PT-BR |
| 5 | Corte | `cut.py` | momento escolhido | `edits/clip_<n>.mp4` |
| 6 | Multilang | `translate.py` | clip cortado | `clip_<n>_<lang>.mp4` |
| 7 | Empacotamento | `pack.py` | clip final | thumb, legendas, `out/` |
| 8 | Títulos | `titles.py` | fala do momento | `titles.md` |
| 9 | Qualidade | `check.py` | clip + campanha | PASS/FAIL |

Todo job vive em `jobs/<id>/`, onde `<id>` é `slug-<sha1 da fonte, 8 chars>`.
`CLIP_DESK_HOME` muda a raiz do projeto; por padrão é o diretório atual.

---

## Sistema de cortes

O corte é a fase em que um momento escolhido vira um MP4 pronto para postar. O comando base:

```bash
python scripts/cut.py jobs/<id> --pick 2 --preset react-br --title "O momento que ninguém viu"
```

### 1. Escolha da janela

- `--pick N` lê o candidato `n=N` de `candidates.json`.
- `--start` / `--end` sobrescrevem a janela (re-corte fino).
- O `cut.py` não "adivinha" o momento: ele usa a janela que o motor já validou em fala real.

### 2. Layouts

| Layout | Composição | Quando usar |
|---|---|---|
| `fill` | Jogo em tela cheia | Gameplay puro, sem rosto |
| `blur` | Fundo borrado + jogo centralizado | Vídeo horizontal que precisa virar vertical |
| `react` | Webcam em cima (730 px) + gameplay embaixo (1190 px) | Reaction clássica com rosto |
| `facecam` | Cam no topo (38%) + jogo embaixo | Rosto em destaque sem moldura detectável |

Com `--cam auto`, o **YuNet** acha o rosto em 12 frames e o OpenCV deduz o retângulo da
webcam. Se não houver rosto:

- `react` pula o momento (o layout perde);
- `facecam` cai automaticamente para `blur`;
- `fill` e `blur` seguem normalmente.

### 3. Legendas

| Modo | Comportamento |
|---|---|
| `word` | Grupos de até 3 palavras, palavra ativa destacada, "pop" na primeira |
| `bubble` | Caixa com o grupo inteiro por vez |
| `sentence` | Uma bolha por sentença (pausa > 0,8 s ou pontuação) |

Com `--lang pt` (ou outro idioma), cada sentença é traduzida como um todo e distribuída no
tempo real da fala. **Não há sincronia de palavra falsa** quando o idioma é diferente do áudio.
Palavrões e slurs são mascarados por padrão; `--allow-profanity` libera os palavrões
(slurs continuam mascarados).

### 4. Título, hook e identidade

- `--title` desenha a faixa branca na costura (visual de reaction).
- `--title-style outline` desenha sem a faixa.
- `--title-secs 0` mantém o título no clip inteiro.
- `--hook` cria um texto de abertura com fade e zoom nos primeiros 2,6 s.
- A paleta vem do preset e pode ser sobrescrita:
  `--cap-color`, `--cap-highlight`, `--cap-outline`, `--box-fill`, `--box-text`.

### 5. Áudio, zoom e encode

- **Áudio:** `loudnorm=I=-14:TP=-1.5:LRA=11` — o padrão das plataformas.
- **Zoom:** `--zoom face` (1,18× no rosto), `game` (1,10× no gameplay) ou `both`.
- **Vídeo:** x264 `medium crf20 yuv420p`, AAC 160k 48 kHz, `+faststart`.
- **Correção de VFR:** `fps=30` explícito e `-t` para não esticar PTS de segmentos variáveis.

### 6. Saídas do corte

```
jobs/<id>/edits/clip_<n>.mp4     o clip final
jobs/<id>/edits/clip_<n>.ass     legendas queimadas (referência)
jobs/<id>/edits/clip_<n>.json    sidecar completo (janela, layout, cam, zoom, paleta, idioma)
log.md                           histórico de hooks usados (não repetir em 7 dias)
```

O sidecar é o que permite re-cortar, traduzir e gerar thumb **sem adivinhar** nenhum parâmetro.

### 7. Re-corte e variantes

- **Re-corte**: `cut.py --pick N --start ... --end ... --title "..." --hook "..."`.
- **Dividir/salvar momento**: edite `candidates.json` ou use a API local (`/api/split-moment`).
- **Multilang**: `translate.py` gera `clip_<n>_<lang>.mp4` herdando layout e paleta.

---

## Melhores momentos

"Melhores momentos" tem dois níveis no projeto: **encontrar** os candidatos e **compilar** os
melhores em um vídeo horizontal para YouTube.

### Nível 1 — encontrar os momentos (`moments.py`)

O motor extrai quatro sinais independentes:

| Sinal | Como mede | Peso |
|---|---|---|
| **Loudness** | Excesso de dB sobre a mediana de fala numa janela de ±150 s | `1,0 × z(excesso)` |
| **Hype words** | Regex no transcript (EN + PT-BR: "não acredito", "meu deus", "kkkk", "insane"…) | `0,8 × z(hype)` |
| **Most replayed** | Heatmap oficial do YouTube pintado segundo a segundo | `2,0 × z(heat)` |
| **Chat spikes** | Mensagens por segundo, peso 2 para LMAO/KEKW/CLIP/💀😂🔥 | `1,5 × z(chat)` |

```
score = 1,0·loud + 0,8·z(hype) + w_heat·z(heat) + w_chat·z(chat)
score = dampen_edges(score, n)
```

- `dampen_edges`: em vídeos ≥ 10 min, os primeiros/últimos `min(240, n/10)` segundos valem 30%
  — cumprimentos e despedidas não são momentos.
- Sem heatmap ou sem chat, o peso correspondente é zero (o motor não inventa sinal).
- Cada candidato recebe um campo `why`: "loudness spike", "hype words", "most replayed 0.83",
  "chat spike" ou "overall energy".

**Formação da janela:**

- Modo `--durations 30,60,150`: um take por duração para cada pico. Picos a menos de
  `1,1 × maior_tier` são o mesmo momento; só o melhor fica.
- O pico fica a ~40% do início da janela (hook cedo).
- As bordas encaixam em fala real: início = 1ª palavra −0,3 s; fim = última +0,6 s.
- A janela garante no mínimo 70% da duração alvo.

**Saída:**

```
jobs/<id>/candidates.json   candidatos, scores, why, texto, sinais disponíveis
jobs/<id>/candidates.md     versão legível com minuto, score e motivo
```

### Nível 2 — re-rank por IA (`rank_llm.py`)

As heurísticas dão **recall**; o LLM dá **precisão**. O script não reordena o arquivo: grava
notas por candidato e os consumidores ordenam por `llm.total_score`.

O modelo recebe até 12 candidatos com:

1. Título e canal da fonte.
2. Histórico real de performance (só após 3 posts com métricas).
3. Feedback do operador sobre spikes (falso positivo / bom).
4. Lista `id | janela | score | motivos | fala`.
5. Transcript por sentença com timestamps (±15 s de folga).

O modelo devolve JSON com:

- **4 notas de 0 a 25:** `hook_score`, `engagement_score`, `value_score`, `shareability_score`.
  O total é **recalculado localmente como soma**, nunca confiado ao modelo.
- **`hook_type`:** pergunta, afirmação, estatística, história, contraste ou nenhum.
- **`hook_title`:** 3–9 palavras em PT-BR, baseado no que é dito de verdade.
- **`adj_start` / `adj_end`:** ajuste de borda de até ±15 s, validado e limitado pelo script.

Robustez: retry ×2, parser tolerante (fences, comentários, vírgulas pendentes), ids
desconhecidos ignorados e janela original preservada em `signal_start`/`signal_end`.

### Nível 3 — compilar para YouTube (`compile.py`)

O motor também monta o formato clássico de canal: **um vídeo horizontal único** com os melhores
momentos em ordem cronológica.

```bash
python scripts/compile.py jobs/<id> '{"target_min": 10, "pip": true}'
python scripts/compile.py jobs/<id> '{"moments": "3,5,8", "title": "OS MELHORES DO DIA"}'
```

**Seleção:**

- Manual: `"moments": "3,5,8"`.
- Automática: ordena por `llm.total_score || score` e faz um *greedy fit* contra a duração alvo
  (5, 10, 15 ou 20 min). O primeiro momento sempre entra.
- Cada momento é aparado a **120 s em torno do pico**.
- O vídeo final é sempre cronológico.

**Render:**

- 1920×1080, um ffmpeg por momento, encode único (`medium crf20`, AAC 160k).
- `fade` de 0,5 s para preto entre momentos (vídeo e áudio).
- `loudnorm -14 LUFS`.
- **Webcam:** a cam já vem queimada no vídeo da live; o PiP **cobre e destaca** a região
  detectada pelo `facecam.find_cam()` em vez de duplicar a pessoa.
- Concat com demuxer `concat` e `-c copy` (sem segunda passada).
- Momento que falha é pulado; a compilação continua.

**Saídas:**

```
jobs/<id>/compile/plan.json      momentos usados
jobs/<id>/compile/melhores.mp4   vídeo final
jobs/<id>/compile/thumb.jpg      capa 1280x720 com faixa de título
jobs/<id>/compile/legendas.md    descrição com timestamps
out/<id>-melhores/               kit postável (MP4 + thumb + legendas)
```

Custo honesto: **~1 min de render por minuto compilado**, sem nova transcrição.

---

## Presets

Presets ficam em `scripts/presets.json` e são injetados no `cut.py` / `translate.py`.
**Flags explícitas sempre vencem o preset.**

```bash
python scripts/cut.py jobs/<id> --pick 2 --preset react-br
python scripts/cut.py jobs/<id> --pick 2 --preset original --zoom off
```

### Presets que vêm no projeto

| Preset | Layout | Legendas | Variantes | Zoom | Para quê |
|---|---|---|---|---|---|
| `react-br` **(padrão)** | `react` | `pt` (sentença traduzida) | `en` | `face` | Reaction PT-BR com zoom no rosto |
| `original` | `react` | original (word nativo) | — | `off` | Sem tradução nenhuma |
| `blur-multi` | `blur` | `pt` | `pt,en` | `game` | Fundo borrado + múltiplos idiomas |

### Campos de um preset

```json
{
  "react-br": {
    "layout": "react",
    "cam": "auto",
    "title_style": "box",
    "title_secs": 0,
    "captions": "word",
    "lang": "pt",
    "langs": "en",
    "zoom": "face",
    "thumb": true,
    "voice": false,
    "allow_profanity": false,
    "cap_color": "#FFFFFF",
    "cap_highlight": "#FFE500",
    "cap_outline": "#000000",
    "box_fill": "#FFFFFF",
    "box_text": "#000000"
  }
}
```

| Campo | O que controla |
|---|---|
| `layout` | `fill`, `blur`, `react` ou `facecam` |
| `cam` | `auto` ou frações `x:y:w:h` |
| `title_style` | `box` (faixa branca) ou `outline` |
| `title_secs` | `0` = clip inteiro |
| `captions` | `word`, `bubble`, `sentence` ou `none` |
| `lang` | idioma das legendas; vazio = áudio original |
| `langs` | variantes extras geradas pelo `translate.py` |
| `zoom` | `off`, `face`, `game` ou `both` |
| `thumb` | gera capa no `pack.py` |
| `voice` | hook falado com `edge-tts` |
| `allow_profanity` | mantém palavrões (slurs sempre mascarados) |
| `cap_color` | cor do texto da legenda |
| `cap_highlight` | cor da palavra ativa |
| `cap_outline` | contorno da legenda |
| `box_fill` | fundo do título/hook/bolhas |
| `box_text` | texto sobre a caixa |

### Criando um preset próprio

1. Duplique um bloco em `scripts/presets.json`.
2. Dê um nome (ex.: `meu-estilo`).
3. Ajuste layout, cores, zoom e idiomas.
4. Use com `--preset meu-estilo`.
5. Para torná-lo padrão, mude `"default"` no topo de `scripts/presets.json`.

Se um preset for renomeado ou removido, o motor degrada para o padrão `react-br` em vez de
quebrar.

---

## Usando pela CLI

### Fluxo A — live agora

```bash
# um comando: grava + chat + momentos + IA + corte + tradução + export
python scripts/monitor.py "https://twitch.tv/<canal>" --minutes 60 --top 3 --preset react-br

# só gravar e detectar spikes
python scripts/live.py "https://www.youtube.com/watch?v=<live>" --minutes 60

# pós-processar uma captura já encerrada
python scripts/post_capture.py jobs/<id> --top 3 --preset react-br
```

### Fluxo B — vídeo pronto

```bash
# 1. encontrar momentos
python scripts/moments.py "https://www.youtube.com/watch?v=..." --durations 30,60,150 --top 4

# 2. re-rank com IA (opcional)
python scripts/rank_llm.py jobs/<id> --top 5
python scripts/rank_llm.py jobs/<id> --top 5 --dry-run   # só imprime o prompt

# 3. cortar
python scripts/cut.py jobs/<id> --pick 2 --preset react-br --title "Ele não acreditou"

# 4. variantes de idioma
python scripts/translate.py jobs/<id> --pick 2 --langs en,es --voice

# 5. thumb + texto de post + export
python scripts/pack.py jobs/<id> --pick 2 --platforms tiktok,reels,youtube --export out/<slug>/

# 6. portão de qualidade
python scripts/check.py jobs/<id>/edits/clip_2.mp4 --campaign campaigns.md --caption "..."
```

### Melhores momentos

```bash
python scripts/compile.py jobs/<id> '{"target_min": 10}'
python scripts/compile.py jobs/<id> '{"moments": "3,5,8", "pip": true, "title": "TOP 5 DA LIVE"}'
```

### Feedback loop

```bash
python scripts/performance.py add --job <id> --n 2 --platform tiktok --views 12000 --likes 900
```

Após 3 posts com métricas, o histórico entra no prompt do re-rank como desempate — o motor
aprende quais hooks e durações funcionam para você.

---

## Backend e API local

O motor é **CLI-first**. O `dashboard/server.py` é um backend HTTP opcional que lê os mesmos
arquivos e expõe rotas para orquestração:

```bash
python dashboard/server.py
# escuta apenas em 127.0.0.1:8787
```

| Rota | O que faz |
|---|---|
| `GET /api/state` | Estado completo: jobs, cortes, capturas, criação |
| `POST /api/watch` | Inicia `live.py` numa URL |
| `POST /api/streams/refresh` | Atualiza a lista de lives |
| `POST /api/create/analyze` | `moments.py` → `rank_llm.py` → `cut.py` |
| `POST /api/rerank` | Re-rank dos candidatos com IA |
| `POST /api/recut` | Re-corta um clip (título, hook, trim) |
| `POST /api/split-moment` | Divide um momento em dois |
| `POST /api/lang` | Gera variantes de idioma |
| `POST /api/create/compile` | Dispara os melhores momentos |
| `POST /api/perf` | Registra métricas de um post |
| `POST /api/settings` | Atualiza settings (merge parcial) |

> O frontend web (`dashboard/static/`) **não é versionado neste repositório**. A pasta pode
> existir apenas na sua máquina; sem ela, a API continua funcionando e a CLI faz todo o fluxo.

O backend **não** publica nada sozinho. A única exclusão automática é de capturas *void*
(que terminaram sem nenhum vídeo), registrada em `dashboard/state/discarded.json`.

---

## Configurando o LLM

### Por ambiente (recomendado para CLI)

```bash
# OpenAI
export GTAVI_LLM_BASE="https://api.openai.com/v1"
export GTAVI_LLM_KEY="sk-..."
export GTAVI_LLM_MODEL="gpt-4o-mini"

# DeepSeek
export GTAVI_LLM_BASE="https://api.deepseek.com/v1"
export GTAVI_LLM_KEY="..."
export GTAVI_LLM_MODEL="deepseek-chat"

# Ollama local (sem custo, sem nuvem)
export GTAVI_LLM_BASE="http://localhost:11434/v1"
export GTAVI_LLM_KEY="ollama"
export GTAVI_LLM_MODEL="llama3.1"
```

### Escolhendo o Whisper

```bash
export GTAVI_WHISPER_MODEL=small   # tiny | base | small | medium | large-v3
```

- `base` acelera em CPU.
- `medium`/`large-v3` melhoram a qualidade em PT-BR.
- Com GPU NVIDIA, o motor usa `cuda float16` automaticamente; sem GPU, CPU `int8`.

---

## Onde ficam os arquivos

```
clipdesk/
├── scripts/                 motor (Python)
│   ├── presets.json         presets de corte
│   ├── moments.py           detecção de momentos
│   ├── rank_llm.py          re-rank com IA
│   ├── cut.py               corte vertical
│   ├── compile.py           melhores momentos horizontais
│   ├── translate.py         variantes de idioma
│   ├── pack.py              thumb + legendas + export
│   └── ...
├── dashboard/               backend local (API opcional)
│   ├── server.py
│   └── state/               settings, performance, feedback (fora do Git)
│   # static/               frontend local, fora do Git
├── docs/
│   └── MOTOR_DE_CRIACAO.md  pipeline detalhado fase por fase
├── jobs/<id>/               um job por fonte
│   ├── candidates.json      momentos encontrados
│   ├── edits/               clips, sidecars, thumbs
│   ├── compile/             melhores.mp4 + capa + legendas
│   └── live/                segmentos, chat, spikes
├── out/<slug>/              pacote final para postar
├── log.md                   histórico de hooks
└── streams.json             últimas lives encontradas
```

### Artefatos principais

| Caminho | Quem produz | O que é |
|---|---|---|
| `jobs/<id>/candidates.json` | `moments.py` + `rank_llm.py` | Momentos, scores, `why`, hook |
| `jobs/<id>/edits/clip_<n>.mp4` | `cut.py` | Clip final |
| `jobs/<id>/edits/clip_<n>.json` | `cut.py` | Sidecar com todos os parâmetros |
| `jobs/<id>/compile/melhores.mp4` | `compile.py` | Vídeo horizontal |
| `out/<slug>/clip.mp4` | `pack.py` | O que você posta |
| `dashboard/state/settings.json` | backend | LLM, Whisper, cookies, preset |
| `dashboard/state/performance.json` | `performance.py` | Métricas dos posts |
| `dashboard/state/discarded.json` | `voids.py` | Log de capturas descartadas |

---

## Limites honestos

- **Post é manual.** Nada sai da sua máquina sem você.
- **Render é CPU.** x264 `medium`: um take de 60 s leva ~4–6 min.
- **Transcrição é pesada.** Um vídeo de 12 min leva ~5 min em CPU.
- **Tradução depende de serviço externo.** Google → MyMemory; se o Google bloquear, a qualidade
  cai um degrau (o fallback não contamina o cache).
- **LLM é opcional.** Sem ele, o ranking é 100% heurístico.
- **Direitos.** Só corte dentro de campanha ou com permissão do criador. O crédito é gerado
  automaticamente, mas usá-lo é sua responsabilidade.
- **Sem Docker não é limitação.** É uma escolha: menos camadas, estado visível, GPU direta e
  depuração simples.

---

## Documentação técnica

- [`docs/COMO_E_FEITO_O_CORTE.md`](docs/COMO_E_FEITO_O_CORTE.md) — passo a passo didático do
  corte, usando um clip real como exemplo (captura → spike → análise → IA → render).
- [`docs/MOTOR_DE_CRIACAO.md`](docs/MOTOR_DE_CRIACAO.md) — pipeline completo, fase por fase,
  com critérios numéricos e artefatos.

---

## Licença

MIT. Projeto não oficial, sem vínculo com Rockstar Games, Take-Two ou Anthropic.

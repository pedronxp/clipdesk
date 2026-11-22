# Produção — rotina diária

Tudo roda na sua máquina, com o Python do projeto (`.venv\Scripts\python`). Nada é postado
automaticamente: o pipeline entrega **clip + legendas + imagens prontos**, você revisa e posta.

## 0. Checagem única (uma vez)

```
.venv\Scripts\python -c "import faster_whisper, cv2, deep_translator, edge_tts; print('deps OK')"
ffmpeg -version
.venv\Scripts\python scripts\export_cookies.py dashboard\state\cookies.txt   (veja "Cookies do YouTube" abaixo)
```

Se o ffmpeg não for encontrado no terminal, use o painel (ele ajusta o PATH sozinho) ou instale:
`winget install ffmpeg`. GPU NVIDIA acelera a transcrição (opcional).

## 0.1 Cookies do YouTube (uma vez — sem isso o YouTube bloqueia)

Sem cookies, o YouTube responde **“Sign in to confirm you're not a bot”**: a busca ainda lista
as lives (sem espectadores), mas monitorar/cortar vídeo do YouTube falha. O Chrome e o Edge
127+ criptografam os cookies de forma que o yt-dlp não consegue ler, então a exportação é
manual, uma única vez:

1. Instale a extensão **Get cookies.txt LOCALLY** (Chrome Web Store ou Edge Add-ons).
2. Abra uma **janela anônima**, faça login em https://www.youtube.com.
3. Ainda na janela anônima: clique na extensão → **Export** (formato Netscape) e salve como
   `dashboard\state\cookies.txt`.
4. Feche a janela anônima sem abrir o YouTube nela de novo (a sessão ficaria rotacionada).
5. Valide (grava o caminho em `dashboard/state/settings.json`):
   `.venv\Scripts\python scripts\export_cookies.py dashboard\state\cookies.txt`

Alternativa sem extensão: instale o Firefox, faça login no YouTube nele e rode
`.venv\Scripts\python scripts\export_cookies.py firefox`. O campo também fica na aba
**Ajustes** do painel (“Cookies do YouTube”). O `cookies.txt` vale como sessão da sua conta:
não compartilhe nem comite o arquivo (`dashboard/state/` já está fora do git).

Cookies não bastam sozinhos: o YouTube também serve desafios JS (EJS) que o yt-dlp precisa
resolver para listar formatos tocáveis — sem isso só aparecem storyboards. Instale o Node.js
22+ e o solver (`pip install yt-dlp-ejs`, já no requirements.txt); o `scripts/common.py` aponta
o runtime sozinho. Teste real de diagnóstico: `.venv\Scripts\python scripts\export_cookies.py
dashboard\state\cookies.txt` falha no “testado contra o YouTube” quando falta o solver.

## 1. Manhã — descobrir o que gravar / analisar (2 min)

```
.venv\Scripts\python scripts\streams.py --query "gta 6" --out streams.md
```

Abra `streams.md` (ou o painel): maiores lives de GTA 6 agora + vídeos prontos candidatos.

## 2. Dois fluxos de produção

### Fluxo A — live acontecendo agora (clipping em tempo real)
```
.venv\Scripts\python scripts\monitor.py "https://twitch.tv/<canal>" --minutes 60 --top 3 --preset react-br
```
Um comando: grava, reconstrói CFR, lê o chat, ranqueia momentos, corta os top 3
(react + zoom + legendas PT + variantes EN), faz thumb e exporta em `out\<slug>\`.
Título-rascunho vem da própria fala — **marcado para revisão**.

### Fluxo B — vídeo pronto do YouTube
```
.venv\Scripts\python scripts\moments.py "https://www.youtube.com/watch?v=..." --durations 30,60,150 --top 4
.venv\Scripts\python scripts\titles.py jobs\<id>
.venv\Scripts\python scripts\cut.py jobs\<id> --pick 1 --layout react --cam auto --lang pt --zoom face
.venv\Scripts\python scripts\pack.py jobs\<id> --pick 1 --platforms tiktok,youtube,reels --export out\<slug>
```
`--durations 30,60,150` gera um take de cada duração por momento quente; `candidates.md`
mostra minuto inicial/final, score e o motivo. Cada take tem 3 títulos sugeridos em `titles.md`.

Dica: análise de vídeo longo é pesada. Para transcrever mais rápido:
`set GTAVI_WHISPER_MODEL=base` (ou `accurate` → medium, quando a qualidade importa).

## 3. Portão de qualidade (antes de postar, sempre)

1. **Título**: revise em `titles.md` / `monitor.json` — tradução de máquina escorrega.
2. **Legendas**: assista o clip; legendas são queimadas no vídeo.
3. **Regras da campanha**:
   ```
   .venv\Scripts\python scripts\check.py jobs\<id>\edits\clip_1.mp4 --campaign campaigns.md --caption "legenda do post"
   ```
   Crie `campaigns.md` com as regras da campanha (Whop/Content Rewards):
   `min_seconds: 15` / `max_seconds: 60` / `required_hashtags: #GTA6 #fyp` / `required_mention: @canal`
4. **Crédito**: sempre no post (`legendas.md` já traz o bloco pronto por plataforma).

## 4. O que sai de produção (`out/<slug>/`)

| Arquivo | Uso |
|---|---|
| `clip.mp4` | 1080x1920, -14 LUFS, legendas queimadas |
| `thumb_tiktok.jpg` | capa TikTok (9:16) |
| `thumb_reels.jpg` | capa Reels (banda na zona segura 4:5) |
| `thumb_youtube.jpg` | capa Shorts/YouTube (16:9) |
| `legendas.md` | texto do post por plataforma + crédito + fonte |

## 5. Painel (opcional, facilita tudo)

```
.venv\Scripts\python dashboard\server.py
```
http://127.0.0.1:8787 — acompanha gravações, corta pela interface, gera sugestões de post
e **limpa o disco sozinho** (voids: capturas sem vídeo são descartadas após 15 min;
sources baixados e análises em andamento são protegidos).

## 6. Limites atuais (honestos)

- **Post é manual** por decisão de projeto (aprovação humana em tudo).
- **Google Translate bloqueado no seu IP hoje** → o sistema usa MyMemory sozinho;
  quando o Google liberar, os renders seguintes voltam à qualidade melhor automaticamente.
- Renders são CPU (x264): um take de 60s leva ~4-6 min. Para escalar, rode à noite ou
  considere GPU.
- Direitos: só corte dentro de campanha ou com permissão do criador (crédito no post).

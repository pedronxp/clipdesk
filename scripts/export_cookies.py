"""One-time YouTube cookie export/validate — fixes the bot check ("Sign in to confirm
you're not a bot") for every script and the dashboard.

usage: python export_cookies.py              # try reading cookies from an installed browser
       python export_cookies.py firefox      # read from one specific browser
       python export_cookies.py cookies.txt  # validate an already exported file

The validated source is saved to dashboard/state/settings.json {"ytdlp_cookies": ...} (the
same field the Ajustes tab edits), so nothing else needs configuring afterwards. Windows
Chrome/Edge v127+ encrypt cookies with app-bound keys yt-dlp cannot read even with the
browser closed — when reading fails, the exact extension steps are printed (also in
PRODUCAO.md).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import ROOT, SETTINGS, load_settings, save_json, run, ytdlp_base

COOKIES_FILE = ROOT / "dashboard" / "state" / "cookies.txt"
PROBE = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"  # stable, always-available video: extraction succeeds only when the cookies are accepted
SESSION_COOKIES = ("SAPISID", "__Secure-1PAPISID", "SSID", "SID")  # login markers; a logged-out jar passes no bot check


def validate(path):
    """(ok, message) — file parses, carries youtube.com cookies and a logged-in session."""
    p = Path(path)
    if not p.is_file() or p.stat().st_size == 0:
        return False, f"{p} não existe ou está vazio"
    names = set()
    for ln in p.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = ln.split("\t")
        if len(parts) >= 7 and "youtube.com" in parts[0]:
            names.add(parts[5].strip())
    if not names:
        return False, f"{p} não tem cookies de youtube.com (formato Netscape esperado)"
    if not any(m in names for m in SESSION_COOKIES):
        return False, (f"{p} tem cookies do YouTube, mas nenhum de sessão logada ({SESSION_COOKIES[0]}, …). "
                       "Abra o youtube.com logado antes de exportar")
    return True, f"{len(names)} cookies de youtube.com, sessão logada presente"


def probe_works(extra):
    """True when yt-dlp extracts the probe video with these cookie args (no inherited cookie args)."""
    r = run(ytdlp_base(cookies=False) + extra + ["-J", "--skip-download", PROBE], capture=True, check=False)
    return r.returncode == 0 and r.stdout.strip().startswith("{")


def save(source):
    """Store the working source in settings.json (relative to ROOT when possible)."""
    s = load_settings()
    try:
        s["ytdlp_cookies"] = Path(source).resolve().relative_to(ROOT).as_posix()
    except ValueError:  # outside ROOT — keep the absolute path
        s["ytdlp_cookies"] = str(Path(source).resolve())
    save_json(SETTINGS, s)
    print(f"salvo em dashboard/state/settings.json: ytdlp_cookies = {s['ytdlp_cookies']}")


def from_browser(browser):
    """Export browser cookies to COOKIES_FILE and validate with a real extraction."""
    COOKIES_FILE.parent.mkdir(parents=True, exist_ok=True)
    COOKIES_FILE.unlink(missing_ok=True)
    args = ["--cookies-from-browser", browser, "--cookies", str(COOKIES_FILE)]
    if probe_works(args):
        ok, msg = validate(COOKIES_FILE)
        if ok:
            print(f"cookies lidos do {browser}: {msg}")
            save(COOKIES_FILE)
            return True
    tail = (run(ytdlp_base(cookies=False) + args + ["-J", "--skip-download", PROBE], capture=True, check=False).stderr or "").strip().splitlines()
    print(f"não consegui ler cookies do {browser}: {tail[-1] if tail else 'erro desconhecido'}", file=sys.stderr)
    return False


def manual_steps():
    print("""
O Chrome/Edge 127+ no Windows criptografa os cookies com chave app-bound: o yt-dlp
não consegue ler, nem com o navegador fechado. Faça a exportação manual uma única vez:

  1. Instale a extensão "Get cookies.txt LOCALLY" (Chrome Web Store ou Edge Add-ons).
  2. Abra uma JANELA ANÔNIMA e faça login em https://www.youtube.com.
  3. Ainda na janela anônima, clique na extensão → "Export" (formato Netscape) e salve como:
       dashboard/state/cookies.txt
  4. Feche a janela anônima sem abrir o YouTube nela de novo (para a sessão não ser rotacionada).
  5. Valide:  python scripts/export_cookies.py dashboard/state/cookies.txt

Alternativa que dispensa extensão: instale o Firefox, faça login no YouTube nele e rode:
       python scripts/export_cookies.py firefox
""")


def main():
    # This script's whole job is printing accented instructions, and Windows consoles still
    # default to cp850/cp1252: switch the console to UTF-8 and reconfigure stdout so the
    # steps are readable in a plain CMD window as well (never an encode crash).
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
            ctypes.windll.kernel32.SetConsoleCP(65001)
        except Exception:
            pass
    arg = (sys.argv[1] if len(sys.argv) > 1 else "").strip()
    if arg and Path(arg).suffix.lower() == ".txt":
        ok, msg = validate(arg)
        print(msg)
        if not ok:
            sys.exit(1)
        if probe_works(["--cookies", str(Path(arg).resolve())]):
            save(arg)
            print("testado contra o YouTube: extração funcionou ✓")
        else:
            sys.exit("o arquivo tem formato válido, mas o YouTube recusou a extração — exporte de novo logado")
        return
    if arg:
        ok = from_browser(arg)
    else:
        ok = False
        for b in ("firefox", "chrome", "edge", "brave", "chromium", "vivaldi"):
            print(f"tentando {b}…")
            if from_browser(b):
                ok = True
                break
    if ok:
        print("pronto: scripts e dashboard já usam os cookies (sem reiniciar nada)")
    else:
        manual_steps()
        sys.exit(1)


if __name__ == "__main__":
    main()

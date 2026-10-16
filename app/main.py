"""Güvenlik Kamerası İzleme — giriş noktası."""
import asyncio
import ctypes
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config  # noqa: E402

PORT = int(os.environ.get("GUVENLIK_PORT", "47800"))
URL = f"http://127.0.0.1:{PORT}/"


def _browser():
    cands = [
        os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
        shutil.which("msedge") or "", shutil.which("chrome") or "",
    ]
    return next((c for c in cands if c and os.path.exists(c)), None)


def open_window():
    exe = _browser()
    if exe:
        profile = config.APP_DIR / "pencere"
        profile.mkdir(parents=True, exist_ok=True)
        subprocess.Popen([exe, f"--app={URL}", f"--user-data-dir={profile}", "--window-size=1400,900",
                          "--no-first-run", "--no-default-browser-check", "--disable-features=Translate"])
    else:
        webbrowser.open(URL)


_mutex = None


def _first_instance():
    """Aynı anda yalnızca bir kopya çalışır (Windows mutex'i; iki kez hızlı tıklansa da ikincisi açılmaz)."""
    global _mutex
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateMutexW.restype = ctypes.c_void_p
    _mutex = k.CreateMutexW(None, False, f"Local\\GuvenlikKamerasi_{PORT}")
    return ctypes.get_last_error() != 183          # ERROR_ALREADY_EXISTS


def _show_running():
    """Çalışan kopyadan penceresini göstermesini ister (o kopya henüz açılıyorsa biraz bekler)."""
    ctypes.windll.user32.AllowSetForegroundWindow(-1)  # öne gelme iznini çalışan kopyaya devret
    for _ in range(30):
        try:
            req = urllib.request.Request(URL + "api/show", data=b"{}", method="POST",
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=2) as r:
                return json.loads(r.read() or b"{}").get("ok", False)
        except urllib.error.HTTPError:
            break                                   # eski sürüm: /api/show yok
        except OSError:
            time.sleep(0.5)                          # sunucu henüz açılıyor
    return False


def _port_in_use():
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", PORT)) == 0


def _start_tray(app):
    import server
    import tray
    loop = asyncio.get_running_loop()

    def quit_():
        async def later():
            await app.shutdown()
            os._exit(0)
        loop.call_soon_threadsafe(lambda: asyncio.ensure_future(later()))

    try:
        app.tray = tray.Tray(server.WEB_DIR / "simge.ico",
                             on_open=lambda: loop.call_soon_threadsafe(open_window),
                             on_toggle=lambda: loop.call_soon_threadsafe(app.toggle_connection),
                             on_quit=quit_,
                             is_connected=lambda: app.status in ("connected", "connecting"))
    except Exception:
        logging.exception("tray")


async def run(open_ui=True):
    from aiohttp import web
    import server

    app = server.App()
    wa = await server.create(app)
    runner = web.AppRunner(wa, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", PORT).start()
    logging.info("Arayüz: %s", URL)
    app.start_connect()
    app.open_window = open_window
    _start_tray(app)
    if "--autostart" in sys.argv and app.settings.get("close_to_tray", True) and app.tray:
        open_ui = False         # Windows açılışında pencere açılmaz, program tray'de bekler
    if open_ui:
        open_window()
    await app.background()


def main():
    config.APP_DIR.mkdir(parents=True, exist_ok=True)
    handlers = [logging.FileHandler(config.APP_DIR / "gunluk.txt", encoding="utf-8")]
    if sys.stdout and sys.stdout.isatty():
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s", handlers=handlers)
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)

    if not _first_instance() or _port_in_use():      # program zaten açık: yeni kopya açılmaz
        if "--autostart" not in sys.argv and not _show_running() and _port_in_use():
            open_window()
        return
    try:
        asyncio.run(run(open_ui="--no-ui" not in sys.argv))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

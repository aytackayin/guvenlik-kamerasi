"""Güvenlik Kamerası İzleme — giriş noktası."""
import asyncio
import logging
import os
import shutil
import socket
import subprocess
import sys
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
    _start_tray(app)
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

    if _port_in_use():          # program zaten açık: yalnızca pencereyi aç
        open_window()
        return
    try:
        asyncio.run(run(open_ui="--no-ui" not in sys.argv))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

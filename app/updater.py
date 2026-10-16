"""GitHub sürümlerinden (releases) güncelleme denetimi ve kurulumu."""
import asyncio
import logging
import os
import re
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

import aiohttp

VERSION = "1.1.0"
REPO = "aytackayin/guvenlik-kamerasi"
AUTHOR_URL = "https://github.com/aytackayin"
REPO_URL = f"https://github.com/{REPO}"
API = f"https://api.github.com/repos/{REPO}/releases/latest"
EXE_NAME = "Güvenlik Kamerası.exe"

log = logging.getLogger("updater")


def _ver(text):
    return tuple(int(x) for x in re.findall(r"\d+", text or "")[:3]) or (0,)


def frozen():
    return getattr(sys, "frozen", False)


class Updater:
    def __init__(self):
        self.info = None              # son denetim sonucu
        self.state = "idle"           # idle | checking | downloading | installing | error
        self.progress = 0.0
        self.message = ""

    def status(self):
        return {"version": VERSION, "repo_url": REPO_URL, "author_url": AUTHOR_URL, "frozen": frozen(),
                "state": self.state, "progress": round(self.progress, 3), "message": self.message,
                "release": self.info}

    async def check(self):
        self.state, self.message = "checking", ""
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as s:
                async with s.get(API, headers={"Accept": "application/vnd.github+json",
                                               "User-Agent": "GuvenlikKamerasi"}) as r:
                    if r.status == 404:
                        raise RuntimeError("Henüz yayınlanmış bir sürüm yok")
                    r.raise_for_status()
                    rel = await r.json()
        except Exception as e:
            self.state, self.message = "error", f"Güncelleme denetlenemedi: {e}"
            return self.status()
        asset = next((a for a in rel.get("assets", []) if a["name"].lower().endswith(".zip")), None)
        tag = rel.get("tag_name", "")
        self.info = {"tag": tag, "name": rel.get("name") or tag, "notes": rel.get("body") or "",
                     "url": rel.get("html_url"), "published": rel.get("published_at"),
                     "asset": asset and {"name": asset["name"], "url": asset["browser_download_url"],
                                         "size": asset["size"]},
                     "newer": _ver(tag) > _ver(VERSION)}
        self.state = "idle"
        return self.status()

    async def install(self, before_exit):
        """Yeni sürümü indirir, program kapandıktan sonra dosyaları değiştirip yeniden başlatır."""
        if not frozen():
            raise RuntimeError("Program kaynak koddan çalışıyor; güncellemek için 'git pull' kullanın.")
        if not (self.info and self.info.get("newer") and self.info.get("asset")):
            raise RuntimeError("Kurulacak yeni sürüm yok")
        if self.state in ("downloading", "installing"):
            return
        asyncio.ensure_future(self._install(before_exit))

    async def _install(self, before_exit):
        asset = self.info["asset"]
        work = Path(tempfile.mkdtemp(prefix="guvenlik_guncelleme_"))
        zpath = work / asset["name"]
        self.state, self.progress, self.message = "downloading", 0.0, "İndiriliyor…"
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None, sock_read=60)) as s:
                async with s.get(asset["url"], headers={"User-Agent": "GuvenlikKamerasi"}) as r:
                    r.raise_for_status()
                    total = int(r.headers.get("Content-Length") or asset["size"] or 0)
                    got = 0
                    with open(zpath, "wb") as f:
                        async for chunk in r.content.iter_chunked(1 << 16):
                            f.write(chunk)
                            got += len(chunk)
                            if total:
                                self.progress = got / total
            self.state, self.message = "installing", "Kuruluyor…"
            src = await asyncio.to_thread(self._extract, zpath, work / "yeni")
        except Exception as e:
            log.exception("güncelleme")
            self.state, self.message = "error", f"Güncelleme indirilemedi: {e}"
            return

        dest = Path(sys.executable).parent
        script = work / "guncelle.ps1"
        q = lambda p: "'" + str(p).replace("'", "''") + "'"   # noqa: E731
        script.write_text(
            "$ErrorActionPreference = 'SilentlyContinue'\n"
            f"Wait-Process -Id {os.getpid()} -Timeout 60\n"
            "Start-Sleep -Milliseconds 800\n"
            f"robocopy {q(src)} {q(dest)} /E /R:5 /W:1 /NFL /NDL /NJH /NJS | Out-Null\n"
            f"Start-Process -FilePath {q(dest / EXE_NAME)} -ArgumentList '--no-ui'\n"
            f"Remove-Item -Recurse -Force {q(work)}\n",
            encoding="utf-8-sig")
        subprocess.Popen(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden",
                          "-File", str(script)],
                         creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP, close_fds=True)
        self.message = "Program yeniden başlatılıyor…"
        await asyncio.sleep(0.5)
        await before_exit()
        os._exit(0)

    @staticmethod
    def _extract(zpath, out):
        with zipfile.ZipFile(zpath) as z:
            for n in z.namelist():                   # zip dışına yazmaya çalışan girdileri reddet
                if n.startswith(("/", "\\")) or ".." in Path(n).parts:
                    raise RuntimeError("Geçersiz güncelleme paketi")
            z.extractall(out)
        exe = next(out.rglob(EXE_NAME), None)
        if not exe:
            raise RuntimeError("Güncelleme paketinde program bulunamadı")
        return exe.parent

"""
İndirme klasörü kitaplığı: dosyaları listeler, küçük resim üretir, not ve kayıt bilgisi (meta) tutar,
tarayıcıda doğrudan oynatılamayan biçimler için oynatma kopyası hazırlar, dosyayı Geri Dönüşüm
Kutusu'na taşır.
"""
import asyncio
import ctypes
import ctypes.wintypes as wt
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

import config
from media import FFMPEG, NO_WINDOW

VIDEO = {"mp4", "mkv", "avi", "webm", "dav"}
IMAGE = {"jpg", "jpeg", "png", "webp"}
LOGS = {"csv", "json"}          # olay logları (Olaylar klasöründe)
BROWSER_PLAYABLE = {"mp4", "webm"}
NOTES_FILE = config.APP_DIR / "notlar.json"
META_FILE = config.APP_DIR / "dosya_bilgileri.json"
THUMBS = config.APP_DIR / "kucuk_resimler"
PLAYCACHE = config.APP_DIR / "oynatma_kopyalari"

FFPROBE = str(Path(FFMPEG).with_name("ffprobe.exe")) if FFMPEG.lower().endswith(".exe") else "ffprobe"

_NAME_RANGE = re.compile(r"^(?P<cam>.+?)_(?P<d>\d{4}-\d{2}-\d{2})_(?P<a>\d{2}-\d{2}-\d{2})_(?P<b>\d{2}-\d{2}-\d{2})")
_LOG_NAME = re.compile(r"^Olaylar_(?P<cam>.+?)_(?P<d1>\d{4}-\d{2}-\d{2})_(?P<t1>\d{2}-\d{2})_(?P<d2>\d{4}-\d{2}-\d{2})_(?P<t2>\d{2}-\d{2})$")
_NAME_SNAP = re.compile(r"^(?P<cam>.+?)_(?P<d>\d{4}-\d{2}-\d{2})_(?P<a>\d{2}-\d{2}-\d{2})")


def _load(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(path, data):
    config.APP_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


class Library:
    def __init__(self, get_root):
        self.get_root = get_root           # () -> kayıt klasörü
        self.notes = _load(NOTES_FILE)     # göreli yol -> not
        self.meta = _load(META_FILE)       # göreli yol -> {channel, aspect, start, end}
        self._locks = {}
        self._thumb_sem = asyncio.Semaphore(3)     # aynı anda en fazla 3 küçük resim üretilir

    # ------------------------------------------------ yollar
    def root(self):
        return Path(self.get_root())

    def rel(self, path):
        return Path(path).resolve().relative_to(self.root().resolve()).as_posix()

    def resolve(self, rel):
        p = (self.root() / rel).resolve()
        if self.root().resolve() not in p.parents and p != self.root().resolve():
            raise PermissionError("Klasör dışı dosya")
        return p

    # ------------------------------------------------ meta
    def set_meta(self, path, **kw):
        key = self.rel(path)
        self.meta[key] = {**self.meta.get(key, {}), **kw}
        _save(META_FILE, self.meta)

    def set_note(self, rel, text):
        text = (text or "").strip()
        if text:
            self.notes[rel] = {"text": text, "updated": dt.datetime.now().strftime("%Y-%m-%d %H:%M")}
        else:
            self.notes.pop(rel, None)
        _save(NOTES_FILE, self.notes)

    # ------------------------------------------------ liste
    def list(self):
        root = self.root()
        out = []
        if not root.exists():
            return out
        for p in list(root.glob("*")) + list(root.glob("*/*")):
            if not p.is_file():
                continue
            ext = p.suffix.lower().lstrip(".")
            # indirilmekte olan dosyaların ara parçaları (… .cap1.mkv, … .part) kütüphanede görünmez
            if ext not in VIDEO | IMAGE | LOGS or ".part" in p.name or re.search(r"\.cap\d*\.", p.name):
                continue
            if ext in LOGS and not p.name.startswith("Olaylar_"):
                continue
            st = p.stat()
            rel = p.relative_to(root).as_posix()
            item = {"path": rel, "name": p.name, "ext": ext,
                    "kind": "video" if ext in VIDEO else "log" if ext in LOGS else "image",
                    "size": st.st_size, "mtime": dt.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%dT%H:%M:%S"),
                    "folder": p.parent.relative_to(root).as_posix() if p.parent != root else ""}
            m = _LOG_NAME.match(p.stem) or _NAME_RANGE.match(p.stem) or _NAME_SNAP.match(p.stem)
            if m and ext in LOGS:
                item["camera"] = m.group("cam").replace("_", ", ")
                item["start"] = f"{m.group('d1')}T{m.group('t1').replace('-', ':')}:00"
                item["end"] = f"{m.group('d2')}T{m.group('t2').replace('-', ':')}:00"
            elif m:
                item["camera"] = m.group("cam")
                item["start"] = f"{m.group('d')}T{m.group('a').replace('-', ':')}"
                if "b" in m.groupdict() and m.group("b"):
                    item["end"] = f"{m.group('d')}T{m.group('b').replace('-', ':')}"
            meta = self.meta.get(rel, {})
            item.update({k: v for k, v in meta.items() if k in ("channel", "aspect", "start", "end", "camera", "source")})
            note = self.notes.get(rel)
            if note:
                item["note"] = note["text"]
                item["note_updated"] = note["updated"]
            out.append(item)
        return out

    # ------------------------------------------------ küçük resim
    async def thumb(self, rel, aspect=None):
        src = self.resolve(rel)
        st = src.stat()
        key = hashlib.sha1(f"{src}|{st.st_mtime}|{st.st_size}|{aspect}".encode()).hexdigest()
        out = THUMBS / f"{key}.jpg"
        if out.exists():
            return out
        THUMBS.mkdir(parents=True, exist_ok=True)
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock, self._thumb_sem:
            if out.exists():
                return out
            ext = src.suffix.lower().lstrip(".")
            w = 360
            h = f"{int(w / aspect) // 2 * 2}" if aspect else "-2"
            vf = f"scale={w}:{h}"
            pre = [] if ext in IMAGE else ["-ss", "1"]
            args = [FFMPEG, "-v", "error", "-y", *pre, "-i", str(src), "-frames:v", "1", "-vf", vf, "-q:v", "4", str(out)]
            p = await asyncio.create_subprocess_exec(*args, creationflags=NO_WINDOW)
            await p.wait()
            if not out.exists() and pre:      # çok kısa videoda ilk kareyi dene
                args = [FFMPEG, "-v", "error", "-y", "-i", str(src), "-frames:v", "1", "-vf", vf, "-q:v", "4", str(out)]
                p = await asyncio.create_subprocess_exec(*args, creationflags=NO_WINDOW)
                await p.wait()
        return out if out.exists() else None

    # ------------------------------------------------ oynatma
    async def playable(self, rel):
        """Tarayıcıda oynayabilecek dosyayı döndürür (gerekirse yeniden kodlamadan MP4 kopyası üretir)."""
        src = self.resolve(rel)
        ext = src.suffix.lower().lstrip(".")
        if ext in BROWSER_PLAYABLE or ext in IMAGE or ext in LOGS:
            return src
        st = src.stat()
        key = hashlib.sha1(f"{src}|{st.st_mtime}|{st.st_size}".encode()).hexdigest()
        out = PLAYCACHE / f"{key}.mp4"
        if out.exists():
            return out
        PLAYCACHE.mkdir(parents=True, exist_ok=True)
        lock = self._locks.setdefault("p" + key, asyncio.Lock())
        async with lock:
            if out.exists():
                return out
            tmp = out.with_suffix(".part.mp4")
            vcodec = await self._vcodec(src)
            tag = ["-tag:v", "hvc1"] if vcodec == "hevc" else []
            args = [FFMPEG, "-v", "error", "-y", "-i", str(src), "-map", "0:v:0", "-map", "0:a:0?",
                    "-c:v", "copy", *tag, "-c:a", "aac", "-b:a", "64k", "-movflags", "+faststart", str(tmp)]
            p = await asyncio.create_subprocess_exec(*args, stderr=asyncio.subprocess.PIPE, creationflags=NO_WINDOW)
            _, err = await p.communicate()
            if p.returncode != 0 or not tmp.exists():
                # kopyalanamayan görüntü biçimi: H.264'e çevir
                args = [FFMPEG, "-v", "error", "-y", "-i", str(src), "-map", "0:v:0", "-map", "0:a:0?",
                        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
                        "-c:a", "aac", "-b:a", "64k", "-movflags", "+faststart", str(tmp)]
                p = await asyncio.create_subprocess_exec(*args, creationflags=NO_WINDOW)
                await p.wait()
            if tmp.exists():
                os.replace(tmp, out)
        self._trim_cache()
        return out if out.exists() else None

    @staticmethod
    async def _vcodec(src):
        p = await asyncio.create_subprocess_exec(FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_entries",
                                                 "stream=codec_name", "-of", "csv=p=0", str(src),
                                                 stdout=asyncio.subprocess.PIPE, creationflags=NO_WINDOW)
        out, _ = await p.communicate()
        return out.decode().strip()

    async def probe(self, rel):
        src = self.resolve(rel)
        p = await asyncio.create_subprocess_exec(
            FFPROBE, "-v", "error", "-show_entries",
            "format=duration:stream=codec_type,codec_name,width,height,avg_frame_rate,display_aspect_ratio",
            "-of", "json", str(src), stdout=asyncio.subprocess.PIPE, creationflags=NO_WINDOW)
        out, _ = await p.communicate()
        try:
            return json.loads(out or b"{}")
        except ValueError:
            return {}

    @staticmethod
    def _trim_cache(limit=3 * 1024 ** 3):
        """Oynatma kopyaları en fazla ~3 GB yer kaplasın; en eskiler silinir."""
        files = sorted(PLAYCACHE.glob("*.mp4"), key=lambda f: f.stat().st_mtime)
        total = sum(f.stat().st_size for f in files)
        while files and total > limit:
            f = files.pop(0)
            total -= f.stat().st_size
            f.unlink(missing_ok=True)

    # ------------------------------------------------ silme
    def recycle(self, rel):
        """Dosyayı Geri Dönüşüm Kutusu'na taşır (kalıcı silmez)."""
        src = self.resolve(rel)
        _recycle(src)
        self.notes.pop(rel, None)
        self.meta.pop(rel, None)
        _save(NOTES_FILE, self.notes)
        _save(META_FILE, self.meta)


class _SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [("hwnd", wt.HWND), ("wFunc", wt.UINT), ("pFrom", wt.LPCWSTR), ("pTo", wt.LPCWSTR),
                ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", wt.BOOL),
                ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wt.LPCWSTR)]


def _recycle(path):
    FO_DELETE, FOF_ALLOWUNDO, FOF_NOCONFIRMATION, FOF_SILENT, FOF_NOERRORUI = 3, 0x40, 0x10, 0x4, 0x400
    op = _SHFILEOPSTRUCTW(None, FO_DELETE, str(path) + "\0", None,
                          FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI, False, None, None)
    r = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    if r != 0 or op.fAnyOperationsAborted:
        raise OSError(f"Dosya Geri Dönüşüm Kutusu'na taşınamadı (kod {r})")

"""Uygulama ayarları. Şifre, Windows DPAPI ile bu kullanıcı hesabına bağlı şifrelenir."""
import base64
import ctypes
import ctypes.wintypes as wt
import json
import os
from pathlib import Path

APP_DIR = Path(os.environ.get("APPDATA", Path.home())) / "GuvenlikKamera"
SETTINGS_FILE = APP_DIR / "ayarlar.json"


def _default_save_dir():
    videos = Path.home() / "Videos"
    return str((videos if videos.exists() else Path.home()) / "Güvenlik Kayıtları")


DEFAULTS = {
    "serial": "",
    "username": "admin",
    "password": "",
    "mode": "auto",        # auto | p2p | lan
    "ip": "",              # yerel ağ IP adresi (isteğe bağlı)
    "save_dir": _default_save_dir(),
    "format": "mp4",
    "quality": "main",     # indirmelerde akış: main | sub
    "show_all": False,     # kamerası olmayan kanalları da göster
    "snap_format": "jpg",  # kare alma biçimi: jpg | png | webp
    "hidden_cams": {},     # ekranda gizlenen kameralar: {"live": [3], "archive": []}
    "log_keep_days": 14,   # programın sakladığı cihaz logu arşivi (gün); daha eskisi kendiliğinden silinir
    "cameras": {},         # kamera başına: {"1": {"mp": 5, "aspect": "auto"}}
}


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi(data: bytes, protect: bool) -> bytes:
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    buf = ctypes.create_string_buffer(data, len(data))
    src = _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    dst = _Blob()
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    if not fn(ctypes.byref(src), None, None, None, None, 0x01, ctypes.byref(dst)):
        raise OSError("DPAPI hatası")
    try:
        return ctypes.string_at(dst.pbData, dst.cbData)
    finally:
        kernel32.LocalFree(dst.pbData)


def _protect(text):
    if not text:
        return ""
    try:
        return "dpapi:" + base64.b64encode(_dpapi(text.encode(), True)).decode()
    except Exception:
        return "b64:" + base64.b64encode(text.encode()).decode()


def _unprotect(text):
    if not text:
        return ""
    try:
        kind, _, val = text.partition(":")
        raw = base64.b64decode(val)
        return (_dpapi(raw, False) if kind == "dpapi" else raw).decode()
    except Exception:
        return ""


def load():
    data = dict(DEFAULTS)
    try:
        stored = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        data.update({k: v for k, v in stored.items() if k in DEFAULTS})
        data["password"] = _unprotect(stored.get("password", ""))
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return data


def save(data):
    APP_DIR.mkdir(parents=True, exist_ok=True)
    out = {k: data.get(k, v) for k, v in DEFAULTS.items()}
    out["password"] = _protect(out["password"])
    SETTINGS_FILE.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

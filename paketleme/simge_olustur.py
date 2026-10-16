"""web/logo.svg'den uygulama simgelerini üretir (Edge ile çizer, Pillow ile .ico yapar).

Çıktılar: paketleme/simge.ico (exe), paketleme/simge.png, web/simge.ico (pencere/tray), web/simge.png.
Yalnızca derleme sırasında gerekir.
"""
import os
import subprocess
import tempfile
from pathlib import Path

from PIL import Image

KOK = Path(__file__).resolve().parent.parent
SVG = KOK / "web" / "logo.svg"
BOYUTLAR = [16, 20, 24, 32, 40, 48, 64, 128, 256]


def edge():
    for p in (r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
              r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"):
        p = os.path.expandvars(p)
        if os.path.exists(p):
            return p
    raise SystemExit("Microsoft Edge bulunamadı")


def ciz(px=1024):
    with tempfile.TemporaryDirectory() as tmp:
        html = Path(tmp) / "s.html"
        html.write_text(f"<html><body style='margin:0;background:transparent'>"
                        f"<img src='{SVG.as_uri()}' width='{px}' height='{px}' style='display:block'></body></html>",
                        encoding="utf-8")
        png = Path(tmp) / "s.png"
        subprocess.run([edge(), "--headless", "--disable-gpu", "--hide-scrollbars",
                        "--default-background-color=00000000", f"--window-size={px},{px}",
                        f"--user-data-dir={tmp}\\p", f"--screenshot={png}", html.as_uri()],
                       check=True, capture_output=True, timeout=60)
        return Image.open(png).convert("RGBA").copy()


def main():
    img = ciz()
    kucukler = [img.resize((s, s), Image.LANCZOS) for s in BOYUTLAR]
    for hedef in (KOK / "paketleme", KOK / "web"):
        kucukler[-1].save(hedef / "simge.ico", sizes=[(s, s) for s in BOYUTLAR], append_images=kucukler[:-1])
        kucukler[-1].save(hedef / "simge.png")
        print(hedef / "simge.ico")


if __name__ == "__main__":
    main()

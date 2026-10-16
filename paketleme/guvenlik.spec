# PyInstaller tanımı — "paketleme\derle.bat" ile çalıştırılır.
import os
import shutil

KOK = os.path.abspath(os.path.join(SPECPATH, ".."))
FFMPEG = os.environ.get("FFMPEG_EXE") or os.path.realpath(shutil.which("ffmpeg"))

a = Analysis(
    [os.path.join(KOK, "app", "main.py")],
    pathex=[os.path.join(KOK, "app")],
    binaries=[(FFMPEG, "."), (os.path.join(os.path.dirname(FFMPEG), "ffprobe.exe"), ".")],
    datas=[(os.path.join(KOK, "web"), "web")],
    hiddenimports=["av"],
    excludes=["tkinter", "unittest", "pydoc", "PIL"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Güvenlik Kamerası",
    icon=os.path.join(SPECPATH, "simge.ico"),
    console=False,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="Güvenlik Kamerası", upx=False)

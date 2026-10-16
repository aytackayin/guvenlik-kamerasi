"""
Bir kanalda iki kodlama ayarını aynı sahnede karşılaştırır (ör. gece plaka testi).

Kullanım:  python araclar/kalite_karsilastir.py [kanal] [çıktı_klasörü]
- Önce şu anki ayarla, sonra 1080P/15 fps ile canlı kare alır, sonra kanalı ESKİ ayarına döndürür.
- Kareleri kameranın gerçek oranına (4:3, 1600x1200) getirip yan yana bir görsel üretir.
- Program (Güvenlik Kamerası) açıksa önce kapatın: cihaz aynı anda birden çok P2P oturumunda zorlanıyor.
"""
import asyncio
import datetime as dt
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
import config  # noqa: E402
import dahua  # noqa: E402

CH = int(sys.argv[1]) if len(sys.argv) > 1 else 1
OUT = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(config.load()["save_dir"]) / "Karşılaştırma" / dt.datetime.now().strftime("%Y-%m-%d_%H-%M")
OUT.mkdir(parents=True, exist_ok=True)


async def grab(d, name):
    p = await asyncio.create_subprocess_exec(
        "ffmpeg", "-v", "error", "-y", "-rtsp_transport", "tcp", "-i", d.rtsp_url(CH, 0), "-an",
        "-vf", r"select=gte(n\,20)", "-frames:v", "3", "-fps_mode", "passthrough", str(OUT / f"{name}_%d.png"))
    await asyncio.wait_for(p.wait(), 90)
    src = OUT / f"{name}_2.png"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(src), "-vf", "scale=1600:1200:flags=lanczos",
                    str(OUT / f"{name}_4x3.png")], check=True)


async def main():
    s = config.load()
    s["mode"] = "p2p" if s.get("mode") == "auto" else s.get("mode")
    d = dahua.Device(s)
    await d.connect()
    cur = await d.camera_config(CH)
    m, sub = cur["main"], cur["sub"]
    orig = {"main": {k: m[k] for k in ("resolution", "fps", "gop", "compression", "control", "quality", "bitrate", "audio")},
            "sub": {k: sub[k] for k in ("resolution", "fps", "bitrate")}}
    print("Şu anki ayar:", m["resolution"], m["fps"], "fps")
    try:
        await grab(d, "A_simdiki")
        test = {"main": {**orig["main"], "resolution": "1920x1080", "fps": 15, "gop": 15}, "sub": orig["sub"]}
        final, w = await d.set_camera_config(CH, test)
        print("Test ayarı:", final["main"]["resolution"], final["main"]["fps"], "fps", w)
        await asyncio.sleep(5)
        await grab(d, "B_1080p15")
    finally:
        final, w = await d.set_camera_config(CH, orig)
        print("Geri alındı:", final["main"]["resolution"], final["main"]["fps"], "fps", final["main"]["control"],
              final["main"]["bitrate"], "kbps", w)
        await d.close()
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(OUT / "A_simdiki_4x3.png"), "-i", str(OUT / "B_1080p15_4x3.png"),
                    "-filter_complex", "[0][1]hstack", str(OUT / "yan_yana.png")], check=True)
    print("Çıktı:", OUT)


if __name__ == "__main__":
    asyncio.run(main())

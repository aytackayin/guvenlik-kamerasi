"""
Görüntü aktarımı (RTSP -> parçalı MP4 -> WebSocket -> tarayıcı MSE) ve kayıt indirme.
"""
import asyncio
import datetime as dt
import glob
import itertools
import json
import logging
import os
import re
import shutil
import struct
import subprocess
import time
from pathlib import Path

log = logging.getLogger("media")


def _find_ffmpeg():
    """Paketlenmiş sürümde exe'nin yanındaki ffmpeg'i, yoksa sistemdekini kullanır."""
    import sys
    for base in (getattr(sys, "_MEIPASS", None), os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else None):
        if base and os.path.isfile(os.path.join(base, "ffmpeg.exe")):
            return os.path.join(base, "ffmpeg.exe")
    return shutil.which("ffmpeg") or "ffmpeg"


FFMPEG = _find_ffmpeg()
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# ---------------------------------------------------------------- MP4 yardımcıları

def _boxes(data):
    i = 0
    while i + 8 <= len(data):
        size, kind = struct.unpack(">I4s", data[i:i + 8])
        if size < 8:
            break
        yield kind, data[i + 8:i + size]
        i += size


def _find_box(data, path):
    for kind, body in _boxes(data):
        if kind == path[0]:
            if len(path) == 1:
                return body
            # bazı kutuların başlıkları alt kutulardan önce sabit alan içerir
            skip = {b"stsd": 8, b"hvc1": 78, b"avc1": 78, b"hev1": 78}.get(kind, 0)
            r = _find_box(body[skip:], path[1:])
            if r is not None:
                return r
    return None


def codec_string(init):
    """Başlatma segmentinden tarayıcı kodek dizesini çıkarır (ör. avc1.640028, hvc1.1.6.L120.90)."""
    stsd = _find_box(init, [b"moov", b"trak", b"mdia", b"minf", b"stbl", b"stsd"])
    if not stsd:
        return None
    entries = stsd[8:]
    kind = entries[4:8]
    sample = entries[8:]
    cfg = None
    for k, body in _boxes(sample[78:]):
        if k in (b"avcC", b"hvcC"):
            cfg = (k, body)
            break
    if not cfg:
        return None
    if cfg[0] == b"avcC":
        b = cfg[1]
        return f"avc1.{b[1]:02x}{b[2]:02x}{b[3]:02x}"
    b = cfg[1]
    space = (b[1] >> 6) & 3
    tier = (b[1] >> 5) & 1
    pidc = b[1] & 0x1F
    compat = struct.unpack(">I", b[2:6])[0]
    rev = int(f"{compat:032b}"[::-1], 2)
    cons = list(b[6:12])
    while cons and cons[-1] == 0:
        cons.pop()
    level = b[12]
    s = f"{kind.decode()}.{['', 'A', 'B', 'C'][space]}{pidc}.{rev:x}.{'H' if tier else 'L'}{level}"
    if cons:
        s += "." + ".".join(f"{c:x}" for c in cons)
    return s


async def _read_box(stream):
    head = await stream.readexactly(8)
    size, kind = struct.unpack(">I4s", head)
    if size == 1:
        ext = await stream.readexactly(8)
        size = struct.unpack(">Q", ext)[0]
        return kind, head + ext + await stream.readexactly(size - 16)
    return kind, head + await stream.readexactly(size - 8)


# ---------------------------------------------------------------- canlı / oynatma akışı

class LiveStream:
    """Bir RTSP kaynağını parçalı MP4'e çevirip parça parça verir."""

    def __init__(self, url, transcode=False, fast=False, scale=None):
        self.url = url
        self.transcode = transcode
        self.scale = scale
        self.proc = None

    def _args(self):
        a = [FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin",
             "-rtsp_transport", "tcp", "-timeout", "15000000",
             "-fflags", "+nobuffer+genpts", "-flags", "low_delay",
             "-analyzeduration", "1000000", "-probesize", "500000",
             "-i", self.url, "-map", "0:v:0", "-an"]
        if self.transcode:
            vf = []
            if self.scale:
                vf = ["-vf", f"scale=-2:'min({self.scale},ih)'"]
            # yalnızca tarayıcı H.265 çözemezse kullanılır; kalite öncelikli ama gerçek zamanlı
            a += vf + ["-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency",
                       "-profile:v", "high", "-pix_fmt", "yuv420p", "-g", "20", "-crf", "19"]
        else:
            a += ["-c:v", "copy"]
        a += ["-f", "mp4", "-movflags", "empty_moov+default_base_moof+frag_keyframe+skip_trailer",
              "-frag_duration", "250000", "pipe:1"]
        return a

    async def start(self):
        args = self._args()
        self.proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            limit=2 ** 22, creationflags=NO_WINDOW)

    async def chunks(self):
        """İlk olarak (init, codec) verir, sonra moof+mdat parçaları."""
        out = self.proc.stdout
        init = b""
        while True:
            kind, box = await _read_box(out)
            init += box
            if kind == b"moov":
                break
        yield init
        pending = b""
        while True:
            kind, box = await _read_box(out)
            if kind == b"moof":
                pending = box
            elif kind == b"mdat":
                yield pending + box
                pending = b""

    async def error_text(self):
        try:
            return (await asyncio.wait_for(self.proc.stderr.read(2000), 1)).decode(errors="replace").strip()
        except Exception:
            return ""

    def stop(self):
        if self.proc and self.proc.returncode is None:
            try:
                self.proc.kill()
            except ProcessLookupError:
                pass


# ---------------------------------------------------------------- DAV (DHAV) yazıcı

class DavWriter:
    """Dahua DHAV kapsayıcısı. Kareler yeniden kodlanmadan cihaz formatında yazılır."""

    VIDEO_CODECS = {"hevc": 0x0C, "h264": 0x02, "mpeg4": 0x01, "mjpeg": 0x03}
    AUDIO_CODECS = {"pcm_alaw": 0x0E, "pcm_mulaw": 0x0A, "aac": 0x1A, "pcm_s16le": 0x10}
    RATES = [8000, 4000, 8000, 11025, 16000, 20000, 22050, 32000, 44100, 48000, 96000, 192000, 64000]

    def __init__(self, path, channel, start_time):
        self.f = open(path, "wb")
        self.channel = (channel - 1) & 0xFF
        self.start = start_time
        self.n = 0
        self.last_ms = {}

    @staticmethod
    def _date(t):
        return ((t.year - 2000) << 26) | (t.month << 22) | (t.day << 17) | (t.hour << 12) | (t.minute << 6) | t.second

    def _frame(self, ftype, ext, payload, when, ms):
        kind = ftype == 0xF0
        if ms <= self.last_ms.get(kind, -1):
            ms = self.last_ms[kind] + 1
        self.last_ms[kind] = ms
        length = 24 + len(ext) + len(payload) + 8
        head = bytearray(b"DHAV" + struct.pack("<BBBBIIIHB", ftype, 0, self.channel, 0, self.n,
                                               length, self._date(when), ms & 0xFFFF, len(ext)))
        head.append(sum(head) & 0xFF)
        self.f.write(bytes(head) + ext + payload + b"dhav" + struct.pack("<I", length))
        self.n += 1

    def video(self, data, key, t_ms, codec, w, h, fps):
        when = self.start + dt.timedelta(milliseconds=t_ms)
        ext = b""
        if key:
            ext = (struct.pack("<BBBBHH", 0x82, 0, 0, 0, w, h)
                   + struct.pack("<BBBB", 0x81, 0, self.VIDEO_CODECS.get(codec, 0x0C), max(1, min(255, int(fps)))))
        self._frame(0xFD if key else 0xFC, ext, data, when, t_ms)

    def audio(self, data, t_ms, codec, rate, channels):
        when = self.start + dt.timedelta(milliseconds=t_ms)
        idx = self.RATES.index(rate) if rate in self.RATES else 0
        ext = struct.pack("<BBBB", 0x83, channels, self.AUDIO_CODECS.get(codec, 0x0E), idx)
        self._frame(0xF0, ext, data, when, t_ms)

    def close(self):
        self.f.close()


def nut_to_dav(srcs, dst, channel, start_time):
    """Ara NUT dosyalarındaki (Annex-B) kareleri sırayla tek bir DAV dosyasına yazar."""
    import av
    if isinstance(srcs, (str, Path)):
        srcs = [srcs]
    w = DavWriter(dst, channel, start_time)
    base = 0                                   # sonraki parçanın zamanı öncekinin bittiği yerden sürer
    try:
        for src in srcs:
            with av.open(str(src)) as c:
                vs = c.streams.video[0] if c.streams.video else None
                aus = c.streams.audio[0] if c.streams.audio else None
                fps = float(vs.average_rate or vs.guessed_rate or 25) if vs else 25
                if fps > 60:
                    fps = 25
                t0 = None
                last = base
                for pkt in c.demux():
                    if pkt.size == 0 or pkt.pts is None:
                        continue
                    t = float(pkt.pts * pkt.time_base)
                    if t0 is None:
                        t0 = t
                    ms = base + int(round((t - t0) * 1000))
                    last = max(last, ms)
                    if vs is not None and pkt.stream is vs:
                        w.video(bytes(pkt), pkt.is_keyframe, ms, vs.codec_context.name,
                                vs.codec_context.width, vs.codec_context.height, fps)
                    elif aus is not None and pkt.stream is aus:
                        w.audio(bytes(pkt), ms, aus.codec_context.name, aus.codec_context.sample_rate,
                                aus.codec_context.channels if hasattr(aus.codec_context, "channels") else 1)
            base = last + int(1000 / fps)
    finally:
        w.close()


# ---------------------------------------------------------------- indirme işleri

FORMATS = {
    "mp4":  {"ext": "mp4", "label": "MP4 (önerilen)"},
    "mp4h": {"ext": "mp4", "label": "MP4 – H.264 (her cihazda açılır)"},
    "mkv":  {"ext": "mkv", "label": "MKV"},
    "avi":  {"ext": "avi", "label": "AVI"},
    "webm": {"ext": "webm", "label": "WebM (web sayfaları ve tarayıcılar için)"},
    "dav":  {"ext": "dav", "label": "DAV (cihazın kendi formatı)"},
}


def _out_args(fmt):
    """
    Yakalama aşamasının kodek ayarları. Görüntü önce yarıda kesilse bile okunabilen bir ara dosyaya
    (Matroska; DAV için NUT) alınır, son biçim (MP4/AVI/WebM...) ayrı ve hızlı bir adımda yazılır.
    """
    if fmt == "mp4":
        return ["-map", "0:v:0", "-map", "0:a:0?", "-c:v", "copy", "-c:a", "aac", "-b:a", "64k"]
    if fmt == "mp4h":
        return ["-map", "0:v:0", "-map", "0:a:0?", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "64k"]
    if fmt == "mkv":
        return ["-map", "0:v:0", "-map", "0:a:0?", "-c", "copy"]
    if fmt == "avi":
        return ["-map", "0:v:0", "-map", "0:a:0?", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                "-pix_fmt", "yuv420p", "-c:a", "libmp3lame", "-b:a", "64k"]
    if fmt == "webm":  # tarayıcıda doğrudan oynayan açık biçim: VP9 görüntü + Opus ses
        return ["-map", "0:v:0", "-map", "0:a:0?", "-c:v", "libvpx-vp9", "-b:v", "0", "-crf", "32",
                "-deadline", "realtime", "-cpu-used", "8", "-row-mt", "1", "-pix_fmt", "yuv420p",
                "-c:a", "libopus", "-b:a", "48k"]
    if fmt == "dav":   # ara dosya: kareler Annex-B, zaman damgalı
        return ["-map", "0:v:0", "-map", "0:a:0?", "-c", "copy", "-bsf:v", "hevc_mp4toannexb", "-f", "nut"]
    raise ValueError(fmt)


CONTAINER = {"mp4": "mp4", "mp4h": "mp4", "mkv": "matroska", "avi": "avi", "webm": "webm"}
FFPROBE = str(Path(FFMPEG).with_name("ffprobe.exe")) if FFMPEG.lower().endswith(".exe") else "ffprobe"


async def _probe_duration(path):
    p = await asyncio.create_subprocess_exec(FFPROBE, "-v", "error", "-show_entries", "format=duration", "-of",
                                             "csv=p=0", str(path), stdout=asyncio.subprocess.PIPE,
                                             stderr=asyncio.subprocess.DEVNULL, creationflags=NO_WINDOW)
    out, _ = await p.communicate()
    try:
        return float(out.decode().strip())
    except ValueError:
        return 0.0


def _safe(name):
    return re.sub(r'[<>:"/\\|?*]+', "_", name).strip() or "Kamera"


ACTIVE = ("waiting", "running", "converting")     # 'paused' bunlara dahil değil: program kapanabilir


class _Paused(Exception):
    """Duraklatma ya da program kapanışı: inen parçalar saklanır, iş sonra kaldığı yerden sürer."""


class Job:
    _ids = itertools.count(1)

    def __init__(self, ch, ch_name, start, end, fmt, quality, folder, aspect=None, source=None, offset=0.0,
                 path=None, taken=()):
        self.id = next(self._ids)
        self.ch, self.ch_name = ch, ch_name
        self.start, self.end = start, end
        self.fmt, self.quality = fmt, quality
        self.aspect = aspect            # "4:3" gibi; dosyaya görüntü oranı olarak yazılır
        self.source = source            # yerel dosyadan kesit alınıyorsa kaynak yol
        self.offset = offset            # kaynakta başlangıç (sn)
        if path:
            self.path = Path(path)
        else:
            stamp = f"{start:%Y-%m-%d_%H-%M-%S}_{end:%H-%M-%S}"
            base = f"{_safe(ch_name)}_{stamp}" + ("_kesit" if source else "")
            self.path = Path(folder) / f"{base}.{FORMATS[fmt]['ext']}"
            n = 2
            while self.path.exists() or self.path in taken:
                self.path = self.path.with_name(f"{base} ({n}).{FORMATS[fmt]['ext']}")
                n += 1
        self.state = "waiting"     # waiting | running | paused | converting | done | error | cancelled
        self.progress = 0.0
        self.message = "Sırada"
        self.size = 0
        self.task = None
        self.proc = None
        self.created = time.time()
        self.pieces = []           # [(dosya adı, süre sn)]: inmiş, sağlam parçalar
        self.sub = None            # kullanılan akış (0 ana, 1 alt)
        self.pause_req = False     # kullanıcı duraklattı
        self.suspend_req = False   # program kapanıyor: açılınca kendiliğinden sürecek

    @property
    def duration(self):
        return max(1.0, (self.end - self.start).total_seconds())

    @property
    def done_sec(self):
        return sum(d for _, d in self.pieces)

    def piece_path(self, n):
        return self.path.with_name(f"{self.path.name}.cap{n}.{'nut' if self.fmt == 'dav' else 'mkv'}")

    def to_dict(self):
        return {
            "id": self.id, "channel": self.ch, "name": self.ch_name,
            "start": self.start.strftime("%Y-%m-%d %H:%M:%S"), "end": self.end.strftime("%Y-%m-%d %H:%M:%S"),
            "format": self.fmt, "format_label": FORMATS[self.fmt]["label"], "state": self.state,
            "progress": round(self.progress, 3), "message": self.message,
            "file": str(self.path), "filename": self.path.name, "size": self.size,
            "local": bool(self.source),
        }

    def to_store(self):
        return {"ch": self.ch, "ch_name": self.ch_name, "start": self.start.isoformat(), "end": self.end.isoformat(),
                "fmt": self.fmt, "quality": self.quality, "aspect": self.aspect, "source": self.source,
                "offset": self.offset, "path": str(self.path), "pieces": self.pieces, "sub": self.sub,
                "paused": self.state == "paused" and self.pause_req, "created": self.created}

    @classmethod
    def from_store(cls, d):
        job = cls(d["ch"], d["ch_name"], dt.datetime.fromisoformat(d["start"]), dt.datetime.fromisoformat(d["end"]),
                  d["fmt"], d.get("quality", "main"), None, aspect=d.get("aspect"), source=d.get("source"),
                  offset=d.get("offset", 0.0), path=d["path"])
        job.pieces = [(n, float(s)) for n, s in d.get("pieces", []) if (job.path.parent / n).exists()]
        job.sub = d.get("sub")
        job.created = d.get("created", job.created)
        job.progress = min(0.99, job.done_sec / job.duration)
        return job


class Downloader:
    def __init__(self, get_device, on_done=None, store=None):
        self.get_device = get_device
        self.on_done = on_done          # bitince çağrılır (kitaplığa kayıt bilgisi yazmak için)
        self.store = Path(store) if store else None   # bitmemiş işler burada saklanır
        self.jobs = {}
        self.sem = asyncio.Semaphore(2)
        self.local_sem = asyncio.Semaphore(2)

    # ------------------------------------------------ kalıcı liste
    def save(self):
        if not self.store:
            return
        keep = [j.to_store() for j in sorted(self.jobs.values(), key=lambda j: j.id)
                if j.state in ACTIVE or j.state == "paused"]
        try:
            self.store.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.store.with_suffix(".tmp")
            tmp.write_text(json.dumps(keep, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, self.store)
        except OSError:
            log.exception("indirme listesi yazılamadı")

    def restore(self):
        """Program açılırken yarım kalan indirmeleri geri yükler; duraklatılmamış olanlar kendiliğinden sürer."""
        if not self.store or not self.store.exists():
            return
        try:
            items = json.loads(self.store.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        for d in items:
            try:
                job = Job.from_store(d)
            except (KeyError, ValueError):
                continue
            self.jobs[job.id] = job
            if d.get("paused"):
                job.state, job.pause_req = "paused", True
                job.message = f"Duraklatıldı (%{int(job.progress * 100)})"
            else:
                self._start(job)
        if items:
            log.info("%d yarım indirme geri yüklendi", len(items))

    # ------------------------------------------------ işlemler
    def add(self, **kw):
        taken = {j.path for j in self.jobs.values() if j.state in ACTIVE or j.state == "paused"}
        job = Job(taken=taken, **kw)
        job.path.parent.mkdir(parents=True, exist_ok=True)
        self.jobs[job.id] = job
        self._start(job)
        self.save()
        return job

    def _start(self, job):
        job.pause_req = job.suspend_req = False
        job.state = "waiting"
        job.message = f"Sırada (%{int(job.progress * 100)} inmiş)" if job.pieces else "Sırada"
        job.task = asyncio.ensure_future(self._run(job))

    def pause(self, jid):
        job = self.jobs.get(jid)
        if not job or job.state not in ("waiting", "running"):
            return
        job.pause_req = True
        if job.state == "waiting":
            job.task.cancel()            # sıradaysa hemen; çalışıyorsa parça düzgün kapatılıp durur
        else:
            job.message = "Duraklatılıyor…"

    def resume(self, jid):
        job = self.jobs.get(jid)
        if job and job.state == "paused":
            self._start(job)
            self.save()

    def cancel(self, jid):
        job = self.jobs.get(jid)
        if not job:
            return
        if job.state == "paused":
            job.state, job.message = "cancelled", "İptal edildi"
            self._cleanup(job)
            self.save()
        elif job.state in ACTIVE:
            job.pause_req = job.suspend_req = False
            job.state = "cancelled"
            job.message = "İptal edildi"
            if job.proc and job.proc.returncode is None:
                job.proc.kill()
            job.task.cancel()

    def remove(self, jid):
        job = self.jobs.get(jid)
        if job and job.state not in ACTIVE and job.state != "paused":
            del self.jobs[jid]

    async def suspend_all(self, timeout=10):
        """Program kapanırken: süren indirmeler parçası düzgün kapatılarak durdurulur, açılışta sürer."""
        tasks = []
        for job in self.jobs.values():
            if job.state in ("waiting", "running") and job.task:
                job.suspend_req = True
                if job.state == "waiting":
                    job.task.cancel()
                tasks.append(job.task)
        if tasks:
            await asyncio.wait(tasks, timeout=timeout)
        self.save()

    def list(self):
        return [j.to_dict() for j in sorted(self.jobs.values(), key=lambda j: -j.id)]

    # ------------------------------------------------ çalıştırma
    async def _device(self, job):
        """Cihaz bağlı değilse bağlanmasını bekler (program yeni açıldıysa bağlantı birkaç saniye sürer)."""
        while True:
            dev = self.get_device()
            if dev and dev.connected:
                return dev
            job.message = "Cihaz bağlantısı bekleniyor…"
            await asyncio.sleep(2)

    async def _run(self, job):
        try:
            async with (self.local_sem if job.source else self.sem):
                if job.state == "cancelled":
                    return
                dev = None if job.source else await self._device(job)
                job.state = "running"
                job.message = "Kesit hazırlanıyor…" if job.source else "Kayıt alınıyor…"
                await self._adopt_orphan(job)
                if job.sub is None:
                    job.sub = 1 if job.quality == "sub" else 0
                drops = 0
                while job.duration - job.done_sec > 2.5:
                    piece = job.piece_path(len(job.pieces) + 1)
                    got, done = await self._capture(job, dev, piece)
                    if got > 0.5 and piece.exists():
                        job.pieces.append((piece.name, (await _probe_duration(piece)) or got))
                        self.save()
                    else:
                        piece.unlink(missing_ok=True)
                    if job.pause_req or job.suspend_req:
                        raise _Paused()
                    if done:
                        break
                    if not job.pieces and job.sub == 1:
                        job.sub = 0         # alt akış kaydı yoksa ana akışla dene
                        continue
                    # akış erken kesildi: bağlantı koptuysa yeniden bağlanınca kaldığı yerden sür
                    if dev and not dev.connected and drops < 5:
                        drops += 1
                        dev = await self._device(job)
                        job.message = "Kayıt alınıyor…"
                        continue
                    break                   # cihaz bağlı ama veri bitti: bu aralığın kalanında kayıt yok
                if not job.pieces:
                    raise RuntimeError("Kayıt alınamadı. Bu aralıkta kayıt olmayabilir.")
                partial = job.duration - job.done_sec > 3
                job.state = "converting"
                paths = [job.path.parent / n for n, _ in job.pieces]
                if job.fmt == "dav":
                    job.message = "DAV dosyası oluşturuluyor…"
                    part = job.path.with_name(job.path.name + ".part")
                    await asyncio.to_thread(nut_to_dav, paths, part, job.ch, job.start)
                    os.replace(part, job.path)
                else:
                    job.message = "Dosya hazırlanıyor…"
                    await self._finalize(job, paths)
                if await _probe_duration(job.path) < 0.5:
                    raise RuntimeError("Video dosyası düzgün oluşturulamadı")
                self._cleanup(job, keep_final=True)
                job.size = job.path.stat().st_size
                job.progress = 1.0
                job.state = "done"
                job.message = f"Kayıt kısmen alınabildi ({int(job.done_sec)} sn)" if partial else "Tamamlandı"
                self.save()
                if self.on_done:
                    try:
                        self.on_done(job)
                    except Exception:
                        log.exception("kayıt bilgisi yazılamadı")
        except (asyncio.CancelledError, _Paused):
            if job.proc and job.proc.returncode is None:
                job.proc.kill()
            if job.pause_req:
                job.state, job.message = "paused", f"Duraklatıldı (%{int(job.progress * 100)})"
            elif job.suspend_req:
                job.state, job.message = "waiting", "Program açılınca devam edecek"
            else:
                job.state, job.message = "cancelled", "İptal edildi"
                self._cleanup(job)
            self.save()
        except Exception as e:
            log.exception("indirme hatası")
            job.state = "error"
            job.message = str(e) or e.__class__.__name__
            self._cleanup(job)
            self.save()

    async def _adopt_orphan(self, job):
        """Program beklenmedik kapandıysa son parça listeye girmemiştir: onarıp (yeniden paketleyip) ekler."""
        p = job.piece_path(len(job.pieces) + 1)
        if not p.exists():
            return
        fixed = p.with_name(p.name + ".fix")
        proc = await asyncio.create_subprocess_exec(
            FFMPEG, "-v", "error", "-y", "-i", str(p), "-map", "0", "-c", "copy",
            "-f", "nut" if job.fmt == "dav" else "matroska", str(fixed),
            stderr=asyncio.subprocess.DEVNULL, creationflags=NO_WINDOW)
        await proc.wait()
        dur = await _probe_duration(fixed) if fixed.exists() else 0
        if proc.returncode == 0 and dur > 0.5:
            os.replace(fixed, p)
            job.pieces.append((p.name, dur))
            self.save()
        else:
            for x in (p, fixed):
                x.unlink(missing_ok=True)

    async def _finalize(self, job, pieces):
        """Parçaları yeniden kodlamadan son biçimde birleştirir; görüntü oranını ve MP4 için hvc1 etiketini yazar."""
        out = job.path.with_name(job.path.name + ".part")
        if len(pieces) == 1:
            src = ["-i", str(pieces[0])]
        else:
            lst = job.path.with_name(job.path.name + ".cap.list")
            lst.write_text("".join("file '" + str(p).replace("'", "'\\''") + "'\n" for p in pieces), encoding="utf-8")
            src = ["-f", "concat", "-safe", "0", "-i", str(lst)]
        base = [FFMPEG, "-v", "error", "-y", *src, "-map", "0", "-c", "copy"]
        extra = ["-aspect", job.aspect] if job.aspect else []
        if CONTAINER[job.fmt] == "mp4":
            extra += ["-movflags", "+faststart"]
        tries = [["-tag:v", "hvc1"], []] if CONTAINER[job.fmt] == "mp4" else [[]]
        err = b""
        for tag in tries:
            p = await asyncio.create_subprocess_exec(*base, *tag, *extra, "-f", CONTAINER[job.fmt], str(out),
                                                     stderr=asyncio.subprocess.PIPE, creationflags=NO_WINDOW)
            _, err = await p.communicate()
            if p.returncode == 0 and out.exists():
                break
        if not out.exists() or p.returncode != 0:
            raise RuntimeError("Dosya biçimi yazılamadı: " + err.decode(errors="replace")[-200:])
        os.replace(out, job.path)

    def _cleanup(self, job, keep_final=False):
        """Ara dosyaları (parçalar, liste, yarım çıktı) siler."""
        names = [job.path.name + ".part", job.path.name + ".cap.list", job.path.name + ".cap.mkv"]
        paths = [job.path.parent / n for n in names] + [job.path.with_suffix(".nut.part")]
        paths += list(job.path.parent.glob(glob.escape(job.path.name) + ".cap*"))
        for p in paths:
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass
        if not keep_final:
            job.pieces = []

    async def _capture(self, job, dev, piece):
        """Aralığın kalanını (inmiş parçaların sonundan itibaren) yeni bir parça dosyasına alır.
        Dönüş: (alınan saniye, aralık sonuna ulaşıldı mı)."""
        fmt = job.fmt
        offset = job.done_sec
        remaining = job.duration - offset
        out = _out_args(fmt)
        if job.source:
            src = ["-ss", f"{job.offset + offset:.3f}", "-i", str(job.source)]
        else:
            url = dev.rtsp_url(job.ch, job.sub, job.start + dt.timedelta(seconds=offset),
                               job.end + dt.timedelta(seconds=8), fast=True)
            src = ["-rtsp_transport", "tcp", "-timeout", "20000000", "-i", url]
        args = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y"] + src + ["-t", f"{remaining:.3f}"] + out
        if fmt != "dav":
            args += ["-f", "matroska"]
        args += ["-progress", "pipe:1", "-nostats", str(piece)]
        job.proc = await asyncio.create_subprocess_exec(
            *args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, creationflags=NO_WINDOW)
        last_change = time.monotonic()
        last_t = -1.0
        done = False
        try:
            while True:
                if job.pause_req or job.suspend_req:
                    break
                try:
                    line = await asyncio.wait_for(job.proc.stdout.readline(), 2)
                except asyncio.TimeoutError:
                    line = None
                if line == b"":
                    break
                if line and line.startswith(b"out_time_us="):
                    try:
                        t = int(line.split(b"=")[1]) / 1e6
                    except ValueError:
                        t = last_t
                    if t > last_t + 0.01:
                        last_t = t
                        last_change = time.monotonic()
                        job.progress = min(0.99, max(0.0, (offset + t) / job.duration))
                        job.message = f"Kayıt alınıyor… %{int(job.progress * 100)}"
                if line and line.startswith(b"progress=end"):
                    done = True
                idle = time.monotonic() - last_change
                # Cihaz akışı aralık sonunda kapatmayabilir: sona yaklaştıysak ve veri durduysa bitir
                if last_t >= remaining - 2.5 and idle > 6:
                    done = True
                    break
                if idle > 45:
                    break
            await self._stop(job.proc)
        finally:
            if job.proc.returncode is None:
                job.proc.kill()
        err = (await job.proc.stderr.read()).decode(errors="replace").strip()
        if err:
            log.info("ffmpeg: %s", err[-500:])
        if last_t >= remaining - 2.5:
            done = True
        ok = piece.exists() and piece.stat().st_size > 1000 and last_t > 0
        return (last_t if ok else 0.0), done

    @staticmethod
    async def _stop(proc):
        """ffmpeg'i dosyayı düzgün kapatacak şekilde durdurur ('q' komutu)."""
        if proc.returncode is not None:
            return
        try:
            proc.stdin.write(b"q")
            await proc.stdin.drain()
            proc.stdin.close()
        except Exception:
            pass
        try:
            await asyncio.wait_for(proc.wait(), 8)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()

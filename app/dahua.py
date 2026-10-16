"""
Dahua cihaz bağlantısı: yerel ağ (doğrudan IP) ya da P2P (SN) üzerinden HTTP API ve RTSP.
"""
import asyncio
import datetime as dt
import json
import os
import logging
import re
import socket
import struct
import time
from urllib.parse import quote

import aiohttp

import p2p

log = logging.getLogger("dahua")

TIME_FMT = "%Y-%m-%d %H:%M:%S"


class DeviceError(Exception):
    pass


# ---------------------------------------------------------------- yerel ağ keşfi

def _discover_packet():
    body = json.dumps({"method": "DHDiscover.search", "params": {"mac": "", "uni": 1}}).encode()
    return b"\x20\x00\x00\x00DHIP" + b"\x00" * 8 + struct.pack("<II", len(body), 0) + struct.pack("<II", len(body), 0) + body


async def discover(serial, timeout=2.0):
    """Yerel ağda SN'ye sahip Dahua cihazını arar. Bulursa IP adresini döndürür."""
    loop = asyncio.get_running_loop()
    found = loop.create_future()

    class Proto(asyncio.DatagramProtocol):
        def datagram_received(self, data, addr):
            i = data.find(b"{")
            if i < 0:
                return
            try:
                info = json.loads(data[i:].decode(errors="ignore")).get("params", {}).get("deviceInfo", {})
            except Exception:
                return
            if info.get("SerialNo", "").upper() == serial.upper() and not found.done():
                ip = (info.get("IPv4Address") or {}).get("IPAddress") or addr[0]
                found.set_result(ip)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.bind(("0.0.0.0", 0))
    transport, _ = await loop.create_datagram_endpoint(Proto, sock=sock)
    try:
        pkt = _discover_packet()
        for dst in (("255.255.255.255", 37810), ("239.255.255.251", 37810)):
            try:
                transport.sendto(pkt, dst)
            except OSError:
                pass
        return await asyncio.wait_for(found, timeout)
    except asyncio.TimeoutError:
        return None
    finally:
        transport.close()


async def _tcp_open(host, port, timeout=2.0):
    try:
        r, w = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
        w.close()
        return True
    except Exception:
        return False


# ---------------------------------------------------------------- RTSP aracı

class RtspProxy:
    """
    Yerel RTSP aracısı. İstenirse PLAY isteğine `Rate-Control: no` ekleyerek cihazın
    kaydı ağın izin verdiği en yüksek hızda göndermesini sağlar (hızlı indirme / ileri sarma).
    """

    def __init__(self, upstream, fast):
        self.upstream = upstream      # async () -> (host, port)
        self.fast = fast
        self.server = None
        self.port = None

    async def start(self):
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self.port

    async def _handle(self, cr, cw):
        try:
            host, port = await self.upstream()
            dr, dw = await asyncio.open_connection(host, port, limit=2 ** 20)
        except Exception as e:
            log.warning("RTSP bağlantısı kurulamadı: %s", e)
            cw.close()
            return

        async def up():
            buf = b""
            try:
                while True:
                    d = await cr.read(65536)
                    if not d:
                        break
                    if not self.fast:
                        dw.write(d)
                    else:
                        buf += d
                        while buf:
                            if buf[:1] == b"$":          # istemciden gelen RTCP (interleaved)
                                if len(buf) < 4:
                                    break
                                n = 4 + struct.unpack(">H", buf[2:4])[0]
                                if len(buf) < n:
                                    break
                                dw.write(buf[:n])
                                buf = buf[n:]
                                continue
                            end = buf.find(b"\r\n\r\n")
                            if end < 0:
                                break
                            head, buf = buf[:end], buf[end + 4:]
                            if head.startswith(b"PLAY "):
                                head += b"\r\nRate-Control: no"
                            # gövdesi olan istekler (nadiren) için Content-Length'e uy
                            cl = 0
                            for line in head.split(b"\r\n"):
                                if line.lower().startswith(b"content-length:"):
                                    cl = int(line.split(b":")[1])
                            if len(buf) < cl:
                                buf = head + b"\r\n\r\n" + buf
                                break
                            dw.write(head + b"\r\n\r\n" + buf[:cl])
                            buf = buf[cl:]
                    await dw.drain()
            except Exception:
                pass
            finally:
                dw.close()

        async def down():
            try:
                if self.fast:
                    await self._down_filtered(dr, cw)
                    return
                while True:
                    d = await dr.read(262144)
                    if not d:
                        break
                    cw.write(d)
                    await cw.drain()
            except Exception:
                pass
            finally:
                cw.close()

        await asyncio.gather(up(), down())

    @staticmethod
    async def _down_filtered(dr, cw):
        """
        Hızlı modda cihazın RTCP raporları gerçek saati taşır; ffmpeg bunlarla zaman damgalarını
        sıkıştırır. RTCP (tek numaralı interleaved kanallar) ayıklanır, zaman RTP'den alınır.
        """
        while True:
            first = await dr.readexactly(1)
            if first == b"$":
                hdr = await dr.readexactly(3)
                n = struct.unpack(">H", hdr[1:3])[0]
                data = await dr.readexactly(n)
                if hdr[0] % 2 == 0:
                    cw.write(first + hdr + data)
            else:
                head = first + await dr.readuntil(b"\r\n\r\n")
                cl = 0
                for line in head.split(b"\r\n"):
                    if line.lower().startswith(b"content-length:"):
                        cl = int(line.split(b":")[1])
                cw.write(head + (await dr.readexactly(cl) if cl else b""))
            await cw.drain()

    def close(self):
        if self.server:
            self.server.close()


# ---------------------------------------------------------------- cihaz

class Device:
    def __init__(self, settings, on_status=None):
        self.s = dict(settings)
        self.on_status = on_status or (lambda m: None)
        self.tunnel = None
        self.mode = None
        self.http_base = None
        self.rtsp_addr = None
        self.http = None
        self.info = {}
        self.channels = []
        self.time_offset = 0.0   # cihaz saati - bilgisayar saati (sn)
        self.proxy = None
        self.fast_proxy = None
        self.connected = False

    # ------------------------------------------------ bağlantı
    async def connect(self):
        s = self.s
        if not s.get("serial") and not s.get("ip"):
            raise DeviceError("Ayarlardan cihaz SN numarasını girin.")
        if not s.get("password"):
            raise DeviceError("Ayarlardan cihaz şifresini girin.")

        mode = s.get("mode", "auto")
        ip = (s.get("ip") or "").strip()
        if mode in ("auto", "lan"):
            if not ip and s.get("serial"):
                self.on_status("Yerel ağda cihaz aranıyor…")
                ip = await discover(s["serial"]) or ""
            if ip and await _tcp_open(ip, 80):
                self.mode = "lan"
                self.http_base = f"http://{ip}"
                self.rtsp_addr = (ip, 554)
            elif mode == "lan":
                raise DeviceError("Cihaz yerel ağda bulunamadı. IP adresini kontrol edin.")
        if not self.mode:
            if not s.get("serial"):
                raise DeviceError("P2P bağlantısı için SN numarası gerekli.")
            self.tunnel = p2p.Tunnel(s["serial"], s["username"], s["password"], status=self.on_status)
            try:
                await self.tunnel.ensure()
            except p2p.P2PError as e:
                raise DeviceError(str(e))
            except (OSError, asyncio.TimeoutError):
                raise DeviceError("İnternet bağlantısı yok ya da Dahua sunucusuna ulaşılamıyor.")
            hp = await self.tunnel.expose(80)
            rp = await self.tunnel.expose(554)
            self.mode = "p2p" if self.tunnel.info.get("mode") == "p2p" else "relay"
            self.http_base = f"http://127.0.0.1:{hp}"
            self.rtsp_addr = ("127.0.0.1", rp)

        self.http = aiohttp.ClientSession(
            middlewares=(aiohttp.DigestAuthMiddleware(s["username"], s["password"]),),
            timeout=aiohttp.ClientTimeout(total=20))
        self.on_status("Cihaz bilgileri alınıyor…")
        try:
            dtype = await self.cgi("/cgi-bin/magicBox.cgi?action=getDeviceType")
        except DeviceError as e:
            await self.close()
            raise
        self.info["model"] = _kv(dtype).get("type", "")
        try:
            self.info["version"] = _kv(await self.cgi("/cgi-bin/magicBox.cgi?action=getSoftwareVersion")).get("version", "")
        except DeviceError:
            pass
        await self.sync_time()
        await self.load_perms()
        await self.load_channels()

        async def upstream():
            if self.tunnel:
                await self.tunnel.ensure()
            return self.rtsp_addr

        self.proxy = RtspProxy(upstream, fast=False)
        self.fast_proxy = RtspProxy(upstream, fast=True)
        await self.proxy.start()
        await self.fast_proxy.start()
        self.connected = True

    async def close(self):
        self.connected = False
        for p in (self.proxy, self.fast_proxy):
            if p:
                p.close()
        if self.http:
            await self.http.close()
            self.http = None
        if self.tunnel:
            self.tunnel.close()
            self.tunnel = None

    # ------------------------------------------------ HTTP API
    async def cgi(self, path, retries=1):
        if os.environ.get("GUVENLIK_DEBUG"):
            log.info("CGI %s", path[:120])
        for i in range(retries + 1):
            try:
                if self.tunnel:
                    await self.tunnel.ensure()
                async with self.http.get(self.http_base + path) as r:
                    text = await r.text(errors="replace")
                    if r.status == 401:
                        raise DeviceError("Kullanıcı adı veya şifre hatalı.")
                    if r.status >= 400:
                        raise DeviceError(f"Cihaz isteği reddetti ({r.status})")
                    return text
            except DeviceError:
                raise
            except Exception as e:
                if i == retries:
                    raise DeviceError(f"Cihaza ulaşılamadı: {e.__class__.__name__}")
                await asyncio.sleep(0.5)

    async def load_channels(self):
        names = _kv(await self.cgi("/cgi-bin/configManager.cgi?action=getConfig&name=ChannelTitle"))
        chans = []
        i = 0
        while f"table.ChannelTitle[{i}].Name" in names:
            chans.append({"id": i + 1, "name": names[f"table.ChannelTitle[{i}].Name"] or f"Kamera {i + 1}"})
            i += 1
        # ana akış çözünürlüğü: ızgaradaki alt akış farklı oranda olabilir, görüntü buna göre düzeltilir
        try:
            enc = _kv(await self.cgi("/cgi-bin/configManager.cgi?action=getConfig&name=Encode"))
            for c in chans:
                w = enc.get(f"table.Encode[{c['id'] - 1}].MainFormat[0].Video.Width")
                h = enc.get(f"table.Encode[{c['id'] - 1}].MainFormat[0].Video.Height")
                if w and h and int(h):
                    c["aspect"] = round(int(w) / int(h), 4)
        except (DeviceError, ValueError):
            pass
        self.channels = chans
        await self.detect_active()

    async def detect_active(self):
        """Son 2 günde kaydı olan kanalları 'kamera bağlı' kabul eder (yeni kameralar kendiliğinden eklenir)."""
        end = self.now() + dt.timedelta(minutes=5)
        start = end - dt.timedelta(days=2)
        sem = asyncio.Semaphore(3)
        prev = {c["id"]: c["active"] for c in getattr(self, "_known", []) if "active" in c}

        async def check(c):
            async with sem:
                for attempt in range(2):
                    try:
                        c["active"] = bool(await self.find_records(c["id"], start, end))
                        return
                    except DeviceError:
                        await asyncio.sleep(0.5)
                c["active"] = prev.get(c["id"], False)

        await asyncio.gather(*[check(c) for c in self.channels])
        if not any(c.get("active") for c in self.channels):
            for c in self.channels:      # hiçbiri tespit edilemediyse hepsini göster
                c["active"] = True
        self._known = [dict(c) for c in self.channels]

    async def rename_channel(self, ch, name):
        await self.cgi(f"/cgi-bin/configManager.cgi?action=setConfig&ChannelTitle[{ch - 1}].Name={quote(name)}")
        await self.load_channels()

    # ------------------------------------------------ kamera görüntü ayarları
    async def camera_config(self, ch):
        """Bir kanalın kodlama ayarlarını ve cihazın izin verdiği sınırları döndürür."""
        i = ch - 1
        enc = _kv(await self.cgi("/cgi-bin/configManager.cgi?action=getConfig&name=Encode"))
        caps = _kv(await self.cgi(f"/cgi-bin/encode.cgi?action=getConfigCaps&channel={ch}"))

        async def flag(name):
            try:
                return _kv(await self.cgi(f"/cgi-bin/configManager.cgi?action=getConfig&name={name}", retries=0)) \
                    .get(f"table.{name}[{i}].Enable") == "true"
            except DeviceError:
                return None

        def stream(kind):
            p = f"table.Encode[{i}].{kind}[0]."
            v = lambda k: enc.get(p + "Video." + k, "")
            return {
                "enabled": enc.get(p + "VideoEnable") == "true",
                "resolution": v("resolution") or f"{v('Width')}x{v('Height')}",
                "fps": _num(v("FPS")), "bitrate": _num(v("BitRate")), "control": v("BitRateControl"),
                "quality": _num(v("Quality")), "gop": _num(v("GOP")), "compression": v("Compression"),
                "audio": enc.get(p + "AudioEnable") == "true",
            }

        def cap(kind):
            p = f"caps[0].{kind}[0].Video."
            br = [_num(x) for x in caps.get(p + "BitRateOptions", "").split(",") if x.strip()]
            return {
                "resolutions": [RES_SIZES.get(x.strip(), x.strip()) for x in caps.get(p + "ResolutionTypes", "").split(",") if x.strip()],
                "fps_max": _num(caps.get(p + "FPSMax")) or 25,
                "bitrate_min": min(br) if br else 32, "bitrate_max": max(br) if br else 4096,
                "gop_max": _num(caps.get(p + "GOPRange[1]")) or 50,
                "compressions": [x for x in caps.get(p + "CompressionTypes", "H.264,H.265").split(",") if x],
            }

        return {
            "channel": ch,
            "main": stream("MainFormat"), "sub": stream("ExtraFormat"),
            "caps": {"main": cap("MainFormat"), "sub": cap("ExtraFormat")},
            "smart": await flag("SmartEncode"), "ai": await flag("AICoding"),
            "enhance": await self.encode_enhance(),
            "disk_bytes": await self.disk_bytes(),
        }

    async def _main_fps(self, chans):
        enc = _kv(await self.cgi("/cgi-bin/configManager.cgi?action=getConfig&name=Encode"))
        return {ch: _num(enc.get(f"table.Encode[{ch - 1}].MainFormat[0].Video.FPS")) for ch in chans}

    async def _set_fps(self, ch, fps, gop, sub_fps=None):
        q = f"Encode[{ch - 1}].MainFormat[0].Video.FPS={fps}&Encode[{ch - 1}].MainFormat[0].Video.GOP={gop}"
        if sub_fps:
            q += f"&Encode[{ch - 1}].ExtraFormat[0].Video.FPS={sub_fps}"
        try:
            await self.cgi("/cgi-bin/configManager.cgi?action=setConfig&" + q, retries=0)
        except DeviceError:
            pass

    async def apply_many(self, chans, cfg, say=lambda m: None):
        """
        Aynı ayarı birden çok kameraya uygular. Kayıt cihazı kodlama gücünü ilk gelene verdiği için
        kare hızı eşit paylaştırılır: önce hepsi düşürülür, sonra ortak hedef denenir, sığmazsa azaltılır.
        Akıllı kodlama ve AI kodlama kameraya özel olduğundan burada değiştirilmez.
        """
        m, sub = dict(cfg.get("main", {})), dict(cfg.get("sub", {}))
        want_fps, want_gop = m.get("fps"), m.get("gop")
        ratio = (want_gop / want_fps) if want_fps and want_gop else 1.0
        want_sub = sub.get("fps")
        base = {"main": {**m, "fps": 1, "gop": 1}, "sub": {**sub, "fps": 1}}
        for n, ch in enumerate(chans, 1):
            say(f"Ayarlar yazılıyor ({n}/{len(chans)})…")
            await self.set_camera_config(ch, base, verify=False)
            await asyncio.sleep(0.5)
        target, reached = want_fps or 1, None
        for attempt in range(8):
            say(f"Kare hızı {target} fps deneniyor…")
            if attempt:
                for ch in chans:
                    await self._set_fps(ch, 1, 1, 1)
                await asyncio.sleep(1)
            gop = max(1, round(target * ratio))
            for ch in chans:
                await self._set_fps(ch, target, gop, min(want_sub or target, target))
                await asyncio.sleep(0.3)
            await asyncio.sleep(3)
            got = await self._main_fps(chans)
            if all(v == target for v in got.values()):
                reached = target
                break
            vals = [v or 1 for v in got.values()]
            target = max(1, min(target - 1, sum(vals) // len(vals) + 1))
        # Kare hızı 1'e indirildiğinde cihaz bit hızı sınırını da düşürür (ör. 1024); kare hızı
        # oturduktan sonra bit hızı, türü ve kalitesi yeniden yazılır.
        say("Bit hızı ayarlanıyor…")
        for ch in chans:
            i = ch - 1
            pairs = [(f"Encode[{i}].MainFormat[0].Video.{k}", v) for k, v in
                     (("BitRateControl", m.get("control")), ("Quality", m.get("quality")), ("BitRate", m.get("bitrate")))
                     if v is not None]
            if sub.get("bitrate"):
                pairs.append((f"Encode[{i}].ExtraFormat[0].Video.BitRate", sub["bitrate"]))
            if pairs:
                try:
                    await self.cgi("/cgi-bin/configManager.cgi?action=setConfig&" +
                                   "&".join(f"{k}={v}" for k, v in pairs), retries=0)
                except DeviceError:
                    pass
            await asyncio.sleep(0.3)
        await asyncio.sleep(2)
        say("Sonuç okunuyor…")
        results, notes = {}, []
        for ch in chans:
            cur = await self.camera_config(ch)
            results[ch] = {"resolution": cur["main"]["resolution"], "fps": cur["main"]["fps"], "gop": cur["main"]["gop"],
                           "control": cur["main"]["control"], "bitrate": cur["main"]["bitrate"], "sub_fps": cur["sub"]["fps"]}
            if m.get("bitrate") and cur["main"]["bitrate"] != m["bitrate"]:
                notes.append(f"Kamera {ch}: bit hızı {m['bitrate']} istendi, cihaz {cur['main']['bitrate']} bıraktı")
        if want_fps and reached != want_fps:
            notes.append(f"Kayıt cihazının kodlama gücü {len(chans)} kamerada {want_fps} fps'e yetmiyor; "
                         f"hepsine eşit olarak {reached or min(r['fps'] for r in results.values())} fps verildi.")
        await self.load_channels()
        return results, notes

    async def encode_enhance(self):
        """Kodlama Geliştirme modu: True/False, cihaz desteklemiyorsa None."""
        try:
            r = _kv(await self.cgi("/cgi-bin/configManager.cgi?action=getConfig&name=EncodeEnhance", retries=0))
        except DeviceError:
            return None
        v = r.get("table.EncodeEnhance.Enable")
        return None if v is None else v == "true"

    async def set_encode_enhance(self, on, reboot=True):
        """Modu değiştirir; cihaz yeni kodlama gücünü ancak yeniden başlayınca kullanır."""
        r = await self.cgi(f"/cgi-bin/configManager.cgi?action=setConfig&EncodeEnhance.Enable={'true' if on else 'false'}", retries=0)
        if "OK" not in r:
            raise DeviceError("Cihaz Kodlama Geliştirme ayarını kabul etmedi")
        if reboot:
            await self.cgi("/cgi-bin/magicBox.cgi?action=reboot", retries=0)

    # ------------------------------------------------ hareket algılama ve uyarılar
    async def motion_config(self, ch):
        i = ch - 1
        md = _kv(await self.cgi("/cgi-bin/configManager.cgi?action=getConfig&name=MotionDetect"))
        try:
            smd = _kv(await self.cgi("/cgi-bin/configManager.cgi?action=getConfig&name=SmartMotionDetect", retries=0))
        except DeviceError:
            smd = {}
        m = lambda k: md.get(f"table.MotionDetect[{i}].{k}")
        s = lambda k: smd.get(f"table.SmartMotionDetect[{i}].{k}")
        on = lambda v: str(v).lower() in ("true", "1")
        return {
            "md": on(m("Enable")), "md_sens": _num(m("MotionDetectWindow[0].Sensitive")) or 50,
            "smd_supported": s("Enable") is not None,
            "smd": on(s("Enable")), "human": on(s("ObjectTypes.Human")), "vehicle": on(s("ObjectTypes.Vehicle")),
            "smd_sens": s("Sensitivity") or "Middle",
            "light": on(m("EventHandler.LightAlarm")), "siren": on(m("EventHandler.Speaker")),
            "beep": on(m("EventHandler.BeepEnable")), "log": on(m("EventHandler.LogEnable")),
            "record": on(m("EventHandler.RecordEnable")),
            "_style": {k: ("bool" if str(m(f"EventHandler.{k}")).lower() in ("true", "false") else "num")
                       for k in ("LightAlarm", "Speaker")},
        }

    async def set_motion_config(self, ch, cfg):
        """
        Hareket algılama ayarlarını yazar. Uyarı ışığı/siren alanları bazı kanallarda 1/0, bazılarında
        true/false tutuluyor; mevcut biçim korunur. Akıllı hareket, hareket algılama açıldıktan sonra ayrı yazılır.
        """
        i = ch - 1
        cur = await self.motion_config(ch)
        style = cur["_style"]
        b = lambda v: "true" if v else "false"
        fmt = lambda key, v: (b(v) if style.get(key) == "bool" else ("1" if v else "0"))
        pairs = []
        if "md" in cfg:
            pairs.append((f"MotionDetect[{i}].Enable", b(cfg["md"])))
        if cfg.get("md_sens") is not None:
            pairs.append((f"MotionDetect[{i}].MotionDetectWindow[0].Sensitive", max(1, min(100, int(cfg["md_sens"])))))
        for key, name in (("light", "LightAlarm"), ("siren", "Speaker")):
            if key in cfg:
                pairs.append((f"MotionDetect[{i}].EventHandler.{name}", fmt(name, cfg[key])))
        for key, name in (("beep", "BeepEnable"), ("log", "LogEnable"), ("record", "RecordEnable")):
            if key in cfg:
                pairs.append((f"MotionDetect[{i}].EventHandler.{name}", b(cfg[key])))
        warnings = []
        for k, v in pairs:          # tek tek: biri reddedilirse diğerleri etkilenmesin
            try:
                r = await self.cgi(f"/cgi-bin/configManager.cgi?action=setConfig&{k}={v}", retries=0)
                ok = "OK" in r
            except DeviceError:
                ok = False
            if not ok:
                warnings.append(f"Cihaz şu ayarı kabul etmedi: {k.split('.')[-1]}")
        if cur["smd_supported"]:
            for key, name, val in (("smd", "Enable", None), ("human", "ObjectTypes.Human", None),
                                   ("vehicle", "ObjectTypes.Vehicle", None), ("smd_sens", "Sensitivity", "str")):
                if key not in cfg:
                    continue
                v = cfg[key] if val == "str" else b(cfg[key])
                try:
                    r = await self.cgi(f"/cgi-bin/configManager.cgi?action=setConfig&SmartMotionDetect[{i}].{name}={v}", retries=0)
                    ok = "OK" in r
                except DeviceError:
                    ok = False
                if not ok:
                    warnings.append(f"Cihaz akıllı hareket ayarını kabul etmedi: {name}")
        await asyncio.sleep(2)
        final = await self.motion_config(ch)
        for key, label in (("md", "Hareket algılama"), ("smd", "Akıllı hareket"), ("light", "Uyarı ışığı"),
                           ("siren", "Siren"), ("beep", "Cihaz bipi")):
            if key in cfg and final.get(key) != bool(cfg[key]):
                warnings.append(f"{label}: cihaz {'açık' if final[key] else 'kapalı'} bıraktı")
        return final, warnings

    async def disk_info(self):
        """Diskler, doluluk ve her kameranın diskte duran kayıt aralığı (hafif sorgular)."""
        info = await self.cgi("/cgi-bin/storageDevice.cgi?action=getDeviceAllInfo")
        disks = {}
        for line in info.splitlines():
            m = re.match(r"list\.info\[(\d+)\]\.(?:Detail\[(\d+)\]\.)?(\w+)=(.*)", line.strip())
            if not m:
                continue
            di, part, key, val = int(m.group(1)), m.group(2), m.group(3), m.group(4)
            d = disks.setdefault(di, {"name": "", "state": "", "parts": {}})
            if part is None:
                d[{"Name": "name", "State": "state"}.get(key, key)] = val
            else:
                d["parts"].setdefault(int(part), {})[key] = val
        out_disks = []
        for di in sorted(disks):
            d = disks[di]
            parts = [d["parts"][k] for k in sorted(d["parts"])]
            total = sum(float(p.get("TotalBytes", 0)) for p in parts)
            used = sum(float(p.get("UsedBytes", 0)) for p in parts)
            out_disks.append({"name": d["name"] or f"Disk {di + 1}", "state": d["state"],
                              "total": int(total), "used": int(used),
                              "error": any(p.get("IsError") == "true" for p in parts),
                              "partitions": [{"path": p.get("Path", ""), "total": int(float(p.get("TotalBytes", 0))),
                                              "used": int(float(p.get("UsedBytes", 0))), "type": p.get("Type", ""),
                                              "error": p.get("IsError") == "true"} for p in parts]})
        try:
            sg = _kv(await self.cgi("/cgi-bin/configManager.cgi?action=getConfig&name=StorageGroup", retries=0))
            overwrite = sg.get("table.StorageGroup[0].OverWrite") == "1"
        except DeviceError:
            overwrite = None
        now = self.now()
        ranges = []
        for c in self.channels:
            if not c.get("active", True):
                continue
            oldest = None
            oid = _kv(await self.cgi("/cgi-bin/mediaFileFind.cgi?action=factory.create")).get("result")
            try:
                await self.cgi(f"/cgi-bin/mediaFileFind.cgi?action=findFile&object={oid}&condition.Channel={c['id']}"
                               f"&condition.StartTime={quote('2000-01-01 00:00:00')}"
                               f"&condition.EndTime={quote(now.strftime(TIME_FMT))}&condition.Types[0]=dav", retries=0)
                r = _kv(await self.cgi(f"/cgi-bin/mediaFileFind.cgi?action=findNextFile&object={oid}&count=1", retries=0))
                oldest = r.get("items[0].StartTime")
            except DeviceError:
                pass
            finally:
                try:
                    await self.cgi(f"/cgi-bin/mediaFileFind.cgi?action=destroy&object={oid}", retries=0)
                except DeviceError:
                    pass
            ranges.append({"channel": c["id"], "name": c["name"], "oldest": oldest, "newest": now.strftime(TIME_FMT)})
        return {"disks": out_disks, "overwrite": overwrite, "ranges": ranges, "now": now.strftime(TIME_FMT)}

    async def change_own_password(self, old, new):
        if old != self.s["password"]:
            raise DeviceError("Mevcut şifre yanlış.")
        await self._user_call("modifyPassword", [("name", self.s["username"]), ("pwd", new), ("pwdOld", old)])

    async def disk_bytes(self):
        try:
            info = _kv(await self.cgi("/cgi-bin/storageDevice.cgi?action=getDeviceAllInfo", retries=0))
            return int(sum(float(v) for k, v in info.items() if k.endswith(".TotalBytes")))
        except (DeviceError, ValueError):
            return 0

    async def set_camera_config(self, ch, cfg, verify=True):
        """
        Ayarları cihaza yazar. Önce çözünürlük yazılır (fps sınırı çözünürlüğe bağlıdır), sonra kalan
        değerler cihazın yeni sınırlarına göre kırpılarak yazılır. Kabul edilmeyen değer olursa bildirir.
        """
        i = ch - 1
        warnings = []

        async def put(pairs):
            if not pairs:
                return
            q = "&".join(f"{k}={quote(str(v), safe='x.')}" for k, v in pairs)
            try:
                r = await self.cgi("/cgi-bin/configManager.cgi?action=setConfig&" + q, retries=0)
                if "OK" in r:
                    return
            except DeviceError:
                pass
            # toplu yazım reddedildi: hangisinin sorunlu olduğunu bulmak için tek tek dene
            for k, v in pairs:
                try:
                    r = await self.cgi(f"/cgi-bin/configManager.cgi?action=setConfig&{k}={quote(str(v), safe='x.')}", retries=0)
                    ok = "OK" in r
                except DeviceError:
                    ok = False
                if not ok:
                    warnings.append(f"Cihaz şu ayarı kabul etmedi: {LABELS.get(k.split('.')[-1], k.split('.')[-1])} = {v}")

        # Akıllı kodlama açıkken cihaz bit hızı türünü/GOP'u kilitler: bu bayraklar önce, tek tek yazılır.
        # (Aynı istekte birlikte gönderilince cihaz AI kodlamayı yok sayabiliyor.)
        if cfg.get("smart") is not None:
            await put([(f"SmartEncode[{i}].Enable", "true" if cfg["smart"] else "false")])
        if cfg.get("ai") is not None:
            await put([(f"AICoding[{i}].Enable", "true" if cfg["ai"] else "false")])
        if cfg.get("smart") is not None or cfg.get("ai") is not None:
            await asyncio.sleep(1)

        m, s = cfg.get("main", {}), cfg.get("sub", {})
        first = []
        if m.get("resolution"):
            first.append((f"Encode[{i}].MainFormat[0].Video.resolution", m["resolution"]))
        if m.get("compression"):
            first.append((f"Encode[{i}].MainFormat[0].Video.Compression", m["compression"]))
        if s.get("resolution"):
            first.append((f"Encode[{i}].ExtraFormat[0].Video.resolution", s["resolution"]))
        if s.get("compression"):
            first.append((f"Encode[{i}].ExtraFormat[0].Video.Compression", s["compression"]))
        await put(first)

        cur = await self.camera_config(ch)
        cm, cs = cur["caps"]["main"], cur["caps"]["sub"]

        def clamp(val, lo, hi, label):
            if val is None:
                return None
            v = max(lo, min(hi, int(val)))
            if v != int(val):
                warnings.append(f"{label} cihaz sınırı nedeniyle {val} yerine {v} olarak ayarlandı")
            return v

        rest = []
        P = f"Encode[{i}].MainFormat[0]."
        fps = clamp(m.get("fps"), 1, cm["fps_max"], "Kare hızı")
        for key, val in (("Video.FPS", fps),
                         ("Video.BitRateControl", m.get("control")),
                         ("Video.Quality", m.get("quality")),
                         ("Video.BitRate", clamp(m.get("bitrate"), cm["bitrate_min"], cm["bitrate_max"], "Bit hızı")),
                         ("Video.GOP", clamp(m.get("gop"), 1, cm["gop_max"], "GOP"))):
            if val is not None:
                rest.append((P + key, val))
        if "audio" in m:
            rest.append((P + "AudioEnable", "true" if m["audio"] else "false"))
        P = f"Encode[{i}].ExtraFormat[0]."
        for key, val in (("Video.FPS", clamp(s.get("fps"), 1, cs["fps_max"], "Canlı akış kare hızı")),
                         ("Video.BitRate", clamp(s.get("bitrate"), cs["bitrate_min"], cs["bitrate_max"], "Canlı akış bit hızı")),
                         ("Video.GOP", clamp(s.get("gop"), 1, cs["gop_max"], "Canlı akış GOP"))):
            if val is not None:
                rest.append((P + key, val))
        await put(rest)
        if not verify:
            return None, warnings
        # Cihaz ayarları gecikmeyle uygular ve kapasite yetmezse "OK" deyip sessizce düşürür:
        # bekleyip gerçekte ne kaldığını okuyarak istenenle karşılaştır.
        await asyncio.sleep(3)
        final = await self.camera_config(ch)
        checks = [("main", "resolution", "Çözünürlük", m.get("resolution")),
                  ("main", "fps", "Kare hızı", fps),
                  ("main", "gop", "GOP", m.get("gop")),
                  ("main", "control", "Bit hızı türü", m.get("control")),
                  ("main", "bitrate", "Bit hızı", m.get("bitrate")),
                  ("main", "audio", "Ses kaydı", m.get("audio")),
                  ("sub", "resolution", "Canlı akış çözünürlüğü", s.get("resolution")),
                  ("sub", "fps", "Canlı akış kare hızı", s.get("fps"))]
        capacity = False
        if final.get("smart"):
            checks = [c for c in checks if c[1] not in ("control", "gop")]
            if m.get("control") and final["main"].get("control") != m.get("control"):
                warnings.append("Akıllı kodlama açıkken bit hızı türünü ve GOP'u cihaz kendisi yönetir.")
        for part, key, label, want in checks:
            got = final[part].get(key)
            if want is None or str(got) == str(want) or any(w.startswith(label) for w in warnings):
                continue
            if key in ("fps", "resolution"):
                capacity = True
            show = lambda v: ("açık" if v else "kapalı") if isinstance(v, bool) else v
            warnings.append(f"{label}: {show(want)} istendi, cihaz {show(got)} olarak ayarladı")
        for key, label in (("smart", "Akıllı kodlama"), ("ai", "AI kodlama")):
            want = cfg.get(key)
            if want is not None and final.get(key) is not None and final[key] != want:
                warnings.append(f"{label}: cihaz {'açık' if final[key] else 'kapalı'} bıraktı")
        if capacity:
            warnings.append("Kayıt cihazının toplam kodlama gücü sınırlıdır; diğer kameraların ayarları bu kameraya "
                            "kalan payı belirler. Bu kameranın ayarını yükseltmek için diğerlerinin kare hızını "
                            "ya da çözünürlüğünü düşürün.")
        await self.load_channels()
        return final, warnings

    async def sync_time(self):
        try:
            t0 = time.time()
            r = _kv(await self.cgi("/cgi-bin/global.cgi?action=getCurrentTime"))
            dev = dt.datetime.strptime(r["result"], TIME_FMT)
            self.time_offset = dev.timestamp() - (t0 + time.time()) / 2
        except Exception:
            self.time_offset = 0.0

    def now(self):
        """Cihazın yerel saati (naive datetime)."""
        return dt.datetime.fromtimestamp(time.time() + self.time_offset)

    async def find_records(self, ch, start, end):
        """Belirtilen aralıktaki kayıt parçalarını döndürür: [(başlangıç, bitiş, tür)]"""
        oid = _kv(await self.cgi("/cgi-bin/mediaFileFind.cgi?action=factory.create")).get("result")
        if not oid:
            return []
        out = []
        try:
            try:
                await self.cgi(
                    f"/cgi-bin/mediaFileFind.cgi?action=findFile&object={oid}&condition.Channel={ch}"
                    f"&condition.StartTime={quote(start.strftime(TIME_FMT))}&condition.EndTime={quote(end.strftime(TIME_FMT))}"
                    f"&condition.Types[0]=dav")
            except DeviceError as e:
                if "(400)" in str(e):   # bu aralıkta kayıt yok
                    return []
                raise
            for _ in range(50):
                r = _kv(await self.cgi(f"/cgi-bin/mediaFileFind.cgi?action=findNextFile&object={oid}&count=100"))
                n = int(r.get("found", "0") or 0)
                for i in range(n):
                    try:
                        a = dt.datetime.strptime(r[f"items[{i}].StartTime"], TIME_FMT)
                        b = dt.datetime.strptime(r[f"items[{i}].EndTime"], TIME_FMT)
                    except (KeyError, ValueError):
                        continue
                    flags = [v for k, v in r.items() if k.startswith(f"items[{i}].Flags")]
                    kind = "event" if any(f in ("Event", "Alarm", "Motion", "VideoMotion") for f in flags) else "normal"
                    out.append((a, b, kind))
                if n < 100:
                    break
        except DeviceError:
            if not out:
                raise
        finally:
            try:
                await self.cgi(f"/cgi-bin/mediaFileFind.cgi?action=close&object={oid}", retries=0)
                await self.cgi(f"/cgi-bin/mediaFileFind.cgi?action=destroy&object={oid}", retries=0)
            except DeviceError:
                pass
        out.sort()
        return out

    async def rec_files(self, ch, start, end, events_only=False):
        """
        Diskteki kayıt dosyaları (ayrıntılı). Hareket kaydı açıkken cihaz hareketli bölümleri ayrı
        "Event" dosyaları olarak yazar; events_only ile yalnızca onlar istenir (hızlı olay listesi).
        """
        oid = _kv(await self.cgi("/cgi-bin/mediaFileFind.cgi?action=factory.create")).get("result")
        if not oid:
            return []
        out = []
        try:
            try:
                await self.cgi(
                    f"/cgi-bin/mediaFileFind.cgi?action=findFile&object={oid}&condition.Channel={ch}"
                    f"&condition.StartTime={quote(start.strftime(TIME_FMT))}&condition.EndTime={quote(end.strftime(TIME_FMT))}"
                    f"&condition.Types[0]=dav" + ("&condition.Flags[0]=Event" if events_only else ""), retries=0)
            except DeviceError as e:
                if "(400)" in str(e):
                    return []
                raise
            for _ in range(200):
                text = await self.cgi(f"/cgi-bin/mediaFileFind.cgi?action=findNextFile&object={oid}&count=100")
                items = _list_items(text, "items")
                for it in items:
                    flags = it.get("Flags", [])
                    events = it.get("Events", [])
                    out.append({"channel": ch, "start": it.get("StartTime", "").replace(" ", "T"),
                                "end": it.get("EndTime", "").replace(" ", "T"),
                                "size": int(it.get("Length", "0") or 0),
                                "flags": flags, "events": events,
                                "kind": "event" if "Event" in flags else "normal"})
                if len(items) < 100:
                    break
        finally:
            try:
                await self.cgi(f"/cgi-bin/mediaFileFind.cgi?action=destroy&object={oid}", retries=0)
            except DeviceError:
                pass
        out.sort(key=lambda x: x["start"])
        return out

    # ------------------------------------------------ RTSP
    def rtsp_url(self, ch, sub=0, start=None, end=None, fast=False):
        u, p = quote(self.s["username"], safe=""), quote(self.s["password"], safe="")
        port = (self.fast_proxy if fast else self.proxy).port
        base = f"rtsp://{u}:{p}@127.0.0.1:{port}"
        if start is None:
            return f"{base}/cam/realmonitor?channel={ch}&subtype={sub}"
        end = end or (start + dt.timedelta(hours=24))
        f = "%Y_%m_%d_%H_%M_%S"
        return f"{base}/cam/playback?channel={ch}&subtype={sub}&starttime={start.strftime(f)}&endtime={end.strftime(f)}"

    async def http_stream(self, path):
        """Ham HTTP yanıtı (dosya indirme denemeleri için)."""
        if self.tunnel:
            await self.tunnel.ensure()
        return await self.http.get(self.http_base + path, timeout=aiohttp.ClientTimeout(total=None, sock_read=20))

    # ------------------------------------------------ kullanıcılar ve yetkiler
    async def load_perms(self):
        """Oturum açan kullanıcının yetkileri (sekmelerin görünürlüğü buna göre belirlenir)."""
        try:
            u = _list_block(await self.cgi(f"/cgi-bin/userManager.cgi?action=getUserInfo&name={quote(self.s['username'])}",
                                           retries=0), "user")
            auth = u.get("AuthorityList", [])
            self.perms = {
                "group": u.get("Group", ""),
                "users": "AuthUserMag" in auth,
                "config": "AuthSysCfg" in auth or "AuthStoreCfg" in auth,
                "backup": "AuthBackup" in auth,
                "monitor": sorted(int(a[8:]) for a in auth if a.startswith("Monitor_") and a[8:].isdigit()),
                "replay": sorted(int(a[7:]) for a in auth if a.startswith("Replay_") and a[7:].isdigit()),
            }
        except DeviceError:
            # yetki okunamıyorsa (bazı kullanıcılar kendi bilgisini okuyamaz) en kısıtlı varsayım
            self.perms = {"group": "", "users": False, "config": False, "backup": True, "monitor": [], "replay": []}
        return self.perms

    async def users(self):
        text = await self.cgi("/cgi-bin/userManager.cgi?action=getUserInfoAll")
        groups = await self.cgi("/cgi-bin/userManager.cgi?action=getGroupInfoAll")
        try:
            active = _kv(await self.cgi("/cgi-bin/userManager.cgi?action=getActiveUserInfoAll", retries=0))
        except DeviceError:
            active = {}
        online = {}
        for k, v in active.items():
            if k.endswith(".Name"):
                online[v] = online.get(v, 0) + 1
        users = []
        for u in _list_items(text, "users"):
            users.append({
                "name": u.get("Name", ""), "group": u.get("Group", ""), "memo": u.get("Memo", ""),
                "reserved": u.get("Reserved") == "true", "sharable": u.get("Sharable") == "true",
                "authorities": u.get("AuthorityList", []),
                "password_changed": re.sub(r"^(\d{4}):(\d{2}):(\d{2})-", r"\1-\2-\3 ", u.get("PasswordModifiedTime", "")),
                "sessions": online.get(u.get("Name", ""), 0),
            })
        group_list = [{"name": g.get("Name", ""), "memo": g.get("Memo", ""), "authorities": g.get("AuthorityList", [])}
                      for g in _list_items(groups, "group")]
        return {"users": users, "groups": group_list, "channels": len(self.channels)}

    async def _user_call(self, action, params):
        q = "&".join(f"{k}={quote(str(v), safe='')}" for k, v in params)
        r = await self.cgi(f"/cgi-bin/userManager.cgi?action={action}&{q}", retries=0)
        if "OK" not in r:
            raise DeviceError("Cihaz isteği kabul etmedi: " + r.strip()[:120])

    @staticmethod
    def _user_params(prefix, u):
        p = []
        for key, field in (("Group", "group"), ("Memo", "memo")):
            if u.get(field) is not None:
                p.append((f"{prefix}.{key}", u[field]))
        if u.get("sharable") is not None:
            p.append((f"{prefix}.Sharable", "true" if u["sharable"] else "false"))
        for n, a in enumerate(u.get("authorities") or []):
            p.append((f"{prefix}.AuthorityList[{n}]", a))
        return p

    async def add_user(self, u):
        params = [("user.Name", u["name"]), ("user.Password", u["password"]), ("user.Reserved", "false")]
        params += self._user_params("user", {**u, "sharable": u.get("sharable", True), "authorities": None})
        await self._user_call("addUser", params)

    async def modify_user(self, name, u):
        params = [("name", name)] + self._user_params("user", {**u, "authorities": None})
        await self._user_call("modifyUser", params)
        if u.get("password"):
            await self.set_user_password(name, u["password"], old=u.get("old_password"), group=u.get("group"),
                                         memo=u.get("memo"))

    async def set_user_password(self, name, pwd, old=None, group=None, memo=None):
        """
        Bu cihaz yazılımı (test edildi) yöneticinin başka kullanıcının şifresini değiştirmesine izin vermiyor;
        yalnızca eski şifre bilinerek değiştirilebiliyor. Eski şifre yoksa kullanıcı aynı ad, grup ve
        açıklamayla yeni şifreyle yeniden oluşturulur.
        """
        try:
            await self._user_call("modifyPasswordByManager", [("userName", name), ("pwd", pwd),
                                                              ("managerName", self.s["username"]),
                                                              ("managerPwd", self.s["password"])])
            return
        except DeviceError:
            pass
        if name == self.s["username"]:
            old = old or self.s["password"]
        if old:
            await self._user_call("modifyPassword", [("name", name), ("pwd", pwd), ("pwdOld", old)])
            return
        await self.delete_user(name)
        try:
            await self.add_user({"name": name, "password": pwd, "group": group or "user", "memo": memo or ""})
        except DeviceError as e:
            raise DeviceError(f"Kullanıcı silindi ama yeniden oluşturulamadı ({e}). Lütfen yeni kullanıcı olarak ekleyin.")

    async def delete_user(self, name):
        if name == self.s["username"]:
            raise DeviceError("Oturum açtığınız kullanıcıyı silemezsiniz.")
        await self._user_call("deleteUser", [("name", name)])

    async def _log_window(self, start, end, limit):
        r = _kv(await self.cgi(f"/cgi-bin/log.cgi?action=startFind&condition.StartTime={quote(start.strftime(TIME_FMT))}"
                               f"&condition.EndTime={quote(end.strftime(TIME_FMT))}&condition.Translate=true"))
        token = r.get("token")
        if not token:
            return []
        out = []
        try:
            while len(out) < limit:
                items = _list_items(await self.cgi(f"/cgi-bin/log.cgi?action=doFind&token={token}&count=500"), "items")
                for it in items:
                    out.append({"time": it.get("Time", ""), "type": it.get("Type", ""), "user": it.get("User", ""),
                                "detail": it.get("Detail", "").strip()})
                if not items or len(items) < 100:
                    break
        finally:
            try:
                await self.cgi(f"/cgi-bin/log.cgi?action=stopFind&token={token}", retries=0)
            except DeviceError:
                pass
        return [x for x in out if start.strftime(TIME_FMT) <= x["time"] <= end.strftime(TIME_FMT)]

    async def logs(self, start, end, keep=None, max_items=20000):
        """
        Cihaz loglarını eskiden yeniye döndürür. Cihaz eskiden yeniye verdiği için zaman geriye doğru
        dilimlere bölünür; böylece sınıra gelinirse en yeni olaylar değil en eskiler dışarıda kalır.
        keep: isteğe bağlı süzgeç (ör. belirli kullanıcı).
        """
        chunks, t = [], end
        step = dt.timedelta(hours=3)
        while t > start:
            a = max(start, t - step)
            chunks.append((a, t))
            t = a
        result, seen = [], 0
        for a, b in chunks:
            items = await self._log_window(a, b, max_items)
            seen += len(items)
            if keep:
                items = [x for x in items if keep(x)]
            result = items + result
            if seen >= max_items:
                break
        return result

    def summary(self):
        return {
            "connected": self.connected,
            "mode": self.mode,
            "model": self.info.get("model", ""),
            "version": self.info.get("version", ""),
            "channels": self.channels,
            "now": self.now().strftime(TIME_FMT),
            "time_offset": self.time_offset,
            "user": self.s.get("username", ""),
            "perms": getattr(self, "perms", None),
        }


RES_SIZES = {"5M-N": "1296x1944", "4M-N": "1280x1440", "1080P": "1920x1080", "1080N": "960x1080",
             "720P": "1280x720", "960H": "960x576", "D1": "704x576", "CIF": "352x288"}
LABELS = {"FPS": "Kare hızı", "BitRate": "Bit hızı", "BitRateControl": "Bit hızı türü", "Quality": "Kalite",
          "GOP": "GOP", "resolution": "Çözünürlük", "Compression": "Sıkıştırma", "Enable": "Açık/kapalı",
          "AudioEnable": "Ses kaydı"}


def _num(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _list_items(text, root):
    """
    'users[0].Name=..' biçimindeki listeyi sözlüklere çevirir. 'X[n]' alanları listeye toplanır,
    '=' içermeyen satırlar önceki değerin devamıdır (log ayrıntıları birden çok satır olabilir).
    """
    import re
    items, last = {}, None
    pat = re.compile(rf"^{root}\[(\d+)\]\.([A-Za-z]+)(?:\[(\d+)\])?(?:\[\d+\])*=(.*)$")
    for line in text.splitlines():
        m = pat.match(line)
        if m:
            idx, key, sub, val = int(m.group(1)), m.group(2), m.group(3), m.group(4)
            d = items.setdefault(idx, {})
            if sub is not None:
                if key == "AccessSchedule":
                    continue
                d.setdefault(key, []).append(val)
                last = None
            else:
                d[key] = val
                last = (idx, key)
        elif last and line.strip():
            items[last[0]][last[1]] += "\n" + line.strip()
    return [items[i] for i in sorted(items)]


def _list_block(text, root):
    """'user.Name=..' biçimindeki tek kaydı sözlüğe çevirir."""
    converted = "\n".join(l.replace(f"{root}.", f"{root}[0].", 1) if l.startswith(f"{root}.") else l
                          for l in text.splitlines())
    items = _list_items(converted, root)
    return items[0] if items else {}


def _kv(text):
    out = {}
    for line in text.splitlines():
        k, sep, v = line.partition("=")
        if sep:
            out[k.strip()] = v.strip()
    return out

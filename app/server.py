"""Yerel web sunucusu: arayüzü sunar, cihaz API'sini ve görüntü akışlarını aracılar."""
import asyncio
import base64
import datetime as dt
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

from aiohttp import web, WSMsgType

import config
import dahua
import files
import logcache
import media
import updater

log = logging.getLogger("server")

WEB_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent)) / "web"
ISO = "%Y-%m-%dT%H:%M:%S"


class App:
    def __init__(self):
        self.settings = config.load()
        self.device = None
        self.status = "idle"        # idle | connecting | connected | error | setup | off
        self.tray = None
        self.updater = updater.Updater()
        self.message = ""
        self.connect_task = None
        self.library = files.Library(lambda: self.settings["save_dir"])
        self.logcache = logcache.LogCache(keep_days=lambda: self.settings.get("log_keep_days", 14))
        self.last_log_refresh = 0
        self.downloads = media.Downloader(lambda: self.device, on_done=self._job_done)
        self.clients = set()
        self.had_client = False
        self.records_cache = {}
        self.last_refresh = 0

    # ------------------------------------------------ bağlantı
    def start_connect(self):
        if self.connect_task and not self.connect_task.done():
            self.connect_task.cancel()
        self.connect_task = asyncio.ensure_future(self._connect())

    async def disconnect(self):
        """Kullanıcı bağlantıyı kapattı: cihaz oturumu kapanır, kendiliğinden yeniden bağlanılmaz."""
        if self.connect_task and not self.connect_task.done():
            self.connect_task.cancel()
        if self.device:
            d, self.device = self.device, None
            try:
                await asyncio.wait_for(d.close(), 3)
            except Exception:
                pass
        self.records_cache.clear()
        self.status, self.message = "off", "Bağlantı kapalı"

    def toggle_connection(self):
        if self.status in ("connected", "connecting"):
            asyncio.ensure_future(self.disconnect())
        else:
            self.start_connect()

    async def _connect(self):
        if self.device:
            await self.device.close()
            self.device = None
        s = self.settings
        if not (s.get("serial") or s.get("ip")) or not s.get("password"):
            self.status, self.message = "setup", "Başlamak için cihaz bilgilerini girin."
            return
        self.status, self.message = "connecting", "Bağlanılıyor…"
        self.records_cache.clear()

        def on_status(m):
            self.message = m

        dev = dahua.Device(s, on_status=on_status)
        try:
            await dev.connect()
        except asyncio.CancelledError:
            await dev.close()
            raise
        except dahua.DeviceError as e:
            await dev.close()
            self.status, self.message = "error", str(e)
            self.failed_at = time.time()
            return
        except Exception as e:
            log.exception("bağlantı hatası")
            await dev.close()
            self.status, self.message = "error", f"Bağlantı kurulamadı: {e}"
            return
        self.device = dev
        self.last_refresh = time.time()
        self.connected_at = time.time()
        self.status = "connected"
        self.message = {"lan": "Yerel ağ üzerinden bağlı", "p2p": "P2P ile bağlı",
                        "relay": "Bulut aktarımı ile bağlı"}.get(dev.mode, "Bağlı")

    async def background(self):
        """Kamera listesini tazeler, uygulama penceresi kapanınca programı kapatır."""
        idle_since = None
        asyncio.get_running_loop().call_later(15, lambda: asyncio.ensure_future(self.updater.check()))
        while True:
            await asyncio.sleep(5)
            # cihaz anlık meşgulse 30 sn'de bir kendiliğinden yeniden dene (şifre hatasında deneme)
            if (self.status == "error" and time.time() - getattr(self, "failed_at", 0) > 30
                    and "şifre" not in self.message.lower() and "sn numarası" not in self.message.lower()
                    and not (self.connect_task and not self.connect_task.done())):
                self.message = "Yeniden bağlanılıyor…"
                self.start_connect()
            if self.device and self.device.connected and time.time() - self.last_refresh > 300:
                self.last_refresh = time.time()
                try:
                    await self.device.load_channels()
                    await self.device.sync_time()
                except Exception as e:
                    log.info("kanal yenileme başarısız: %s", e)
            # cihaz logunu arka planda önbelleğe al (yalnızca yeni kısım; log penceresi hızlı açılsın)
            d = self.device
            if (d and d.connected and (getattr(d, "perms", None) or {}).get("users")
                    and time.time() - self.last_log_refresh > 600
                    and time.time() - getattr(self, "connected_at", 0) > 60):
                self.last_log_refresh = time.time()
                asyncio.ensure_future(self._refresh_logs(d))
            if self.had_client and not self.clients:
                idle_since = idle_since or time.time()
                busy = any(j["state"] in ("waiting", "running", "converting") for j in self.downloads.list())
                if time.time() - idle_since > 20 and not busy:
                    log.info("Pencere kapandı, çıkılıyor")
                    await self.shutdown()
                    os._exit(0)
            else:
                idle_since = None

    def display_aspect(self, ch=None, camera=None):
        """Kameranın gerçek görüntü oranı ("4:3"/"16:9"); 'orijinal' seçiliyse None."""
        if ch is None and camera and self.device:
            ch = next((c["id"] for c in self.device.channels if c["name"] == camera), None)
        if ch is None:
            return None
        p = self.camera_prefs(ch)
        a = p.get("aspect", "auto")
        if a in ("16:9", "4:3"):
            return a
        if a == "orig":
            return None
        return "4:3" if int(p.get("mp", 5)) == 5 else "16:9"

    def _job_done(self, job):
        meta = {"channel": job.ch, "camera": job.ch_name, "aspect": job.aspect,
                "start": job.start.strftime(ISO), "end": job.end.strftime(ISO),
                "source": "kesit" if job.source else "cihaz"}
        self.library.set_meta(job.path, **{k: v for k, v in meta.items() if v is not None})

    def camera_prefs(self, ch):
        p = {"mp": 5, "aspect": "auto"}
        p.update((self.settings.get("cameras") or {}).get(str(ch), {}))
        return p

    async def _refresh_logs(self, d):
        try:
            await self.logcache.refresh(d, hours=24)
        except Exception as e:
            log.info("log önbelleği tazelenemedi: %s", e)

    async def shutdown(self):
        """Cihazdaki açık akışları kapatır (yoksa cihaz bir süre boşa veri göndermeye devam eder)."""
        if self.tray:
            self.tray.remove()
        if self.device:
            try:
                await asyncio.wait_for(self.device.close(), 3)
                await asyncio.sleep(0.3)
            except Exception:
                pass

    def state(self):
        s = self.settings
        d = self.device
        channels = []
        if d:
            channels = [c for c in d.channels if s.get("show_all") or c.get("active", True)]
        return {
            "status": self.status,
            "message": self.message,
            "settings": {k: v for k, v in s.items() if k != "password"} | {"has_password": bool(s.get("password"))},
            "device": d.summary() if d else None,
            "channels": [c | {"prefs": self.camera_prefs(c["id"])} for c in channels],
            "all_channels": d.channels if d else [],
            "formats": [{"id": k, "label": v["label"]} for k, v in media.FORMATS.items()],
            "version": updater.VERSION,
            "update": (self.updater.info or {}).get("tag") if (self.updater.info or {}).get("newer") else None,
        }


def parse_time(text):
    return dt.datetime.strptime(text[:19], ISO)


def routes(app: App):
    r = web.RouteTableDef()

    @r.get("/")
    async def index(_):
        # dosya adreslerine sürüm eklenir: program güncellenince tarayıcı eski kodu önbellekten kullanmaz
        html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
        ver = str(int(max(p.stat().st_mtime for p in WEB_DIR.iterdir())))
        for ext in (".js", ".css"):
            html = html.replace(f'{ext}"', f'{ext}?v={ver}"')
        return web.Response(text=html, content_type="text/html", headers={"Cache-Control": "no-store"})

    @r.get("/api/state")
    async def state(_):
        return web.json_response(app.state())

    @r.post("/api/settings")
    async def save_settings(req):
        data = await req.json()
        s = dict(app.settings)
        for k in config.DEFAULTS:
            if k in data and k not in ("password", "cameras"):
                s[k] = data[k]
        if data.get("password"):
            s["password"] = data["password"]
        s["serial"] = (s.get("serial") or "").strip().upper()
        s["ip"] = (s.get("ip") or "").strip()
        reconnect = any(s.get(k) != app.settings.get(k) for k in ("serial", "username", "password", "mode", "ip"))
        app.settings = s
        config.save(s)
        if app.device:
            app.device.s.update(s)
        if reconnect or app.status in ("error", "setup", "idle"):
            app.start_connect()
        return web.json_response(app.state())

    @r.post("/api/quit")
    async def quit_(_):
        async def later():
            await app.shutdown()
            os._exit(0)
        asyncio.get_running_loop().call_later(0.2, lambda: asyncio.ensure_future(later()))
        return web.json_response({"ok": True})

    @r.get("/api/update")
    async def update_status(_):
        return web.json_response(app.updater.status())

    @r.post("/api/update/check")
    async def update_check(_):
        return web.json_response(await app.updater.check())

    @r.post("/api/update/install")
    async def update_install(_):
        try:
            await app.updater.install(app.shutdown)
        except RuntimeError as e:
            return web.json_response({"error": str(e)}, status=400)
        return web.json_response(app.updater.status())

    @r.post("/api/open-url")
    async def open_url(req):
        url = str((await req.json()).get("url", ""))
        if not url.startswith("https://github.com/"):
            raise web.HTTPBadRequest()
        os.startfile(url)
        return web.json_response({"ok": True})

    @r.post("/api/disconnect")
    async def disconnect(_):
        await app.disconnect()
        return web.json_response({"ok": True})

    @r.post("/api/reconnect")
    async def reconnect(_):
        app.start_connect()
        return web.json_response({"ok": True})

    @r.post("/api/refresh-channels")
    async def refresh_channels(_):
        if app.device:
            await app.device.load_channels()
            app.last_refresh = time.time()
        return web.json_response(app.state())

    @r.post("/api/rename")
    async def rename(req):
        data = await req.json()
        if not app.device:
            raise web.HTTPServiceUnavailable(text="Cihaz bağlı değil")
        try:
            await app.device.rename_channel(int(data["channel"]), str(data["name"])[:30])
        except dahua.DeviceError as e:
            return web.json_response({"error": str(e)}, status=400)
        return web.json_response(app.state())

    def need(perm):
        d = app.device
        if not d or not d.connected:
            raise web.HTTPServiceUnavailable(text="Cihaz bağlı değil")
        if not (getattr(d, "perms", None) or {}).get(perm):
            raise web.HTTPForbidden(text="Bu işlem için yetkiniz yok")
        return d

    async def guarded(fn):
        try:
            return web.json_response(await fn())
        except dahua.DeviceError as e:
            return web.json_response({"error": str(e)}, status=400)

    @r.get("/api/disk")
    async def disk(req):
        d = app.device
        if not d or not d.connected:
            return web.json_response({"error": "Cihaz bağlı değil"}, status=503)
        cached = getattr(app, "disk_cache", None)
        if cached and time.time() - cached[0] < (60 if req.query.get("fresh") else 600):          # cihazı yormamak için 10 dk önbellek
            return web.json_response(cached[1])
        try:
            data = await d.disk_info()
        except dahua.DeviceError as e:
            return web.json_response({"error": str(e)}, status=400)
        app.disk_cache = (time.time(), data)
        return web.json_response(data)

    @r.post("/api/my-password")
    async def my_password(req):
        d = app.device
        if not d or not d.connected:
            return web.json_response({"error": "Cihaz bağlı değil"}, status=503)
        data = await req.json()
        new = data.get("new") or ""
        if len(new) < 8:
            return web.json_response({"error": "Yeni şifre en az 8 karakter olmalı"}, status=400)
        try:
            await d.change_own_password(data.get("old") or "", new)
        except dahua.DeviceError as e:
            return web.json_response({"error": str(e)}, status=400)
        app.settings["password"] = new
        config.save(app.settings)
        app.start_connect()                                   # yeni şifreyle yeniden bağlan
        return web.json_response({"ok": True})

    def folder_size(p):
        total = n = 0
        if p.exists():
            for f in p.glob("*"):
                if f.is_file():
                    total += f.stat().st_size
                    n += 1
        return {"bytes": total, "files": n}

    @r.get("/api/appdata")
    async def appdata(_):
        return web.json_response({"logs": app.logcache.info(), "playcache": folder_size(files.PLAYCACHE),
                                  "thumbs": folder_size(files.THUMBS)})

    @r.post("/api/appdata/clear")
    async def appdata_clear(req):
        what = (await req.json()).get("what")
        if what == "logs":
            await app.logcache.clear()
            app.last_log_refresh = time.time()          # hemen yeniden doldurmasın
        elif what == "cache":
            for folder in (files.PLAYCACHE, files.THUMBS):
                for f in folder.glob("*") if folder.exists() else []:
                    try:
                        f.unlink()
                    except OSError:
                        pass
        elif what == "prune":
            await app.logcache.prune()
        else:
            raise web.HTTPBadRequest(text="Geçersiz istek")
        return await appdata(req)

    @r.get("/api/users")
    async def users_list(_):
        d = need("users")
        return await guarded(d.users)

    @r.post("/api/users")
    async def users_add(req):
        d = need("users")
        u = await req.json()
        if not u.get("name") or not u.get("password"):
            return web.json_response({"error": "Kullanıcı adı ve şifre gerekli"}, status=400)

        async def go():
            await d.add_user(u)
            return await d.users()
        return await guarded(go)

    @r.put("/api/users/{name}")
    async def users_edit(req):
        d = need("users")
        u = await req.json()

        async def go():
            await d.modify_user(req.match_info["name"], u)
            return await d.users()
        return await guarded(go)

    @r.delete("/api/users/{name}")
    async def users_del(req):
        d = need("users")

        async def go():
            await d.delete_user(req.match_info["name"])
            return await d.users()
        return await guarded(go)

    @r.get("/api/logs")
    async def logs(req):
        d = need("users")
        user = req.query.get("user")
        if req.query.get("start") and req.query.get("end"):      # tek zaman dilimi (arayüz dilim dilim ister)
            start, end = parse_time(req.query["start"]), parse_time(req.query["end"])
        else:
            days = max(1, min(30, int(req.query.get("days", 1))))
            end = d.now()
            start = end - dt.timedelta(days=days)

        async def go():
            # Cihaz olayları (hareket, disk, yeniden başlatma) bazen bir kullanıcı adıyla loglanıyor:
            # türüne göre "sistem" sayılır ve kullanıcının "yaptıkları" listesine karışmaz.
            sys_words = ("Hareket", "Alarm", "Algılama", "Disk", "S.M.A.R.T", "Kapatma", "Başlat", "Video Kaybı",
                         "Kamera", "Ağ", "IP Çakışması")
            is_sys = lambda i: i["user"] in ("", "default") or any(w in i["type"] for w in sys_words)
            keep = None
            if user is not None:
                keep = is_sys if user == "" else (lambda i: i["user"] == user and not is_sys(i))
            items = await app.logcache.get(d, start, end, keep=keep)
            return {"start": start.strftime(ISO), "end": end.strftime(ISO), "items": items}
        return await guarded(go)

    @r.get("/api/camera/{ch}")
    async def camera_get(req):
        ch = int(req.match_info["ch"])
        if app.device and app.device.connected and not (getattr(app.device, "perms", None) or {}).get("config"):
            return web.json_response({"error": "Bu işlem için ayar yetkiniz yok"}, status=403)
        if not app.device or not app.device.connected:
            return web.json_response({"error": "Cihaz bağlı değil"}, status=503)
        try:
            cfg = await app.device.camera_config(ch)
        except dahua.DeviceError as e:
            return web.json_response({"error": str(e)}, status=400)
        cfg["prefs"] = app.camera_prefs(ch)
        try:
            cfg["motion"] = await app.device.motion_config(ch)
        except dahua.DeviceError:
            cfg["motion"] = None
        return web.json_response(cfg)

    @r.post("/api/camera/{ch}")
    async def camera_set(req):
        ch = int(req.match_info["ch"])
        if app.device and app.device.connected and not (getattr(app.device, "perms", None) or {}).get("config"):
            return web.json_response({"error": "Bu işlem için ayar yetkiniz yok"}, status=403)
        data = await req.json()
        if "prefs" in data:
            cams = dict(app.settings.get("cameras") or {})
            p = app.camera_prefs(ch)
            p.update({k: v for k, v in data["prefs"].items() if k in ("mp", "aspect")})
            cams[str(ch)] = p
            app.settings["cameras"] = cams
            config.save(app.settings)
        result = {"prefs": app.camera_prefs(ch), "warnings": []}
        if data.get("motion"):
            if not app.device or not app.device.connected:
                return web.json_response({"error": "Cihaz bağlı değil"}, status=503)
            try:
                result["motion"], w = await app.device.set_motion_config(ch, data["motion"])
                result["warnings"] += w
            except dahua.DeviceError as e:
                return web.json_response({"error": str(e)}, status=400)
        if data.get("device"):
            if not app.device or not app.device.connected:
                return web.json_response({"error": "Cihaz bağlı değil"}, status=503)
            try:
                cfg, warnings = await app.device.set_camera_config(ch, data["device"])
            except dahua.DeviceError as e:
                return web.json_response({"error": str(e)}, status=400)
            result.update(cfg)
            result["warnings"] += warnings
            result["prefs"] = app.camera_prefs(ch)
        return web.json_response(result)

    @r.post("/api/cameras/apply")
    async def cameras_apply(req):
        if app.device and app.device.connected and not (getattr(app.device, "perms", None) or {}).get("config"):
            return web.json_response({"error": "Bu işlem için ayar yetkiniz yok"}, status=403)
        data = await req.json()
        if not app.device or not app.device.connected:
            return web.json_response({"error": "Cihaz bağlı değil"}, status=503)
        chans = [int(c) for c in data.get("channels", [])]
        if data.get("prefs"):
            cams = dict(app.settings.get("cameras") or {})
            for ch in chans:
                p = app.camera_prefs(ch)
                p.update({k: v for k, v in data["prefs"].items() if k in ("mp", "aspect")})
                cams[str(ch)] = p
            app.settings["cameras"] = cams
            config.save(app.settings)
        app.apply_status = "Başlıyor…"
        try:
            results, notes = await app.device.apply_many(chans, data.get("device") or {},
                                                         say=lambda m: setattr(app, "apply_status", m))
        except dahua.DeviceError as e:
            return web.json_response({"error": str(e)}, status=400)
        finally:
            app.apply_status = ""
        return web.json_response({"results": {str(k): v for k, v in results.items()}, "notes": notes})

    @r.post("/api/encode-enhance")
    async def encode_enhance(req):
        if app.device and app.device.connected and not (getattr(app.device, "perms", None) or {}).get("config"):
            return web.json_response({"error": "Bu işlem için ayar yetkiniz yok"}, status=403)
        data = await req.json()
        if not app.device or not app.device.connected:
            return web.json_response({"error": "Cihaz bağlı değil"}, status=503)
        try:
            await app.device.set_encode_enhance(bool(data.get("enable")), reboot=True)
        except dahua.DeviceError as e:
            return web.json_response({"error": str(e)}, status=400)
        # cihaz yeniden başlıyor: bağlantıyı bir süre sonra yeniden kur
        app.status, app.message = "connecting", "Kayıt cihazı yeniden başlıyor (1–2 dakika)…"
        async def later():
            await asyncio.sleep(75)
            app.start_connect()
        asyncio.ensure_future(later())
        return web.json_response({"ok": True})

    @r.get("/api/cameras/apply-status")
    async def cameras_apply_status(_):
        return web.json_response({"status": getattr(app, "apply_status", "")})

    @r.get("/api/records")
    async def records(req):
        d = app.device
        if not d or not d.connected:
            return web.json_response({"error": "Cihaz bağlı değil"}, status=503)
        day = dt.datetime.strptime(req.query["date"], "%Y-%m-%d")
        chans = [int(x) for x in req.query.get("ch", "").split(",") if x]
        end = day + dt.timedelta(days=1) - dt.timedelta(seconds=1)
        is_past = end < d.now() - dt.timedelta(minutes=5)
        sem = asyncio.Semaphore(2)
        active = {c["id"] for c in d.channels if c.get("active", True)}
        failed = []

        async def one(ch):
            key = (ch, day.date())
            if is_past and key in app.records_cache:
                return ch, app.records_cache[key]
            rec = []
            # Cihaz meşgulken (ör. açılışta) arama boş ya da hatalı dönebilir: kamerası olan kanalda
            # boş sonuç güvenilmez sayılıp birkaç kez yeniden denenir, başarısızlık önbelleğe yazılmaz.
            for attempt in range(3):
                async with sem:
                    try:
                        rec = await d.find_records(ch, day, end)
                    except dahua.DeviceError:
                        rec = []
                if rec or ch not in active or day.date() > d.now().date():
                    break
                await asyncio.sleep(1.5 * (attempt + 1))
            out = [[a.strftime(ISO), b.strftime(ISO), k] for a, b, k in rec]
            if not rec and ch in active:
                failed.append(ch)
            elif is_past:
                app.records_cache[key] = out
            return ch, out

        res = await asyncio.gather(*[one(c) for c in chans])
        return web.json_response({"now": d.now().strftime(ISO), "records": {str(c): v for c, v in res},
                                  "failed": failed})

    @r.get("/ws/app")
    async def ws_app(req):
        """Pencere açık olduğu sürece bağlı kalır (pencere kapanınca program kapanır)."""
        ws = web.WebSocketResponse(heartbeat=10)
        await ws.prepare(req)
        app.clients.add(ws)
        app.had_client = True
        try:
            async for _ in ws:
                pass
        finally:
            app.clients.discard(ws)
        return ws

    @r.get("/ws/stream")
    async def ws_stream(req):
        ws = web.WebSocketResponse(heartbeat=15, max_msg_size=0)
        await ws.prepare(req)
        d = app.device
        q = req.query
        if not d or not d.connected:
            await ws.send_json({"type": "error", "message": "Cihaz bağlı değil"})
            await ws.close()
            return ws
        ch = int(q.get("ch", 1))
        sub = int(q.get("sub", 1))
        speed = float(q.get("speed", 1) or 1)
        start = parse_time(q["start"]) if q.get("start") else None
        transcode = q.get("codec") == "h264"
        if start is not None:
            sub = 0   # cihaz yalnızca ana akışı kaydediyor
        url = d.rtsp_url(ch, sub, start, fast=bool(start is not None and speed > 1))
        stream = media.LiveStream(url, transcode=transcode, scale=(720 if transcode and q.get("small") == "1" else None))
        await stream.start()

        async def pump():
            gen = stream.chunks()
            try:
                init = await asyncio.wait_for(gen.__anext__(), 25)
            except (asyncio.TimeoutError, asyncio.IncompleteReadError, StopAsyncIteration):
                err = await stream.error_text()
                msg = "Görüntü alınamadı"
                if "404" in err:
                    msg = "Bu zamanda kayıt yok" if start else "Bu kanalda görüntü yok"
                elif err:
                    log.info("akış hatası ch%s: %s", ch, err[-300:])
                await ws.send_json({"type": "error", "message": msg})
                return
            await ws.send_json({"type": "init", "codec": media.codec_string(init) or "", "start": q.get("start", "")})
            await ws.send_bytes(init)
            try:
                async for chunk in gen:
                    await ws.send_bytes(chunk)
            except (asyncio.IncompleteReadError, ConnectionResetError):
                pass
            if not ws.closed:
                await ws.send_json({"type": "end"})

        pump_task = asyncio.ensure_future(pump())

        async def reader():
            async for msg in ws:
                if msg.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
                    break

        read_task = asyncio.ensure_future(reader())
        try:
            await asyncio.wait([pump_task, read_task], return_when=asyncio.FIRST_COMPLETED)
        finally:
            stream.stop()
            pump_task.cancel()
            read_task.cancel()
            if not ws.closed:
                await ws.close()
        return ws

    @r.get("/api/downloads")
    async def list_dl(_):
        return web.json_response(app.downloads.list())

    def day_windows(start, end, hours):
        """[start,end] aralığını günlük saat penceresiyle (ör. 08:00–18:00) keser."""
        if not hours or not hours.get("from") or not hours.get("to"):
            return [(start, end)]
        h1 = dt.datetime.strptime(hours["from"], "%H:%M").time()
        h2 = dt.datetime.strptime(hours["to"], "%H:%M").time()
        out = []
        day = dt.datetime.combine(start.date(), dt.time()) - dt.timedelta(days=1)
        while day <= end:
            a = dt.datetime.combine(day.date(), h1)
            b = dt.datetime.combine(day.date(), h2)
            if b <= a:
                b += dt.timedelta(days=1)          # gece yarısını geçen pencere (ör. 20:00–06:00)
            a, b = max(a, start), min(b, end)
            if b > a:
                out.append((a, b))
            day += dt.timedelta(days=1)
        return out

    def chunks(a, b, size=dt.timedelta(hours=1)):
        while a < b:
            n = min(b, a + size)
            yield a, n
            a = n

    EVENT_NAMES = {"VideoMotion": "Hareket", "SmartMotionHuman": "İnsan", "SmartMotionVehicle": "Araç",
                   "AlarmLocal": "Alarm", "VideoLoss": "Görüntü kaybı", "VideoBlind": "Kamera kapatıldı"}

    async def export_events(d, chans, start, end, hours, fmt="csv"):
        rows = []
        for ch in chans:
            name = next((c["name"] for c in d.channels if c["id"] == ch), f"Kamera {ch}")
            for a, b in day_windows(start, end, hours):
                for f in await d.rec_files(ch, a, b, events_only=True):
                    if f["kind"] != "event":
                        continue
                    rows.append({"kamera": name, "kanal": ch, "baslangic": f["start"].replace("T", " "),
                                 "bitis": f["end"].replace("T", " "),
                                 "sure_sn": int((parse_time(f["end"]) - parse_time(f["start"])).total_seconds()),
                                 "olay": ", ".join(EVENT_NAMES.get(e, e) for e in f["events"]) or "Olay"})
        rows.sort(key=lambda r: (r["baslangic"], r["kanal"]))
        folder = Path(app.settings["save_dir"]) / "Olaylar"
        folder.mkdir(parents=True, exist_ok=True)
        cams = "_".join(media._safe(next((c["name"] for c in d.channels if c["id"] == ch), str(ch))) for ch in chans)
        if len(chans) > 2:
            cams = f"{len(chans)}_kamera"
        stem = f"Olaylar_{cams}_{start:%Y-%m-%d_%H-%M}_{end:%Y-%m-%d_%H-%M}"
        paths = []
        if fmt in ("csv", "both"):
            p = folder / f"{stem}.csv"
            lines = ["Kamera;Kanal;Başlangıç;Bitiş;Süre (sn);Olay"]
            lines += [f"{r['kamera']};{r['kanal']};{r['baslangic']};{r['bitis']};{r['sure_sn']};{r['olay']}" for r in rows]
            p.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")      # Excel Türkçe karakterleri doğru açsın
            paths.append(p)
        if fmt in ("json", "both"):
            p = folder / f"{stem}.json"
            p.write_text(json.dumps({"cihaz": d.info.get("model", ""), "aralik": [start.strftime(ISO), end.strftime(ISO)],
                                     "saat_araligi": hours, "olaylar": rows}, ensure_ascii=False, indent=1), encoding="utf-8")
            paths.append(p)
        return rows, paths

    @r.get("/api/recfiles")
    async def recfiles(req):
        d = app.device
        if not d or not d.connected:
            return web.json_response({"error": "Cihaz bağlı değil"}, status=503)
        a = dt.datetime.strptime(req.query["from"], "%Y-%m-%d")
        b = dt.datetime.strptime(req.query["to"], "%Y-%m-%d") + dt.timedelta(days=1) - dt.timedelta(seconds=1)
        b = min(b, d.now())
        chans = [int(x) for x in req.query.get("ch", "").split(",") if x] or [c["id"] for c in d.channels]
        perms = getattr(d, "perms", None) or {}
        if perms.get("replay"):
            chans = [c for c in chans if c in perms["replay"]]
        cache = app.__dict__.setdefault("recfile_cache", {})
        out = []
        try:
            for ch in chans:
                day = dt.datetime.combine(a.date(), dt.time())
                while day <= b:
                    key = (ch, day.date())
                    past = day.date() < d.now().date()
                    if past and key in cache:
                        out += cache[key]
                    else:
                        files = await d.rec_files(ch, day, min(b, day + dt.timedelta(days=1) - dt.timedelta(seconds=1)))
                        if past:
                            cache[key] = files
                        out += files
                    day += dt.timedelta(days=1)
        except dahua.DeviceError as e:
            return web.json_response({"error": str(e)}, status=400)
        names = {c["id"]: c["name"] for c in d.channels}
        for f in out:
            f["camera"] = names.get(f["channel"], f"Kamera {f['channel']}")
            f["event_names"] = [EVENT_NAMES.get(e, e) for e in f["events"]]
        return web.json_response({"files": out, "now": d.now().strftime(ISO)})

    @r.post("/api/events/export")
    async def events_export(req):
        d = app.device
        if not d or not d.connected:
            return web.json_response({"error": "Cihaz bağlı değil"}, status=503)
        data = await req.json()
        try:
            rows, paths = await export_events(d, [int(c) for c in data["channels"]], parse_time(data["start"]),
                                              parse_time(data["end"]), data.get("hours"), data.get("format", "csv"))
        except dahua.DeviceError as e:
            return web.json_response({"error": str(e)}, status=400)
        return web.json_response({"count": len(rows), "files": [str(p) for p in paths]})

    @r.post("/api/downloads")
    async def add_dl(req):
        data = await req.json()
        d = app.device
        if not d or not d.connected:
            return web.json_response({"error": "Cihaz bağlı değil"}, status=503)
        fmt = data.get("format") or app.settings.get("format", "mp4")
        if fmt not in media.FORMATS:
            fmt = "mp4"
        # tek aralık (Kırp ve İndir) ya da dosya gezgininden birden çok parça
        items = data.get("items") or [{"channel": c, "start": data["start"], "end": data["end"]}
                                      for c in (data.get("channels") or [data.get("channel")])]
        perms = getattr(d, "perms", None) or {}
        segs = []
        for it in items:
            ch = int(it["channel"])
            if perms.get("replay") and ch not in perms["replay"]:
                return web.json_response({"error": f"Kanal {ch} kayıtlarını izleme yetkiniz yok"}, status=403)
            start, end = parse_time(it["start"]), parse_time(it["end"])
            if end <= start:
                return web.json_response({"error": "Bitiş zamanı başlangıçtan sonra olmalı"}, status=400)
            for a, b in day_windows(start, end, data.get("hours")):
                segs += [(ch, x, y) for x, y in chunks(a, b)]
        if not segs:
            return web.json_response({"error": "Seçilen saat aralığında indirilecek kayıt yok"}, status=400)
        if len(segs) > 400:
            return web.json_response({"error": f"Çok fazla parça ({len(segs)}). Daha kısa bir aralık seçin."}, status=400)
        jobs = []
        for ch, a, b in segs:
            name = next((c["name"] for c in d.channels if c["id"] == ch), f"Kamera {ch}")
            jobs.append(app.downloads.add(ch=ch, ch_name=name, start=a, end=b, fmt=fmt, quality="main",
                                          folder=app.settings["save_dir"], aspect=app.display_aspect(ch)).to_dict())
        if not (data.get("items") or data.get("with_events")):
            return web.json_response(jobs)          # eski çağrılar liste bekliyor
        result = {"jobs": jobs}
        if data.get("with_events"):
            chans = sorted({int(it["channel"]) for it in items})
            a = min(parse_time(it["start"]) for it in items)
            b = max(parse_time(it["end"]) for it in items)
            try:
                rows, paths = await export_events(d, chans, a, b, data.get("hours"), data.get("events_format", "csv"))
                result["events"] = {"count": len(rows), "files": [str(p) for p in paths]}
            except dahua.DeviceError as e:
                result["events"] = {"error": str(e)}
        return web.json_response(result)

    @r.delete("/api/downloads/{id}")
    async def del_dl(req):
        jid = int(req.match_info["id"])
        job = app.downloads.jobs.get(jid)
        if job and job.state in ("waiting", "running", "converting"):
            app.downloads.cancel(jid)
        else:
            app.downloads.remove(jid)
        return web.json_response(app.downloads.list())

    # ------------------------------------------------ dosya kitaplığı
    def lib_item_aspect(item):
        a = item.get("aspect") or app.display_aspect(item.get("channel"), item.get("camera"))
        if not a:
            return None
        w, h = (float(x) for x in a.split(":"))
        return w / h

    @r.get("/api/files")
    async def files_list(_):
        items = await asyncio.to_thread(app.library.list)
        for it in items:
            if it["kind"] == "video" and not it.get("aspect"):
                it["aspect"] = app.display_aspect(it.get("channel"), it.get("camera"))
        return web.json_response({"root": app.settings["save_dir"], "items": items, "jobs": app.downloads.list()})

    def lib_path(req):
        try:
            return req.query["path"], app.library.resolve(req.query["path"])
        except (KeyError, PermissionError, ValueError):
            raise web.HTTPBadRequest(text="Geçersiz dosya")

    @r.get("/api/files/thumb")
    async def files_thumb(req):
        rel, p = lib_path(req)
        if not p.exists():
            raise web.HTTPNotFound()
        item = {"path": rel, **app.library.meta.get(rel, {})}
        if "camera" not in item:
            m = files._NAME_SNAP.match(p.stem)
            item["camera"] = m.group("cam") if m else None
        aspect = lib_item_aspect(item) if p.suffix.lower().lstrip(".") in files.VIDEO else None
        t = await app.library.thumb(rel, aspect)
        if not t:
            raise web.HTTPNotFound()
        return web.FileResponse(t, headers={"Cache-Control": "max-age=86400"})

    @r.get("/api/files/media")
    async def files_media(req):
        rel, p = lib_path(req)
        if not p.exists():
            raise web.HTTPNotFound()
        f = await app.library.playable(rel)
        if not f:
            return web.json_response({"error": "Bu dosya oynatılamadı"}, status=415)
        return web.FileResponse(f)

    @r.get("/api/files/info")
    async def files_info(req):
        rel, p = lib_path(req)
        info = await app.library.probe(rel)
        return web.json_response(info)

    @r.post("/api/files/note")
    async def files_note(req):
        data = await req.json()
        try:
            app.library.resolve(data["path"])
        except (KeyError, PermissionError, ValueError):
            raise web.HTTPBadRequest(text="Geçersiz dosya")
        app.library.set_note(data["path"], data.get("text", ""))
        return web.json_response({"ok": True})

    @r.delete("/api/files")
    async def files_delete(req):
        rel, p = lib_path(req)
        try:
            await asyncio.to_thread(app.library.recycle, rel)
        except OSError as e:
            return web.json_response({"error": str(e)}, status=400)
        return web.json_response({"ok": True})

    @r.post("/api/files/export")
    async def files_export(req):
        data = await req.json()
        try:
            rel = data["path"]
            src = app.library.resolve(rel)
            a, b = float(data["start"]), float(data["end"])
        except (KeyError, PermissionError, ValueError):
            raise web.HTTPBadRequest(text="Geçersiz istek")
        if b - a < 0.5:
            return web.json_response({"error": "Kesit en az yarım saniye olmalı"}, status=400)
        fmt = data.get("format") or "mp4"
        if fmt not in media.FORMATS:
            fmt = "mp4"
        meta = app.library.meta.get(rel, {})
        m = files._NAME_RANGE.match(src.stem) or files._NAME_SNAP.match(src.stem)
        camera = meta.get("camera") or (m.group("cam") if m else src.stem[:20])
        rec0 = None
        if meta.get("start"):
            rec0 = parse_time(meta["start"])
        elif m:
            rec0 = dt.datetime.strptime(f"{m.group('d')} {m.group('a')}", "%Y-%m-%d %H-%M-%S")
        base = rec0 or dt.datetime.now().replace(microsecond=0)
        start = base + dt.timedelta(seconds=a)
        end = base + dt.timedelta(seconds=b)
        ch = meta.get("channel") or 1
        aspect = meta.get("aspect") or app.display_aspect(meta.get("channel"), camera)
        job = app.downloads.add(ch=ch, ch_name=camera, start=start, end=end, fmt=fmt, quality="main",
                                folder=str(src.parent), aspect=aspect, source=str(src), offset=a)
        return web.json_response(job.to_dict())

    @r.post("/api/grab")
    async def grab(req):
        """Çoklu görünümde kare: küçük canlı akış yerine cihazın ana akışından tam çözünürlüklü kare alır."""
        d = app.device
        if not d or not d.connected:
            return web.json_response({"error": "Cihaz bağlı değil"}, status=503)
        data = await req.json()
        ch = int(data["channel"])
        when = parse_time(data["time"]) if data.get("time") else None
        url = d.rtsp_url(ch, 0, when) if when else d.rtsp_url(ch, 0)
        ext = app.settings.get("snap_format", "jpg")
        name = media._safe(next((c["name"] for c in d.channels if c["id"] == ch), f"Kamera {ch}"))
        stamp = (when or d.now()).strftime("%Y-%m-%d_%H-%M-%S")
        folder = Path(app.settings["save_dir"]) / "Fotoğraflar"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{name}_{stamp}.{ext}"
        n = 2
        while path.exists():
            path = folder / f"{name}_{stamp} ({n}).{ext}"
            n += 1
        aspect = app.display_aspect(ch)
        vf = []
        if aspect:
            w, h = (int(x) for x in aspect.split(":"))
            vf = ["-vf", f"scale=trunc(ih*{w}/{h}/2)*2:ih:flags=lanczos"]   # dikey çözünürlük korunur
        quality = {"jpg": ["-q:v", "2"], "webp": ["-quality", "92"], "png": []}.get(ext, [])
        args = [media.FFMPEG, "-v", "error", "-y", "-rtsp_transport", "tcp", "-timeout", "15000000", "-i", url,
                "-an", "-frames:v", "1", *vf, *quality, str(path)]
        p = await asyncio.create_subprocess_exec(*args, stderr=asyncio.subprocess.PIPE, creationflags=media.NO_WINDOW)
        try:
            _, err = await asyncio.wait_for(p.communicate(), 30)
        except asyncio.TimeoutError:
            p.kill()
            return web.json_response({"error": "Kare alınamadı (zaman aşımı)"}, status=504)
        if not path.exists():
            return web.json_response({"error": "Kare alınamadı"}, status=400)
        return web.json_response({"file": str(path), "filename": path.name})

    @r.post("/api/snapshot")
    async def snapshot(req):
        data = await req.json()
        img = data["image"]
        raw = base64.b64decode(img.split(",", 1)[1])
        ext = {"data:image/png": "png", "data:image/webp": "webp"}.get(img.split(";", 1)[0], "jpg")
        name = media._safe(data.get("name", "Kamera"))
        when = (data.get("time") or dt.datetime.now().strftime(ISO)).replace(":", "-").replace("T", "_")
        folder = Path(app.settings["save_dir"]) / "Fotoğraflar"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{name}_{when}.{ext}"
        n = 2
        while path.exists():
            path = folder / f"{name}_{when} ({n}).{ext}"
            n += 1
        path.write_bytes(raw)
        return web.json_response({"file": str(path), "filename": path.name})

    @r.post("/api/open")
    async def open_path(req):
        data = await req.json()
        p = Path(data.get("path") or app.settings["save_dir"])
        if data.get("play") and p.is_file():
            os.startfile(str(p))          # varsayılan video oynatıcısıyla aç
        elif data.get("select") and p.exists():
            subprocess.Popen(["explorer", "/select,", str(p)])
        else:
            target = p if p.is_dir() else p.parent
            target.mkdir(parents=True, exist_ok=True)
            os.startfile(str(target))
        return web.json_response({"ok": True})

    @r.post("/api/choose-folder")
    async def choose_folder(_):
        def pick():
            ps = ("Add-Type -AssemblyName System.Windows.Forms;"
                  "$d=New-Object System.Windows.Forms.FolderBrowserDialog;"
                  "$d.Description='Kayıtların kaydedileceği klasörü seçin';"
                  f"$d.SelectedPath='{app.settings['save_dir']}';"
                  "$f=New-Object System.Windows.Forms.Form -Property @{TopMost=$true};"
                  "if($d.ShowDialog($f) -eq 'OK'){[Console]::OutputEncoding=[Text.Encoding]::UTF8;$d.SelectedPath}")
            r = subprocess.run(["powershell", "-NoProfile", "-STA", "-Command", ps],
                               capture_output=True, text=True, encoding="utf-8", creationflags=media.NO_WINDOW)
            return r.stdout.strip()
        path = await asyncio.to_thread(pick)
        return web.json_response({"path": path})

    r.static("/static", WEB_DIR)
    return r


@web.middleware
async def _no_stale_ui(request, handler):
    """Arayüz dosyaları her açılışta doğrulanır; program güncellenince pencere eski kodu kullanmaz."""
    resp = await handler(request)
    if request.path.startswith("/static/") and isinstance(resp, web.StreamResponse):
        resp.headers["Cache-Control"] = "no-cache"
    return resp


async def create(app: App):
    wa = web.Application(client_max_size=50 * 1024 * 1024, middlewares=[_no_stale_ui])
    wa.add_routes(routes(app))
    return wa

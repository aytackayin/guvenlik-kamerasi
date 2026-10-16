"""
Cihaz logu için yerel önbellek.

Cihaz logu türe göre süzülemiyor, yavaş okunuyor ve her okuma isteği loga yeni "giriş" satırları
ekliyor. Bu yüzden okunan zaman aralıkları bilgisayarda saklanır, cihazdan yalnızca daha önce
okunmamış aralıklar istenir. Son birkaç dakika her zaman yeniden okunur (cihaz yazmaya devam ettiği için).
"""
import asyncio
import datetime as dt
import json
import logging
import os

import config

log = logging.getLogger("logcache")

FILE = config.APP_DIR / "log_onbellegi.json"
FMT = "%Y-%m-%d %H:%M:%S"
FRESH = dt.timedelta(minutes=2)          # bu kadar yeni kısım önbelleğe "tamamlandı" diye yazılmaz


def _p(t):
    return dt.datetime.strptime(t, FMT)


class LogCache:
    def __init__(self, keep_days=lambda: 14):
        self.keep_days = keep_days          # ayarlardan okunur
        self.entries = {}       # anahtar -> kayıt
        self.covered = []       # [(başlangıç, bitiş)] okunmuş aralıklar (datetime)
        self.device_key = None
        self.lock = asyncio.Lock()
        self._load()

    # ------------------------------------------------ dosya
    def _load(self):
        try:
            data = json.loads(FILE.read_text(encoding="utf-8"))
            self.device_key = data.get("device")
            self.covered = [(_p(a), _p(b)) for a, b in data.get("covered", [])]
            for e in data.get("entries", []):
                self.entries[self._key(e)] = e
        except Exception:
            pass

    def _save(self):
        cutoff = (dt.datetime.now() - dt.timedelta(days=max(1, int(self.keep_days() or 14)))).strftime(FMT)
        self.entries = {k: e for k, e in self.entries.items() if e["time"] >= cutoff}
        self.covered = [(a, b) for a, b in self.covered if b.strftime(FMT) >= cutoff]
        tmp = FILE.with_suffix(".tmp")
        config.APP_DIR.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps({"device": self.device_key,
                                   "covered": [[a.strftime(FMT), b.strftime(FMT)] for a, b in self.covered],
                                   "entries": sorted(self.entries.values(), key=lambda e: e["time"])},
                                  ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, FILE)

    @staticmethod
    def _key(e):
        return f"{e['time']}|{e['type']}|{e['user']}|{e['detail']}"

    # ------------------------------------------------ aralık hesabı
    def _merge(self):
        iv = sorted(self.covered)
        out = []
        for a, b in iv:
            if out and a <= out[-1][1] + dt.timedelta(seconds=1):
                out[-1] = (out[-1][0], max(out[-1][1], b))
            else:
                out.append((a, b))
        self.covered = out

    def _gaps(self, a, b):
        gaps, cur = [], a
        for x, y in sorted(self.covered):
            if y <= cur or x >= b:
                continue
            if x > cur:
                gaps.append((cur, x))
            cur = max(cur, y)
        if cur < b:
            gaps.append((cur, b))
        return [(x, y) for x, y in gaps if (y - x).total_seconds() >= 1]

    # ------------------------------------------------ okuma
    async def get(self, device, start, end, keep=None):
        """[start, end] aralığındaki log kayıtlarını önbellekten, eksikleri cihazdan tamamlayarak verir."""
        async with self.lock:
            key = device.s.get("serial") or device.s.get("ip")
            if key != self.device_key:          # başka cihaz: önbelleği sıfırla
                self.entries, self.covered, self.device_key = {}, [], key
            now = device.now()
            for a, b in self._gaps(start, min(end, now)):
                items = await device.logs(a, b)
                for e in items:
                    self.entries[self._key(e)] = e
                stable_end = min(b, now - FRESH)          # en yeni dakikalar sonra yeniden okunur
                if stable_end > a:
                    self.covered.append((a, stable_end))
            self._merge()
            try:
                self._save()
            except OSError as e:
                log.info("log önbelleği yazılamadı: %s", e)
        s, e = start.strftime(FMT), end.strftime(FMT)
        out = [x for x in self.entries.values() if s <= x["time"] <= e]
        if keep:
            out = [x for x in out if keep(x)]
        out.sort(key=lambda x: x["time"])
        return out

    async def refresh(self, device, hours=24):
        """Arka planda: son N saatin yalnızca okunmamış kısmını çeker (pencere açılınca hazır olsun)."""
        now = device.now()
        await self.get(device, now - dt.timedelta(hours=hours), now)

    # ------------------------------------------------ bakım
    def info(self):
        times = [e["time"] for e in self.entries.values()]
        return {"entries": len(times), "oldest": min(times) if times else None, "newest": max(times) if times else None,
                "bytes": FILE.stat().st_size if FILE.exists() else 0, "keep_days": int(self.keep_days() or 14)}

    async def prune(self):
        async with self.lock:
            self._save()

    async def clear(self):
        """Programın sakladığı log arşivini siler (cihazdaki loga dokunmaz)."""
        async with self.lock:
            self.entries, self.covered = {}, []
            try:
                FILE.unlink(missing_ok=True)
            except OSError:
                pass

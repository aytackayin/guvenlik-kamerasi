"""
Dahua P2P (Easy4IP) bağlantısı ve PTCP tüneli.

Cihaz, seri numarası (SN) ile Dahua bulutu üzerinden bulunur; ardından UDP üzerinde
PTCP (TCP benzeri) bir oturum kurulur. Bu oturum içinde cihazın istediğimiz TCP
portlarına (RTSP 554, HTTP 80 ...) birden fazla eşzamanlı bağlantı açılabilir.
Her bağlantı yerel bir TCP portuna köprülenir, böylece ffmpeg ve HTTP istemcileri
cihaz yerel ağdaymış gibi çalışır.

Protokol ayrıntıları herkese açık tersine mühendislik notlarına dayanır
(github.com/khoanguyen-3fc/dh-p2p). Sıralama, yeniden gönderim ve çoklu bağlantı
desteği bu uygulamaya özgüdür.
"""
import asyncio
import base64
import datetime
import hashlib
import hmac
import json
import logging
import os
import random
import re
import socket
import struct
import time

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

log = logging.getLogger("p2p")

MAIN_SERVER = ("www.easy4ipcloud.com", 8800)
CLOUD_USER = "cba1b29e32cb17aa46b8ff9e73c7f40b"
CLOUD_KEY = "996103384cdf19179e19243e959bbf8b"
ADDR_IV = b"2z52*lk9o6HRyJrf"
INFO_KEY = b"kRjmsUB&ezmdGLL67H#$ojw@XflcaIaf"
INFO_IV = b"MydvJw*Iw1w&i^kk"

SYNC_BODY = b"\x00\x03\x01\x00"
MAX_CHUNK = 1300  # UDP üzerinde parçalanmayı önlemek için veri boyutu


class P2PError(Exception):
    pass


# ---------------------------------------------------------------- yardımcılar

def _aes_ofb(key, iv, data):
    c = Cipher(algorithms.AES(key), modes.OFB(iv)).encryptor()
    return c.update(data) + c.finalize()


def _decrypt_info(info):
    if not info:
        return {}
    try:
        return json.loads(_aes_ofb(INFO_KEY, INFO_IV, base64.b64decode(info)))
    except Exception:
        return {}


def _pass_key(username, password, randsalt):
    return hashlib.md5(f"{username}:Login to {randsalt}:{password}".encode()).hexdigest().upper().encode()


def _derive(key, nonce):
    return hashlib.pbkdf2_hmac("sha256", key, str(nonce).encode(), 20000, 32)


def _enc_addr(key, nonce, text):
    return base64.b64encode(_aes_ofb(_derive(key, nonce), ADDR_IV, text.encode())).decode()


def _dec_addr(key, nonce, text):
    return _aes_ofb(_derive(key, nonce), ADDR_IV, base64.b64decode(text)).decode()


def _dev_auth(username, key, nonce, randsalt, payload=""):
    now = int(time.time())
    sig = base64.b64encode(hmac.new(key, f"{nonce}{now}{payload}".encode(), hashlib.sha256).digest()).decode()
    salt = f"<RandSalt>{randsalt}</RandSalt>" if randsalt else ""
    return (f"<CreateDate>{now}</CreateDate><DevAuth>{sig}</DevAuth><Nonce>{nonce}</Nonce>"
            f"{salt}<UserName>{username}</UserName>")


def _xml_get(body, tag):
    m = re.search(rf"<{tag}>(.*?)</{tag}>", body or "", re.S)
    return m.group(1) if m else None


def _addr(text):
    host, port = text.rsplit(":", 1)
    return host, int(port)


def _inv_addr(host, port):
    return bytes(0xFF - b for b in port.to_bytes(2, "big") + socket.inet_aton(host))


class DHResponse:
    def __init__(self, raw):
        text = raw.decode(errors="replace")
        head, _, self.body = text.partition("\r\n\r\n")
        lines = head.split("\r\n")
        parts = lines[0].split(" ", 2)
        self.code = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        self.status = parts[2] if len(parts) > 2 else ""

    def get(self, tag):
        return _xml_get(self.body, tag)

    def __repr__(self):
        return f"<DH {self.code} {self.status} {self.body[:200]!r}>"


# ---------------------------------------------------------------- UDP katmanı

class _UDP(asyncio.DatagramProtocol):
    """Tek bir UDP soketi; gelen paketler kuyruğa düşer."""

    def __init__(self):
        self.queue = asyncio.Queue()
        self.transport = None
        self.on_packet = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        if self.on_packet:
            self.on_packet(data, addr)
        else:
            self.queue.put_nowait((data, addr))

    def error_received(self, exc):
        log.debug("udp hata: %s", exc)

    @property
    def port(self):
        return self.transport.get_extra_info("sockname")[1]

    def send(self, data, addr):
        self.transport.sendto(data, addr)

    async def recv(self, timeout=5.0, src=None):
        end = time.monotonic() + timeout
        while True:
            left = end - time.monotonic()
            if left <= 0:
                raise asyncio.TimeoutError
            data, addr = await asyncio.wait_for(self.queue.get(), left)
            if src is None or addr[1] == src[1]:
                return data, addr

    def close(self):
        if self.transport:
            self.transport.close()


async def _udp(loop):
    t, p = await loop.create_datagram_endpoint(_UDP, local_addr=("0.0.0.0", 0))
    return p


class _Cloud:
    """Dahua bulutunun HTTP-benzeri UDP istek formatı."""

    def __init__(self):
        self.cseq = 0

    def build(self, path, body=""):
        self.cseq += 1
        nonce = random.randrange(2 ** 31)
        created = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        digest = base64.b64encode(hashlib.sha1(f"{nonce}{created}DHP2P:{CLOUD_USER}:{CLOUD_KEY}".encode()).digest()).decode()
        req = (f"{'DHPOST' if body else 'DHGET'} {path} HTTP/1.1\r\n"
               f"CSeq: {self.cseq}\r\n"
               f'Authorization: WSSE profile="UsernameToken"\r\n'
               f'X-WSSE: UsernameToken Username="{CLOUD_USER}", PasswordDigest="{digest}", '
               f'Nonce="{nonce}", Created="{created}"\r\n')
        if body:
            req += f"Content-Type: \r\nContent-Length: {len(body)}\r\n"
        return (req + "\r\n" + body).encode()

    async def ask(self, udp, addr, path, body="", timeout=6.0, tries=2):
        for i in range(tries):
            udp.send(self.build(path, body), addr)
            try:
                while True:
                    data, src = await udp.recv(timeout)
                    if data.startswith(b"PTCP"):
                        continue
                    r = DHResponse(data)
                    if r.code == 100:
                        continue
                    log.debug("%s -> %r", path, r)
                    return r
            except asyncio.TimeoutError:
                if i == tries - 1:
                    raise P2PError(f"Bulut sunucusu yanıt vermedi ({path})")


async def _read_dh(udp, timeout):
    while True:
        data, src = await udp.recv(timeout)
        if data.startswith(b"PTCP"):
            continue
        r = DHResponse(data)
        if r.code != 100:
            return r


# ---------------------------------------------------------------- PTCP

def _ptcp_pack(sent, recv, pid, lmid, rmid, body):
    return b"PTCP" + struct.pack(">LLLLL", sent, recv, pid, lmid, rmid) + body


def _ptcp_unpack(data):
    if len(data) < 24 or data[:4] != b"PTCP":
        return None
    sent, recv, pid, lmid, rmid = struct.unpack(">LLLLL", data[4:24])
    return sent, recv, pid, lmid, rmid, data[24:]


class _Simple:
    """El sıkışma sırasında kullanılan basit, sırayla çalışan PTCP sayaçları."""

    def __init__(self, udp, addr):
        self.udp, self.addr = udp, addr
        self.sent = self.recv = self.count = self.id = self.rmid = 0

    def send(self, body=b""):
        pid = 0x0002FFFF if body == SYNC_BODY else (0x0000FFFF - self.count) & 0xFFFFFFFF
        self.udp.send(_ptcp_pack(self.sent, self.recv, pid, self.id, self.rmid, body), self.addr)
        self.sent += len(body)
        self.id += 1
        if body and body != SYNC_BODY:
            self.count += 1

    async def read(self, timeout=5.0, skip_empty=False):
        while True:
            data, _ = await self.udp.recv(timeout, self.addr)
            p = _ptcp_unpack(data)
            if not p:
                continue
            self.recv += len(p[5])
            self.rmid = p[3]
            if skip_empty and not p[5]:
                continue
            return p


class PTCPSession:
    """
    Kurulmuş bir PTCP oturumu. Birden çok mantıksal bağlantıyı (realm) taşır.

    Akış güvenilirliği: gelen paketler `sent` ofsetine göre sıralanır, eksik olanlar
    için onay tekrarlanır; gönderilen ve henüz onaylanmayan paketler zaman aşımında
    tekrar gönderilir.
    """

    RTO = 0.6
    MAX_BUFFER = 24 * 1024 * 1024

    def __init__(self, udp, addr, simple, on_close=None):
        self.udp, self.addr = udp, addr
        self.sent, self.recv = simple.sent, simple.recv
        self.count, self.id, self.rmid = simple.count, simple.id, simple.rmid
        self.unacked = {}           # ofset -> [paket gövdesi, son gönderim zamanı, deneme]
        self.peer_acked = self.sent
        self.ooo = {}               # sıra dışı gelen paketler: ofset -> gövde
        self.realms = {}            # realm -> Connection
        self.pending = {}           # realm -> Future (CONN bekleyen)
        self.last_rx = time.monotonic()
        self.closed = False
        self.on_close = on_close
        self._ack_due = False
        udp.on_packet = self._on_packet
        # Kuyrukta bekleyenleri işle
        while not udp.queue.empty():
            d, a = udp.queue.get_nowait()
            self._on_packet(d, a)
        self._tasks = [asyncio.ensure_future(self._timer())]

    # ------------------------------------------------ gönderim
    def _emit(self, body, sent=None, lmid=None):
        pid = 0x0002FFFF if body == SYNC_BODY else (0x0000FFFF - self.count) & 0xFFFFFFFF
        off = self.sent if sent is None else sent
        self.udp.send(_ptcp_pack(off, self.recv, pid, self.id if lmid is None else lmid, self.rmid, body), self.addr)

    def send(self, body):
        if self.closed:
            raise ConnectionError("P2P oturumu kapalı")
        if body:
            self.unacked[self.sent] = [body, time.monotonic(), 0, self.id]
        self._emit(body)
        self.sent += len(body)
        self.id += 1
        if body:
            self.count += 1
        self._ack_due = False

    def ack(self):
        self._emit(b"")
        self.id += 1
        self._ack_due = False

    def buffered(self):
        return sum(len(c.reader._buffer) for c in self.realms.values())

    # ------------------------------------------------ alım
    def _on_packet(self, data, addr):
        if addr[1] != self.addr[1]:
            return
        p = _ptcp_unpack(data)
        if not p:
            return
        sent, recv, pid, lmid, rmid, body = p
        self.last_rx = time.monotonic()
        # Karşı tarafın onayı: recv, bizden aldığı toplam bayt
        if recv > self.peer_acked or (self.peer_acked - recv) > 0x7FFFFFFF:
            self.peer_acked = recv
            for off in [o for o in self.unacked if o < recv]:
                del self.unacked[off]
        if not body:
            return
        if sent == self.recv and body[0] == 0x10 and self.buffered() > self.MAX_BUFFER:
            # Tüketici yavaş: paketi kabul etme, cihaz daha sonra yeniden gönderir
            return
        if sent == self.recv:
            self._accept(lmid, body)
            while self.recv in self.ooo:
                b, l = self.ooo.pop(self.recv)
                self._accept(l, b)
        elif ((sent - self.recv) & 0xFFFFFFFF) < 0x7FFFFFFF:
            # ileride bir paket: boşluk var, sakla
            if len(self.ooo) < 4096:
                self.ooo[sent] = (body, lmid)
        # (geçmişte kalmış kopya paketler sadece onaylanır)
        self.ack()

    def _accept(self, lmid, body):
        self.recv = (self.recv + len(body)) & 0xFFFFFFFF
        self.rmid = lmid
        t = body[0]
        if t == 0x10 and len(body) >= 12:
            length = struct.unpack(">L", body[:4])[0] & 0xFFFF
            realm = struct.unpack(">L", body[4:8])[0]
            c = self.realms.get(realm)
            if c:
                c.feed(body[12:12 + length])
        elif t == 0x12 and len(body) >= 12:
            realm = struct.unpack(">L", body[4:8])[0]
            st = body[12:16]
            fut = self.pending.pop(realm, None)
            if st == b"CONN":
                if fut and not fut.done():
                    fut.set_result(True)
            else:
                if fut and not fut.done():
                    fut.set_exception(ConnectionRefusedError("Cihaz bağlantıyı reddetti"))
                c = self.realms.pop(realm, None)
                if c:
                    c.remote_closed()
        # 0x13 kalp atışı ve diğer komutlar için yalnızca onay yeterli

    # ------------------------------------------------ zamanlayıcı
    async def _timer(self):
        last_hb = time.monotonic()
        try:
            while not self.closed:
                await asyncio.sleep(0.2)
                now = time.monotonic()
                for off, item in sorted(self.unacked.items())[:64]:
                    body, ts, tries, lmid = item
                    if now - ts > self.RTO * (1 + min(tries, 4)):
                        if tries > 25:
                            log.warning("P2P: paket onaylanmadı, oturum kapanıyor")
                            self.close()
                            return
                        self._emit(body, sent=off, lmid=lmid)
                        item[1], item[2] = now, tries + 1
                if now - last_hb > 5:
                    last_hb = now
                    self.send(b"\x13\x00\x00\x00" + b"\x00" * 8)
                if now - self.last_rx > 30:
                    log.warning("P2P: cihazdan 30 sn yanıt yok, oturum kapanıyor")
                    self.close()
                    return
        except asyncio.CancelledError:
            pass

    # ------------------------------------------------ bağlantılar
    async def open(self, port):
        for i in range(3):
            try:
                return await self._open(port)
            except asyncio.TimeoutError:
                if self.closed or i == 2:
                    raise ConnectionError("Cihazda bağlantı açılamadı (zaman aşımı)")

    async def _open(self, port):
        realm = random.randint(1, 0xFFFFFFFE)
        while realm in self.realms:
            realm = random.randint(1, 0xFFFFFFFE)
        conn = Connection(self, realm)
        self.realms[realm] = conn
        fut = asyncio.get_running_loop().create_future()
        self.pending[realm] = fut
        self.send(b"\x11\x00\x00\x00" + struct.pack(">LLL", realm, 0, port) + b"\x7f\x00\x00\x01")
        try:
            await asyncio.wait_for(fut, 6)
        except Exception:
            self.realms.pop(realm, None)
            self.pending.pop(realm, None)
            raise
        return conn

    def close(self):
        if self.closed:
            return
        # Cihaza açık bağlantıları kapattığımızı bildir (yoksa akışlar cihazda bir süre daha sürer)
        for realm in list(self.realms):
            try:
                body = b"\x12\x00\x00\x00" + struct.pack(">LL", realm, 0) + b"DISC"
                self._emit(body)
                self.sent += len(body)
                self.id += 1
                self.count += 1
            except Exception:
                pass
        self.closed = True
        for c in list(self.realms.values()):
            c.remote_closed()
        self.realms.clear()
        for t in self._tasks:
            t.cancel()
        self.udp.close()
        if self.on_close:
            self.on_close()


class Connection:
    """PTCP içindeki tek bir TCP bağlantısı."""

    def __init__(self, session, realm):
        self.s, self.realm = session, realm
        self.reader = asyncio.StreamReader(limit=2 ** 22)
        self.closed = False

    def feed(self, data):
        if not self.closed:
            self.reader.feed_data(data)

    def remote_closed(self):
        if not self.closed:
            self.closed = True
            self.reader.feed_eof()

    async def write(self, data):
        for i in range(0, len(data), MAX_CHUNK):
            chunk = data[i:i + MAX_CHUNK]
            # tıkanıklık kontrolü: çok fazla onaylanmamış veri varsa bekle
            while len(self.s.unacked) > 256 and not self.s.closed:
                await asyncio.sleep(0.01)
            self.s.send(struct.pack(">LLL", 0x10000000 | len(chunk), self.realm, 0) + chunk)

    def close(self):
        if not self.closed:
            self.closed = True
            self.s.realms.pop(self.realm, None)
            if not self.s.closed:
                self.s.send(b"\x12\x00\x00\x00" + struct.pack(">LL", self.realm, 0) + b"DISC")
            self.reader.feed_eof()


# ---------------------------------------------------------------- el sıkışma

async def connect(serial, username, password, status=None):
    """
    SN üzerinden cihaza P2P oturumu kurar. Dönen değer: (PTCPSession, cihaz_bilgisi)
    """
    say = status or (lambda m: log.info(m))
    loop = asyncio.get_running_loop()
    host = (await loop.getaddrinfo(MAIN_SERVER[0], None, family=socket.AF_INET))[0][4][0]
    main = (host, MAIN_SERVER[1])
    cloud = _Cloud()

    a = await _udp(loop)
    try:
        say("Dahua bulut sunucusuna bağlanılıyor…")
        await cloud.ask(a, main, "/probe/p2psrv")
        r = await cloud.ask(a, main, f"/online/p2psrv/{serial}")
        if r.code >= 400 or not r.get("US"):
            raise P2PError("Cihaz bulutta bulunamadı. SN numarasını ve cihazın internet bağlantısını kontrol edin.")
        p2psrv = _addr(r.get("US"))

        say("Cihaz aranıyor…")
        b = await _udp(loop)
        try:
            r = await cloud.ask(b, p2psrv, f"/probe/device/{serial}")
            if r.code >= 400:
                raise P2PError("Cihaz çevrimdışı görünüyor.")
            info = {}
            try:
                r = await cloud.ask(b, p2psrv, f"/info/device/{serial}", tries=1, timeout=4)
                if r.code < 300:
                    info = _decrypt_info(r.get("Info"))
            except P2PError:
                pass
        finally:
            b.close()
        randsalt = info.get("randsalt", "")
        r = await cloud.ask(a, main, "/online/relay")
        relay = _addr(r.get("Address"))
    except Exception:
        a.close()
        raise

    last_err = None
    for use_auth in (False, True):
        try:
            return await _channel(loop, cloud, main, relay, a, serial, username, password,
                                  randsalt, use_auth, info, say)
        except _NeedAuth as e:
            last_err = e
            a.close()
            a = await _udp(loop)
            continue
    raise P2PError(str(last_err))


class _NeedAuth(P2PError):
    pass


async def _channel(loop, cloud, main, relay, a, serial, username, password, randsalt, use_auth, info, say):
    dev = await _udp(loop)  # cihazla konuşacak soket (p2p-channel aynı porttan gitmeli)
    agent_udp = a
    try:
        aid = os.urandom(8)
        laddr = f"127.0.0.1:{dev.port}"
        ipaddr = f"<IpEncrpt>true</IpEncrpt><LocalAddr>{laddr}</LocalAddr>"
        auth = ""
        key = nonce = None
        if use_auth:
            key = _pass_key(username, password, randsalt)
            nonce = random.randrange(2 ** 31)
            enc = _enc_addr(key, nonce, laddr)
            ipaddr = f"<IpEncrptV2>true</IpEncrptV2><LocalAddr>{enc}</LocalAddr>"
            auth = _dev_auth(username, key, nonce, randsalt, enc)

        say("P2P kanalı açılıyor…")
        dev.send(cloud.build(
            f"/device/{serial}/p2p-channel",
            f"<body>{auth}<Identify>{' '.join(f'{x:x}' for x in aid)}</Identify>{ipaddr}<version>5.0.0</version></body>"),
            main)

        r = await cloud.ask(agent_udp, relay, "/relay/agent")
        token, agent = r.get("Token"), _addr(r.get("Agent"))
        await cloud.ask(agent_udp, agent, f"/relay/start/{token}", "<body><Client>:0</Client></body>")

        try:
            r = await _read_dh(dev, 10)
        except asyncio.TimeoutError:
            raise P2PError("Cihaz P2P isteğine yanıt vermedi.")
        if r.code == 403 and not use_auth:
            raise _NeedAuth("Cihaz kimlik doğrulaması istiyor")
        if r.code == 403 or r.code == 401:
            raise P2PError("Kullanıcı adı veya şifre hatalı.")
        if r.code >= 400:
            raise P2PError(f"Cihaz bağlantıyı reddetti: {r.code} {r.status}")

        device_laddr = r.get("LocalAddr")
        if use_auth and r.get("Nonce"):
            try:
                device_laddr = _dec_addr(key, r.get("Nonce"), device_laddr)
            except Exception:
                pass
        pub = _addr(r.get("PubAddr"))
        if use_auth:
            auth = _dev_auth(username, key, r.get("Nonce") or nonce, randsalt)

        agent_udp.send(cloud.build(f"/device/{serial}/relay-channel",
                                   f"<body>{auth}<agentAddr>{agent[0]}:{agent[1]}</agentAddr></body>"), main)
        # Agent sunucusundan "Server Nat Info" gelmesini bekle
        await _read_dh(agent_udp, 10)
        relay_s = _Simple(agent_udp, agent)
        relay_s.send(SYNC_BODY)
        await relay_s.read(5)

        # ---- doğrudan delik açma (hole punching) dene
        say("Cihaza doğrudan bağlanılıyor…")
        try:
            sess = await _direct(dev, pub, device_laddr, aid, relay_s, use_auth)
            agent_udp.close()
            log.info("P2P doğrudan bağlantı kuruldu (%s:%s)", *pub)
            return sess, {"mode": "p2p", **info}
        except (asyncio.TimeoutError, P2PError) as e:
            log.info("Doğrudan bağlantı olmadı (%s), aktarma (relay) moduna geçiliyor", e)

        say("Aktarma sunucusu üzerinden bağlanılıyor…")
        dev.close()
        sess = PTCPSession(agent_udp, agent, relay_s)
        return sess, {"mode": "relay", **info}
    except Exception:
        dev.close()
        raise


async def _direct(dev, pub, device_laddr, aid, relay_s, use_auth):
    relay_s.send(b"\x17\x00\x00\x00" + b"\x00" * 8)
    p = await relay_s.read(5, skip_empty=True)
    sign = p[5][12:]
    relay_s.send()

    caid = bytes(0xFF - x for x in aid)
    cookie = os.urandom(4)
    tid = os.urandom(12)
    dev.send(b"\xff\xfe\xff\xe7" + cookie + tid + b"\x7f\xd5\xff\xf7" + caid
             + b"\xff\xfb\xff\xf7\xff\xfe" + _inv_addr(*pub), pub)
    data, _ = await dev.recv(5, pub)
    rtid = data[8:20]
    lh, lp = _addr(device_laddr)
    dev.send(b"\xfe\xfe\xff\xe7" + cookie + rtid + b"\x7f\xd6\xff\xf7" + caid
             + b"\xff\xfb\xff\xf7\xff\xfe" + _inv_addr(lh, lp), pub)
    if use_auth:
        await dev.recv(5, pub)
        msg = (b"\xfe\xfe\xff\xf3" + cookie + rtid + b"\x7f\xd6\xff\xf7" + caid
               + b"\xff\xfb\xff\xf7\xff\xfe" + b"\xa8\x13\x3f\x57\xfe\x37")
        for _ in range(5):
            dev.send(msg, pub)
    # STUN benzeri yanıtları boşalt
    end = time.monotonic() + 3
    got = 0
    while got < 5 and time.monotonic() < end:
        try:
            await dev.recv(end - time.monotonic(), pub)
            got += 1
        except asyncio.TimeoutError:
            break

    s = _Simple(dev, pub)
    s.send(SYNC_BODY)
    while True:
        p = await s.read(5)
        if p[5] == SYNC_BODY:
            break
    s.send(b"\x19\x00\x00\x00" + b"\x00" * 8 + sign)
    p = await s.read(5, skip_empty=True)
    if p[5][0] != 0x1A:
        raise P2PError("Cihaz doğrulaması başarısız")
    s.send(b"\x1b\x00\x00\x00" + b"\x00" * 8)
    try:
        await s.read(3)
    except asyncio.TimeoutError:
        pass
    return PTCPSession(dev, pub, s)


# ---------------------------------------------------------------- yerel köprü

class Tunnel:
    """
    P2P oturumunu yerel TCP portlarına bağlar: 127.0.0.1:<yerel> -> cihaz:<uzak>
    Oturum düşerse ilk yeni bağlantıda otomatik olarak yeniden kurulur.
    """

    def __init__(self, serial, username, password, status=None):
        self.serial, self.username, self.password = serial, username, password
        self.status = status
        self.session = None
        self.info = {}
        self.ports = {}      # uzak port -> yerel port
        self.servers = []
        self._lock = asyncio.Lock()

    async def ensure(self):
        async with self._lock:
            if self.session and not self.session.closed:
                return self.session
            err = None
            for attempt in range(3):
                try:
                    self.session, self.info = await connect(self.serial, self.username, self.password, self.status)
                    return self.session
                except P2PError as e:
                    err = e
                    if "hatalı" in str(e) or "bulunamadı" in str(e):
                        break
                except (asyncio.TimeoutError, OSError) as e:
                    err = P2PError("Cihaza bağlanılamadı, tekrar deneniyor…")
                log.info("P2P bağlantı denemesi %d başarısız: %s", attempt + 1, err)
                await asyncio.sleep(1 + attempt)
            raise err

    async def expose(self, remote_port):
        if remote_port in self.ports:
            return self.ports[remote_port]

        async def handle(reader, writer):
            try:
                sess = await self.ensure()
                conn = await sess.open(remote_port)
            except Exception as e:
                log.warning("Tünel bağlantısı açılamadı: %s", e)
                writer.close()
                return

            async def up():
                try:
                    while True:
                        d = await reader.read(65536)
                        if not d:
                            break
                        await conn.write(d)
                except Exception:
                    pass
                finally:
                    conn.close()

            async def down():
                try:
                    while True:
                        d = await conn.reader.read(65536)
                        if not d:
                            break
                        writer.write(d)
                        await writer.drain()
                except Exception:
                    pass
                finally:
                    try:
                        writer.close()
                    except Exception:
                        pass

            await asyncio.gather(up(), down())

        srv = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = srv.sockets[0].getsockname()[1]
        self.servers.append(srv)
        self.ports[remote_port] = port
        return port

    def close(self):
        for s in self.servers:
            s.close()
        self.servers.clear()
        self.ports.clear()
        if self.session:
            self.session.close()
            self.session = None

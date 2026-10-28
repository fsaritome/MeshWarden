"""MeshWarden - a read-only MeshCore DM bot for your home network.

Connects to a MeshCore companion over TCP (WiFi firmware), waits for DMs from allowed keys and
answers a fixed set of read-only commands (solar, network, hosts, Home Assistant, TrueNAS, repeater).
Sends alerts and a morning summary. Nothing received from the mesh is ever executed or switched.
All settings and secrets come from environment variables (see meshwarden.env.example).

Copyright (c) 2026 Ferit Sari Tomé - MIT License, see LICENSE.
"""
import asyncio, json, logging, os, socket, ssl, time, urllib.parse, urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from meshcore import MeshCore, EventType

E = os.getenv
NODE_HOST, NODE_PORT = E("NODE_HOST", ""), int(E("NODE_PORT", "5000"))
PROM_URL, PROM_JOB = E("PROM_URL", "http://127.0.0.1:9090"), E("PROM_JOB", "solar")
HA_URL, HA_TOKEN = E("HA_URL", ""), E("HA_TOKEN", "")
HA_SENSORS = [s.strip() for s in E("HA_SENSORS", "").split(",") if s.strip()]  # optional explicit list
NAS_URL, NAS_KEY = E("NAS_URL", ""), E("NAS_KEY", "")
REPEATER_KEY, REPEATER_PW = E("REPEATER_KEY", ""), E("REPEATER_PW", "")
GATEWAY = E("GATEWAY", "")
ALLOWED = [p.strip().lower() for p in E("ALLOWED_KEYS", "").split(",") if p.strip()]
ALERT_KEYS = [p.strip().lower() for p in E("ALERT_KEYS", "").split(",") if p.strip()]
SUMMARY_AT = E("SUMMARY_AT", "07:00")  # local time, empty = off
TZ = ZoneInfo(E("BOT_TZ", "Europe/Berlin"))
COOLDOWN_S, MAX_LEN, MAX_PARTS = float(E("COOLDOWN_S", "3")), 140, 3
RATE_PER_MIN, QUEUE_MAX = int(E("RATE_PER_MIN", "10")), 5

logging.basicConfig(level=E("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("meshwarden")
_insecure = ssl.create_default_context(); _insecure.check_hostname = False; _insecure.verify_mode = ssl.CERT_NONE

# ---------------------------------------------------------------- helpers
SOC_TABLE = [(13.60, 100), (13.40, 99), (13.30, 90), (13.20, 70), (13.10, 40), (13.00, 30),
             (12.90, 20), (12.80, 17), (12.50, 14), (12.00, 9), (10.00, 0)]


def soc(v: float) -> int:
    if v >= SOC_TABLE[0][0]:
        return 100
    for (vh, sh), (vl, sl) in zip(SOC_TABLE, SOC_TABLE[1:]):
        if vl <= v <= vh:
            return round(sl + (v - vl) / (vh - vl) * (sh - sl))
    return 0


def http_json(url, headers=None, insecure=False, timeout=6):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout, context=_insecure if insecure else None) as r:
        return json.load(r)


def prom(q: str) -> list:
    return http_json(f"{PROM_URL}/api/v1/query?query=" + urllib.parse.quote(q))["data"]["result"]


def prom1(q: str, default=None):
    r = prom(q)
    return float(r[0]["value"][1]) if r else default


def tcp_ok(host, port, timeout=3) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def short(name: str, n=12) -> str:
    for w in ("Temperature", "Temperatur", "Humidity", "Luftfeuchtigkeit", "Sensor", "sensor"):
        name = name.replace(w, "")
    name = " ".join(name.split())
    return name[:n]


# ---------------------------------------------------------------- commands (all read-only)
def cmd_solar():
    rows = prom(f'{{job="{PROM_JOB}",__name__=~"battery_voltage|battery_current|mppt_pv_power|inverter_status|battery_has_alarm|mppt_has_error"}}')
    m = {r["metric"]["__name__"]: float(r["value"][1]) for r in rows}
    if not m:
        return "Solar: no data"
    v = m.get("battery_voltage", 0)
    flags = [f for k, f in (("battery_has_alarm", "BATT ALARM"), ("mppt_has_error", "MPPT ERR")) if m.get(k)]
    return (f"Batt {v:.2f}V ~{soc(v)}% {m.get('battery_current', 0):+.1f}A | PV {m.get('mppt_pv_power', 0):.0f}W | "
            f"Inv {'ON' if m.get('inverter_status') else 'OFF'} | " + (",".join(flags) if flags else "ok"))


def cmd_today():
    now = datetime.now(TZ)
    h = max(0.05, (now - now.replace(hour=0, minute=0, second=0, microsecond=0)).total_seconds() / 3600)
    rng = f"{int(h * 60)}m"
    wh = prom1(f'avg_over_time(mppt_pv_power{{job="{PROM_JOB}"}}[{rng}])', 0) * h
    pk = prom1(f'max_over_time(mppt_pv_power{{job="{PROM_JOB}"}}[{rng}])', 0)
    vmin = prom1(f'min_over_time(battery_voltage{{job="{PROM_JOB}"}}[12h])', 0)
    vmax = prom1(f'max_over_time(battery_voltage{{job="{PROM_JOB}"}}[{rng}])', 0)
    return f"Today PV {wh / 1000:.2f}kWh peak {pk:.0f}W | Batt min12h {vmin:.2f}V max {vmax:.2f}V"


def cmd_hosts():
    rows = prom("up")
    down = [r["metric"].get("job", "?") + "/" + r["metric"].get("instance", "?").split(":")[0].split(".")[0] for r in rows if r["value"][1] == "0"]
    out = f"Targets {len(rows) - len(down)}/{len(rows)} up"
    if down:
        out += " | DOWN: " + ", ".join(down)
    return out


def cmd_net():
    inet = tcp_ok("1.1.1.1", 53) or tcp_ok("8.8.8.8", 53)
    try:
        socket.getaddrinfo("example.com", 443); dns = True
    except OSError:
        dns = False
    t0 = time.time(); web = tcp_ok("example.com", 443); rtt = (time.time() - t0) * 1000
    gw_txt = ""
    if GATEWAY:
        gw = tcp_ok(GATEWAY, 80) or tcp_ok(GATEWAY, 443) or tcp_ok(GATEWAY, 53)
        gw_txt = f"GW {'OK' if gw else 'DOWN'} | "
    return gw_txt + f"Inet {'OK' if inet else 'DOWN'} | DNS {'OK' if dns else 'FAIL'} | Web {'%.0fms' % rtt if web else 'FAIL'}"


def cmd_status():
    p = urllib.parse.urlparse(PROM_URL)
    prom_ok = tcp_ok(p.hostname, p.port or 80)
    solar = prom1(f'up{{job="{PROM_JOB}"}}') if prom_ok else None
    inet = tcp_ok("1.1.1.1", 53) or tcp_ok("8.8.8.8", 53)
    f = lambda b: "OK" if b else "DOWN"
    out = f"Net {f(inet)} | Prom {f(prom_ok)} | Solar {f(solar == 1)}"
    if HA_URL:
        out += f" | HA {f(tcp_ok(*_hostport(HA_URL)))}"
    if NAS_URL:
        out += f" | NAS {f(tcp_ok(*_hostport(NAS_URL)))}"
    return out


def _hostport(url):
    u = urllib.parse.urlparse(url)
    return u.hostname, u.port or (443 if u.scheme == "https" else 80)


def _ha_states():
    if not (HA_URL and HA_TOKEN):
        raise RuntimeError("HA not configured")
    return http_json(f"{HA_URL}/api/states", {"Authorization": f"Bearer {HA_TOKEN}"})


def cmd_sensors():
    try:
        st = _ha_states()
    except RuntimeError as e:
        return str(e)
    by_id = {s["entity_id"]: s for s in st}
    if HA_SENSORS:
        pick = [by_id[i] for i in HA_SENSORS if i in by_id]
    else:
        pick = [s for s in st if s["entity_id"].startswith("sensor.")
                and s["attributes"].get("device_class") in ("temperature", "humidity")
                and s["state"] not in ("unknown", "unavailable")][:12]
    parts = []
    for s in pick:
        a = s["attributes"]; unit = a.get("unit_of_measurement", "")
        try:
            val = f"{float(s['state']):.1f}"
        except ValueError:
            val = s["state"]
        parts.append(f"{short(a.get('friendly_name', s['entity_id']))} {val}{unit}")
    return " | ".join(parts) if parts else "no sensors found"


def cmd_printer():
    try:
        st = _ha_states()
    except RuntimeError as e:
        return str(e)
    want = {"print_status": None, "print_progress": None, "remaining_time": None, "current_stage": None, "task_name": None}
    for s in st:
        eid = s["entity_id"]
        if not eid.startswith("sensor."):
            continue
        for k in want:
            if want[k] is None and eid.endswith(k) and s["state"] not in ("unknown", "unavailable"):
                want[k] = s["state"]
    if not any(want.values()):
        return "no printer entities found"
    out = f"Printer {want['print_status'] or want['current_stage'] or '?'}"
    if want["print_progress"]:
        out += f" {want['print_progress']}%"
    if want["remaining_time"]:
        try:
            m = float(want["remaining_time"]); out += f" | {int(m // 60)}h{int(m % 60):02d} left"
        except ValueError:
            pass
    if want["task_name"]:
        out += f" | {want['task_name'][:30]}"
    return out


def _nas(path):
    if not (NAS_URL and NAS_KEY):
        raise RuntimeError("NAS not configured")
    return http_json(f"{NAS_URL}/api/v2.0/{path}", {"Authorization": f"Bearer {NAS_KEY}"}, insecure=True)


def cmd_nas():
    try:
        pools = _nas("pool")
    except RuntimeError as e:
        return str(e)
    parts = []
    for p in pools:
        txt = f"{p['name']} {p.get('status', '?')}"
        size, alloc = p.get("size"), p.get("allocated")
        if size and alloc:
            txt += f" {alloc / size * 100:.0f}%"
        parts.append(txt)
    try:
        alerts = [a for a in _nas("alert/list") if not a.get("dismissed") and a.get("level") not in ("INFO", "NOTICE")]
        parts.append(f"{len(alerts)} alerts" if alerts else "no alerts")
    except Exception:
        pass
    return "NAS " + " | ".join(parts)


HELP = "solar today status net hosts sensors printer nas mesh ping help"


# ---------------------------------------------------------------- bot
class Bot:
    def __init__(self):
        self.mc: MeshCore | None = None
        self.last_reply: dict[str, float] = {}
        self.recent: dict[str, list] = {}
        self.queues: dict[str, asyncio.Queue] = {}
        self.alert_state: dict[str, bool] = {}
        self.pending: dict[str, float] = {}
        self.last_summary_day = None
        self.inverter_on = None

    async def send(self, prefix: str, text: str):
        mc = self.mc
        contact = mc.get_contact_by_key_prefix(prefix)
        if contact is None:
            await mc.ensure_contacts(follow=True)
            contact = mc.get_contact_by_key_prefix(prefix)
        if contact is None:
            log.warning("no contact for %s", prefix); return
        chunks = [text[i:i + MAX_LEN] for i in range(0, len(text), MAX_LEN)][:MAX_PARTS]
        for i, c in enumerate(chunks):
            if len(chunks) > 1:
                c = f"{i + 1}/{len(chunks)} {c}"
            res = await mc.commands.send_msg_with_retry(contact, c)
            log.info("-> %s: %s [%s]", prefix, c, "ok" if res else "no ack")

    async def cmd_mesh(self) -> str:
        mc = self.mc
        if not REPEATER_KEY:
            return "no repeater configured"
        rep = mc.get_contact_by_key_prefix(REPEATER_KEY[:12])
        if rep is None:
            await mc.ensure_contacts(follow=True); rep = mc.get_contact_by_key_prefix(REPEATER_KEY[:12])
        if rep is None:
            return "repeater not in contacts yet"
        if not await mc.commands.send_login_sync(rep, REPEATER_PW, timeout=20):
            return "repeater login failed"
        await mc.commands.send_statusreq(rep)
        res = await mc.wait_for_event(EventType.STATUS_RESPONSE, timeout=20)
        st = res.payload if res else None
        if not st:
            return "repeater: no status reply"
        g = st.get
        return (f"Rep {g('bat', 0) / 1000:.2f}V up {int(g('uptime', 0)) // 3600}h | noise {g('noise_floor')}dBm | "
                f"lastSNR {g('last_snr')} | rx {g('nb_recv')} tx {g('nb_sent')}")

    async def handle(self, event):
        p = event.payload
        prefix, text = (p.get("pubkey_prefix") or "").lower(), (p.get("text") or "").strip()
        if not any(prefix.startswith(a[:12]) or a.startswith(prefix) for a in ALLOWED):
            log.info("ignored DM from %s", prefix); return
        now = time.time()
        hist = [t for t in self.recent.get(prefix, []) if now - t < 60]
        if len(hist) >= RATE_PER_MIN:
            self.recent[prefix] = hist
            log.warning("rate limit: dropped '%s' from %s", text, prefix); return
        hist.append(now); self.recent[prefix] = hist
        q = self.queues.get(prefix)
        if q is None:
            q = self.queues[prefix] = asyncio.Queue(maxsize=QUEUE_MAX)
            asyncio.create_task(self._worker(prefix, q))
        try:
            q.put_nowait(p)
        except asyncio.QueueFull:
            log.warning("queue full: dropped '%s' from %s", text, prefix)

    async def _worker(self, prefix, q):
        while True:
            p = await q.get()
            wait = COOLDOWN_S - (time.time() - self.last_reply.get(prefix, 0))
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                await self.process(prefix, p)
            except Exception:
                log.exception("process failed")
            self.last_reply[prefix] = time.time()

    async def process(self, prefix, p):
        text = (p.get("text") or "").strip()
        c = text.lower()
        log.info("<- %s: %s", prefix, text)
        sync_cmds = {"solar": cmd_solar, "s": cmd_solar, "today": cmd_today, "t": cmd_today,
                     "status": cmd_status, "st": cmd_status, "net": cmd_net, "hosts": cmd_hosts,
                     "sensors": cmd_sensors, "temp": cmd_sensors, "printer": cmd_printer, "nas": cmd_nas}
        try:
            if c in sync_cmds:
                reply = await asyncio.to_thread(sync_cmds[c])
            elif c == "mesh":
                reply = await self.cmd_mesh()
            elif c in ("ping", "test"):
                hops = p.get("path_len"); snr = p.get("SNR")
                reply = "pong | " + ("direct" if hops in (255, None) else f"{hops} hops") + (f" | SNR {snr:.1f}dB" if snr is not None else "")
            elif c in ("help", "?", "h"):
                reply = HELP
            else:
                reply = "unknown | " + HELP
        except Exception as e:
            log.exception("command failed"); reply = f"error: {type(e).__name__}"
        await self.send(prefix, reply)

    # ------------------------------------------------------------ alerts
    def _checks(self) -> dict:
        """returns {alert_name: (is_bad, message)}"""
        out = {}
        try:
            v = prom1(f'battery_voltage{{job="{PROM_JOB}"}}')
            if v is not None:
                out["batt_low"] = (soc(v) < 20, f"Battery low: {v:.2f}V ~{soc(v)}%")
            for name, q, msg in (("batt_alarm", "battery_has_alarm", "Battery ALARM"),
                                 ("mppt_err", "mppt_has_error", "MPPT error")):
                val = prom1(f'{q}{{job="{PROM_JOB}"}}')
                if val is not None:
                    out[name] = (val != 0, msg)
            inv = prom1(f'inverter_status{{job="{PROM_JOB}"}}')
            if inv is not None:
                out["_inverter"] = (inv != 0, "")
            down = [r["metric"].get("job", "?") for r in prom("up") if r["value"][1] == "0"]
            out["targets_down"] = (bool(down), "Down: " + ", ".join(down))
            out["prom"] = (False, "")
        except Exception:
            out["prom"] = (True, "Prometheus unreachable")
        inet = tcp_ok("1.1.1.1", 53) or tcp_ok("8.8.8.8", 53)
        out["internet"] = (not inet, "Internet DOWN")
        if NAS_URL and NAS_KEY:
            try:
                bad = [p["name"] for p in _nas("pool") if not p.get("healthy", True) or p.get("status") not in ("ONLINE", None)]
                out["nas"] = (bool(bad), "NAS pool problem: " + ", ".join(bad))
            except Exception:
                out["nas"] = (True, "NAS API unreachable")
        if HA_URL and HA_TOKEN:
            out["ha"] = (not tcp_ok(*_hostport(HA_URL)), "Home Assistant unreachable")
        return out

    async def alert_loop(self):
        await asyncio.sleep(30)
        HOLD = 300  # a condition must persist 5 min before alerting
        while True:
            try:
                checks = await asyncio.to_thread(self._checks)
                now = time.time()
                inv = checks.pop("_inverter", None)
                if inv is not None:
                    on = inv[0]
                    if self.inverter_on is not None and on != self.inverter_on:
                        await self.broadcast(f"Inverter switched {'ON' if on else 'OFF'}")
                    self.inverter_on = on
                for name, (bad, msg) in checks.items():
                    was = self.alert_state.get(name, False)
                    if bad and not was:
                        self.pending.setdefault(name, now)
                        if now - self.pending[name] >= HOLD:
                            self.alert_state[name] = True; self.pending.pop(name, None)
                            await self.broadcast("ALERT " + msg)
                    elif not bad:
                        self.pending.pop(name, None)
                        if was:
                            self.alert_state[name] = False
                            await self.broadcast(f"OK again: {name.replace('_', ' ')}")
                await self.maybe_summary()
            except Exception:
                log.exception("alert loop")
            await asyncio.sleep(60)

    async def maybe_summary(self):
        if not SUMMARY_AT:
            return
        now = datetime.now(TZ)
        hh, mm = map(int, SUMMARY_AT.split(":"))
        if now.hour == hh and now.minute >= mm and self.last_summary_day != now.date():
            self.last_summary_day = now.date()
            parts = []
            for f in (cmd_solar, cmd_status):
                try:
                    parts.append(await asyncio.to_thread(f))
                except Exception:
                    pass
            try:
                vmin = await asyncio.to_thread(prom1, f'min_over_time(battery_voltage{{job="{PROM_JOB}"}}[10h])', 0)
                parts.insert(0, f"Morning: night min {vmin:.2f}V")
            except Exception:
                pass
            await self.broadcast(" | ".join(parts))

    async def broadcast(self, text):
        log.warning("broadcast: %s", text)
        for k in ALERT_KEYS:
            try:
                await self.send(k, text)
            except Exception:
                log.exception("alert send failed")

    # ------------------------------------------------------------ main
    async def run(self):
        if not NODE_HOST:
            raise SystemExit("NODE_HOST is not set - see meshwarden.env.example")
        if not ALLOWED:
            log.warning("ALLOWED_KEYS is empty - the bot will not answer anyone")
        asyncio.create_task(self.alert_loop())
        while True:
            try:
                self.mc = await MeshCore.create_tcp(NODE_HOST, NODE_PORT, auto_reconnect=True, max_reconnect_attempts=1000)
                await self.mc.ensure_contacts()
                log.info("connected to %s, allowed %s, alerts to %s, HA %s, NAS %s",
                         self.mc.self_info.get("name"), ALLOWED, ALERT_KEYS,
                         "on" if HA_TOKEN else "off", "on" if NAS_KEY else "off")
                self.mc.subscribe(EventType.CONTACT_MSG_RECV, self.handle)
                await self.mc.start_auto_message_fetching()
                fails = 0
                while self.mc.is_connected:
                    await asyncio.sleep(30)
                    # watchdog: poll for waiting messages (pushes can get lost) and check the node answers
                    try:
                        for _ in range(10):
                            ev = await asyncio.wait_for(self.mc.commands.get_msg(), 10)
                            if ev is None or ev.type in (EventType.NO_MORE_MSGS, EventType.ERROR):
                                break
                            if ev.type == EventType.CONTACT_MSG_RECV:
                                log.warning("watchdog picked up a waiting message")
                        fails = 0
                    except Exception as e:
                        fails += 1
                        log.warning("watchdog poll failed (%d): %r", fails, e)
                        if fails >= 3:
                            log.error("node not answering, reconnecting")
                            break
                try:
                    await self.mc.disconnect()
                except Exception:
                    pass
                log.warning("connection lost")
            except Exception:
                log.exception("bot error, retry in 15 s")
            await asyncio.sleep(15)


if __name__ == "__main__":
    asyncio.run(Bot().run())

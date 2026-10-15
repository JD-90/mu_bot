"""Shared hub between machines (Phase 1/2 of claude/multi-machine-design.md).

One machine (the PC) runs a small HTTP server inside its bot; every bot (that one included) posts
its characters' state every few seconds and gets back everyone else's. The server also hands out
claims (locks) such as "castle 3 at 14:00 is taken by wizzardo", so two machines never pick the
same thing.

Config: hub.json next to this file (git-ignored, one per machine; template hub.example.json):
    {"machine": "pc",  "serve": true,  "url": "http://127.0.0.1:47300", "key": "same-secret-everywhere"}
    {"machine": "mac", "serve": false, "url": "http://192.168.1.50:47300", "key": "same-secret-everywhere"}
No hub.json -> no link at all: the bot runs exactly as before (alone).

Times: the server stamps everything with ITS clock and only sends ages / seconds-left, so the
machines' clocks never have to agree (the PC's clock is 3 h off).

Command line:
    python mu_hub.py serve   run only the server (no bot), for testing
    python mu_hub.py test    read hub.json, contact the hub, print what it knows
Status page in a browser: http://<hub>:47300/?key=<key>

Event switches (Juan 2026-10-15): Blood Castle / Golden Invasion / Treasure Hunt / Big Boss on-off live
at the hub, so switching one on any machine (console key, or the buttons on the status page) switches
it on every machine. The server keeps them in hub_flags.json (git-ignored), so a restart keeps them.
A machine that can't reach the hub keeps its own switches and sends what it changed once it's back.
"""
import html
import json
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HUB_FILE = Path(__file__).with_name("hub.json")
DEFAULT_PORT = 47300
BEAT_EVERY = 3          # s between heartbeats
STALE_SEC = 20          # a machine not heard from for this long: its characters are "unknown"
TIMEOUT = 2.5           # s per request
CLAIM_TTL_MAX = 3 * 3600

# fields of Hub.snapshot() that go to the other machines (plain JSON, no internals)
SHARE_FIELDS = ("online", "paused", "mode", "map", "x", "y", "level", "resets", "hp", "party",
                "step", "spot", "target", "rate", "bc", "bc_away", "boxes", "bag_free",
                "assisted", "leveler", "pair_lead", "gi_hunter", "bc_tier", "bc_role",
                "scouting", "share_with", "boxes5")
TRADE_IDLE_SEC = 30     # a trade session nobody synced for this long is dropped
TRADE_ENDED_KEEP = 20   # an ended one stays visible this long (the other side must see "ended")
SAMPLES_KEEP = 2000     # spot_stats samples the server remembers (other machines fetch the ones they miss)
FLAGS_FILE = Path(__file__).with_name("hub_flags.json")
FLAGS = {"bc": "Blood Castle", "gi": "Golden Invasion", "th": "Treasure Hunt", "bb": "Big Boss"}
MODE_NAMES = {"level": "leveling", "bc": "Blood Castle", "gi": "Golden Invasion", "th": "Treasure Hunt",
              "bb": "Big Boss", "trade": "box trade", "vault": "vault trip", "crystal": "Alquimista",
              "reset": "reset", "shop": "Zen Chests", "paused": "paused", "offline": "offline",
              "start": "starting", "wait": "waiting (bar)"}


def load_config():
    try:
        cfg = json.loads(HUB_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        print(f"hub.json could not be read ({e!r}) -> no hub link")
        return None
    if not isinstance(cfg, dict) or not (cfg.get("url") or cfg.get("serve")):
        return None
    cfg.setdefault("machine", socket.gethostname())
    cfg.setdefault("port", DEFAULT_PORT)
    cfg.setdefault("url", f"http://127.0.0.1:{cfg['port']}")
    cfg["url"] = cfg["url"].rstrip("/")
    return cfg


# ───────────────────────────── server ─────────────────────────────
class HubState:
    def __init__(self):
        self.lock = threading.Lock()
        self.machines = {}      # machine -> {"at": t, "chars": {name: {...}}, "addr": ip}
        self.claims = {}        # key -> {"owner", "machine", "until", "at"}
        self.started = time.time()
        self.epoch = f"{int(self.started)}"   # changes when the server restarts (sample numbers start over)
        self.samples = []       # [(seq, machine, key, levels, minutes)] spot_stats samples from every machine
        self.seq = 0
        self.trades = {}        # lead -> box trade session between machines (see trade())
        self.flags, self.flags_meta, self.flags_ver = {}, {}, 0    # event switches (see set_flags)
        try:
            d = json.loads(FLAGS_FILE.read_text(encoding="utf-8"))
            self.flags = {k: bool(v) for k, v in (d.get("flags") or {}).items() if k in FLAGS}
            self.flags_meta = {k: {"by": m.get("by"), "at": 0} for k, m in (d.get("meta") or {}).items() if k in self.flags}
            self.flags_ver = 1 if self.flags else 0
        except (OSError, ValueError, AttributeError):
            pass

    def set_flags(self, changes, by, adopt=False):
        """Event switches. adopt=True: only the ones nobody has set yet (a machine's startup values).
        Call with self.lock held."""
        now, changed = time.time(), False
        for k, v in (changes or {}).items():
            if k not in FLAGS or v is None or (adopt and k in self.flags):
                continue
            v = bool(v)
            if self.flags.get(k) != v or k not in self.flags:
                self.flags[k] = v
                self.flags_meta[k] = {"by": by, "at": now}
                changed = True
        if changed:
            self.flags_ver += 1
            try:
                FLAGS_FILE.write_text(json.dumps({"flags": self.flags, "meta": {
                    k: {"by": m.get("by")} for k, m in self.flags_meta.items()}}, indent=1), encoding="utf-8")
            except OSError:
                pass
        return changed

    def toggle(self, k, by):
        with self.lock:
            if k in FLAGS:
                self.set_flags({k: not self.flags.get(k, False)}, by)

    def beat(self, machine, chars, addr, samples=None, since=None, epoch=None, flags=None, set_flags=None):
        now = time.time()
        with self.lock:
            self.machines[machine] = {"at": now, "chars": chars or {}, "addr": addr, "flags": flags or {}}
            if set_flags:                       # switched on that machine (console key)
                self.set_flags(set_flags, machine)
            if flags:                           # nobody set these yet: that machine's startup values
                self.set_flags(flags, f"{machine} (startup)", adopt=True)
            for smp in samples or []:
                try:
                    key, levels, minutes = str(smp[0]), float(smp[1]), float(smp[2])
                except (TypeError, ValueError, IndexError):
                    continue
                self.seq += 1
                self.samples.append((self.seq, machine, key, levels, minutes))
            del self.samples[:-SAMPLES_KEEP]
            v = self.view(now)
            if epoch != self.epoch or since is None:
                since = self.seq if since is None else 0   # first contact: only new samples; server restarted: all kept
            v["samples"] = [[q, k, l, m] for q, mc, k, l, m in self.samples if q > since and mc != machine]
            v["seq"], v["epoch"] = self.seq, self.epoch
            return v

    def view(self, now=None):
        now = now or time.time()
        self._expire(now)
        return {
            "machines": {m: {"age": round(now - d["at"], 1), "addr": d["addr"], "chars": d["chars"],
                             "flags": d.get("flags") or {}}
                         for m, d in self.machines.items()},
            "flags": dict(self.flags), "flags_ver": self.flags_ver,
            "flags_meta": {k: {"by": m.get("by"), "ago": round(now - m["at"]) if m.get("at") else None}
                           for k, m in self.flags_meta.items()},
            "claims": {k: {"owner": c["owner"], "machine": c["machine"], "left": round(c["until"] - now)}
                       for k, c in self.claims.items()},
            "trades": {l: {"alt": t["alt"], "machine": t["machine"], "age": round(now - t["at"], 1),
                           "ended": t["ended"], "alt_fields": t["alt_fields"]} for l, t in self.trades.items()},
            "uptime": round(now - self.started),
        }

    def _expire(self, now):
        for k in [k for k, c in self.claims.items() if c["until"] <= now]:
            del self.claims[k]
        for k in [k for k, t in self.trades.items()
                  if now - t["at"] > (TRADE_ENDED_KEEP if t["ended"] else TRADE_IDLE_SEC)]:
            del self.trades[k]

    def trade(self, body, machine):
        """Box trade between characters on different machines. One session per receiver ("lead").
        op start: the giver ("alt") opens it (refused while another giver's session is live);
        op sync:  each side posts the fields it owns and gets the whole session back;
        op end:   either side closes it (ok + why), the other side sees "ended" on its next sync."""
        now = time.time()
        op, lead, alt = body.get("op"), str(body.get("lead")), str(body.get("alt"))
        with self.lock:
            self._expire(now)
            t = self.trades.get(lead)
            if op == "start":
                if t and not t["ended"] and t["alt"] != alt:
                    return {"ok": False, "alt": t["alt"]}
                t = self.trades[lead] = {"alt": alt, "lead": lead, "machine": machine, "at": now,
                                         "ended": None, "alt_fields": dict(body.get("fields") or {}),
                                         "lead_fields": {}}
                return {"ok": True}
            if not t or t["alt"] != alt:
                return {"ok": False, "gone": True}
            if op == "sync":
                side = "alt_fields" if body.get("side") == "alt" else "lead_fields"
                t[side].update(body.get("fields") or {})
                if not t["ended"]:
                    t["at"] = now
                return {"ok": True, "ended": t["ended"], "alt_fields": t["alt_fields"], "lead_fields": t["lead_fields"]}
            if op == "end":
                if not t["ended"]:
                    t["ended"] = {"ok": bool(body.get("ok")), "why": body.get("why"), "by": body.get("side")}
                    t["at"] = now
                return {"ok": True}
        return {"ok": False, "error": "bad op"}

    def claim(self, key, owner, machine, ttl):
        """Take `key` for `owner` (or renew it). Refused while someone else holds it."""
        now = time.time()
        ttl = max(5, min(float(ttl or 60), CLAIM_TTL_MAX))
        with self.lock:
            self._expire(now)
            c = self.claims.get(key)
            if c and c["owner"] != owner:
                return {"ok": False, "owner": c["owner"], "machine": c["machine"], "left": round(c["until"] - now)}
            self.claims[key] = {"owner": owner, "machine": machine, "until": now + ttl, "at": now}
            return {"ok": True, "owner": owner, "machine": machine, "left": round(ttl)}

    def release(self, key, owner):
        with self.lock:
            c = self.claims.get(key)
            if c and (c["owner"] == owner or owner is None):
                del self.claims[key]
                return {"ok": True}
            return {"ok": False, "owner": c["owner"] if c else None}


def _ago(sec):
    if sec is None:
        return "before the last restart"
    sec = int(sec)
    return f"{sec} s ago" if sec < 90 else f"{sec // 60} min ago" if sec < 5400 else f"{sec // 3600} h ago"


def _status_html(v, key=""):
    e = lambda x: html.escape("" if x is None else str(x))
    q = f"?key={urllib.parse.quote(key)}" if key else ""
    machines = sorted(v["machines"].items())
    # event switches
    sw = []
    for k, label in FLAGS.items():
        on = v["flags"].get(k)
        meta = (v.get("flags_meta") or {}).get(k) or {}
        state = "<b class=on>ON</b>" if on else "<b class=off>OFF</b>" if on is not None else "<i>not set</i>"
        local = []
        for m, d in machines:
            lv = (d.get("flags") or {}).get(k)
            ok = lv is None or lv == on
            local.append(f"<td class={'ok' if ok else 'bad'}>{'on' if lv else 'off' if lv is not None else '?'}"
                         f"{'' if ok else ' (not synced yet)'}</td>")
        who = f"{e(meta.get('by'))}, {_ago(meta.get('ago'))}" if meta.get("by") else ""
        btn = (f"<form method=post action='/toggle{q}{'&' if q else '?'}f={k}'>"
               f"<button>{'switch OFF' if on else 'switch ON'}</button></form>")
        sw.append(f"<tr><td>{e(label)}</td><td>{state}</td><td>{who}</td>{''.join(local)}<td>{btn}</td></tr>")
    sw_head = "".join(f"<th>{e(m)} sees" for m, _ in machines)
    # what everyone is doing
    counts = {}
    rows = []
    for m, d in machines:
        for n, c in sorted(d["chars"].items()):
            mode = c.get("mode") or "?"
            if c.get("paused"):
                mode = "paused"
            counts[mode] = counts.get(mode, 0) + 1
            extra = []
            if c.get("bc_tier"):
                extra.append(f"BC{c['bc_tier']} {c.get('bc_role') or ''}".strip())
            if c.get("gi_hunter"):
                extra.append("GI hunter")
            if c.get("assisted"):
                extra.append(f"carried by {c.get('leveler')}")
            if c.get("share_with"):
                extra.append(f"sharing {c['share_with']}'s spot")
            boxes = f"{c.get('boxes') or 0}" + (f" ({c.get('boxes5')} +5)" if c.get("boxes5") else "")
            rows.append("<tr>" + "".join(f"<td>{x}</td>" for x in (
                e(m), f"<b>{e(n)}</b>", f"<span class=m-{e(mode)}>{e(MODE_NAMES.get(mode, mode))}</span>",
                e(", ".join(extra)), e(c.get("level")), e(c.get("resets")), e(c.get("map")),
                f"{e(c.get('x'))},{e(c.get('y'))}", e(c.get("hp")), e(boxes),
                e(c.get("bag_free") if c.get("bag_free") is not None else ""), f"{d['age']} s")) + "</tr>")
    summary = " · ".join(f"{e(MODE_NAMES.get(k, k))}: {n}" for k, n in sorted(counts.items(), key=lambda x: -x[1]))
    claims = "".join(f"<tr><td>{e(k)}</td><td>{e(c['owner'])}</td>"
                     f"<td>{e(c['machine'])}</td><td>{c['left']} s</td></tr>"
                     for k, c in sorted(v["claims"].items()))
    trades = "".join(f"<tr><td>{e(t['alt'])} → {e(l)}</td><td>{e(t['machine'])}</td>"
                     f"<td>{'ended: ' + e((t.get('ended') or {}).get('why')) if t.get('ended') else 'live'}</td>"
                     f"<td>{t['age']} s</td></tr>" for l, t in sorted((v.get("trades") or {}).items()))
    mach = " · ".join(f"{e(m)} ({e(d['addr'])}, heard {d['age']} s ago)" for m, d in machines) or "none"
    return f"""<!doctype html><meta charset=utf-8><meta http-equiv=refresh content=5>
<meta name=viewport content="width=device-width,initial-scale=1"><title>MU hub</title>
<style>body{{font:14px system-ui;margin:16px;background:#111;color:#ddd}}table{{border-collapse:collapse;margin-bottom:18px}}
td,th{{border:1px solid #444;padding:4px 8px;text-align:left}}th{{background:#222}}
.on{{color:#4c4}}.off{{color:#e55}}.bad{{color:#fa3}}.ok{{color:#999}}form{{margin:0}}
button{{font:inherit;padding:3px 10px;cursor:pointer}}
.m-bc,.m-gi,.m-th,.m-bb{{color:#fc5;font-weight:bold}}.m-paused,.m-offline{{color:#888}}.m-level{{color:#8cf}}
.wrap{{overflow-x:auto}}</style>
<h3>Events (shared by every machine)</h3>
<div class=wrap><table><tr><th>event<th>state<th>last switched by{sw_head}<th></tr>{''.join(sw)}</table></div>
<h3>Characters</h3><p>{summary or 'no characters'}</p>
<div class=wrap><table><tr><th>machine<th>name<th>doing<th>details<th>lvl<th>resets<th>map<th>pos<th>hp<th>boxes<th>bag free<th>heard</tr>{''.join(rows)}</table></div>
<h3>Box trades between machines</h3><table><tr><th>who<th>machine<th>state<th>age</tr>{trades}</table>
<h3>Claims</h3><table><tr><th>key<th>owner<th>machine<th>left</tr>{claims}</table>
<p>machines: {mach} · hub up {_ago(v['uptime']).replace(' ago', '')}</p>"""


def make_handler(state, key):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _authorized(self):
            if not key:
                return True
            if self.headers.get("X-Hub-Key") == key:
                return True
            return f"key={key}" in (self.path.split("?", 1) + [""])[1].split("&")

        def _send(self, code, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if not self._authorized():
                return self._send(403, {"error": "bad key"})
            path = self.path.split("?", 1)[0]
            if path == "/status":
                return self._send(200, state.view())
            if path == "/":
                return self._send(200, _status_html(state.view(), key or "").encode(), "text/html; charset=utf-8")
            self._send(404, {"error": "not found"})

        def do_POST(self):
            if not self._authorized():
                return self._send(403, {"error": "bad key"})
            try:
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) or b"{}"
                body = {} if self.path.startswith("/toggle") else json.loads(raw)
            except (ValueError, OSError):
                return self._send(400, {"error": "bad json"})
            path = self.path.split("?", 1)[0]
            if path == "/toggle":                # button on the status page
                qs = urllib.parse.parse_qs((self.path.split("?", 1) + [""])[1])
                state.toggle((qs.get("f") or [""])[0], f"status page ({self.client_address[0]})")
                self.send_response(303)
                self.send_header("Location", "/" + (f"?key={urllib.parse.quote(key)}" if key else ""))
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if path == "/beat":
                return self._send(200, state.beat(str(body.get("machine")), body.get("chars"),
                                                  self.client_address[0], body.get("samples"),
                                                  body.get("since"), body.get("epoch"),
                                                  body.get("flags"), body.get("set_flags")))
            if path == "/claim":
                return self._send(200, state.claim(str(body.get("key")), body.get("owner"),
                                                   body.get("machine"), body.get("ttl")))
            if path == "/trade":
                return self._send(200, state.trade(body, body.get("machine")))
            if path == "/release":
                return self._send(200, state.release(str(body.get("key")), body.get("owner")))
            self._send(404, {"error": "not found"})
    return Handler


def lan_ip():
    """This machine's address on the home network (what the other machine puts in its hub.json)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))      # no packet is sent: only picks the outgoing interface
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "<this PC's LAN IP>"


def start_server(cfg, log=print):
    """Start the hub server in a background thread. Returns the server, or None if it can't bind."""
    state = HubState()
    try:
        srv = ThreadingHTTPServer(("0.0.0.0", int(cfg.get("port") or DEFAULT_PORT)), make_handler(state, cfg.get("key")))
    except OSError as e:
        log(f"hub: server could NOT start on port {cfg.get('port')} ({e!r}) -> only connecting as a client")
        return None
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, name="hub-server", daemon=True).start()
    log(f"hub: server listening -> other machines put \"url\": \"http://{lan_ip()}:{cfg.get('port')}\" in their hub.json "
        f"(status page: that address + /?key=...)")
    return srv


# ───────────────────────────── client ─────────────────────────────
class HubLink:
    """Talks to the hub for this machine's bot. Never raises: when the hub is unreachable every
    call returns None / an empty view and the bot carries on alone."""

    def __init__(self, cfg, snapshot_fn, log=print):
        self.cfg = cfg
        self.machine = cfg["machine"]
        self.url = cfg["url"]
        self.key = cfg.get("key") or ""
        self.snapshot_fn = snapshot_fn
        self.log = log
        self.lock = threading.Lock()
        self.view = {}             # last /beat answer
        self.view_at = 0           # local monotonic time of that answer
        self.ok = None             # None = never tried, True/False = last beat worked
        self.fail_since = None
        self.seen = set()          # remote characters already announced in the log
        self.outbox = []           # spot_stats samples to send: [key, levels, minutes]
        self.since, self.epoch = None, None   # last sample number / server epoch received
        self.on_samples = None     # callback(key, levels, minutes) for samples from other machines
        self.samples_in = 0
        self.flags_fn = None       # () -> this machine's switches {"bc": bool, ...}
        self.on_flags = None       # callback(changes {k: bool}, by) when the hub's switches differ from ours
        self.flags_out = {}        # switched here, not yet confirmed by the hub
        self.flags_ver = None
        self.stop = False
        self.server = start_server(cfg, log) if cfg.get("serve") else None

    # ── http ──
    def _req(self, path, body=None):
        data = None if body is None else json.dumps(body, default=str).encode()
        req = urllib.request.Request(self.url + path, data=data, method="POST" if data is not None else "GET",
                                     headers={"Content-Type": "application/json", "X-Hub-Key": self.key})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.loads(r.read() or b"{}")

    def _call(self, path, body=None):
        try:
            return self._req(path, body)
        except (urllib.error.URLError, OSError, ValueError) as e:
            return {"_error": repr(e)}

    # ── heartbeat thread ──
    def start(self):
        threading.Thread(target=self._loop, name="hub-link", daemon=True).start()
        return self

    def _loop(self):
        while not self.stop:
            try:
                self.beat()
            except Exception as e:      # never let the link kill anything
                self.log(f"hub: beat error {e!r}")
            time.sleep(BEAT_EVERY)

    def local_chars(self):
        out = {}
        for n, i in (self.snapshot_fn() or {}).items():
            out[n] = {k: i.get(k) for k in SHARE_FIELDS if k in i}
        return out

    def beat(self):
        with self.lock:
            out, self.outbox = self.outbox, []
            fout = dict(self.flags_out)
        local = {}
        if self.flags_fn:
            try:
                local = self.flags_fn() or {}
            except Exception:
                local = {}
        r = self._call("/beat", {"machine": self.machine, "chars": self.local_chars(), "samples": out,
                                 "since": self.since, "epoch": self.epoch, "flags": local, "set_flags": fout})
        if "_error" in r or "machines" not in r:
            if self.ok is not False:
                err = r.get("_error") or r.get("error") or "bad answer"
                why = "wrong key in hub.json" if "403" in err else err
                self.log(f"hub: can't reach {self.url} ({why}) "
                         f"-> carrying on alone, retrying every {BEAT_EVERY}s")
                self.fail_since = time.monotonic()
            self.ok = False
            with self.lock:                   # keep the samples for the next try (bounded)
                self.outbox = (out + self.outbox)[-500:]
            return
        if self.ok is not True:
            others = [m for m in r["machines"] if m != self.machine]
            self.log(f"hub: connected to {self.url} as '{self.machine}' (other machines: {', '.join(others) or 'none yet'})")
        self.ok = True
        with self.lock:
            self.view, self.view_at = r, time.monotonic()
            for k, v in fout.items():                 # the hub has them now
                if self.flags_out.get(k) == v:
                    del self.flags_out[k]
            pending = dict(self.flags_out)
        hub_flags = r.get("flags") or {}
        if self.on_flags and hub_flags:
            diff = {k: v for k, v in hub_flags.items() if k in FLAGS and local.get(k) != v and k not in pending}
            if diff:
                meta = r.get("flags_meta") or {}
                by = ", ".join(sorted({str((meta.get(k) or {}).get("by")) for k in diff}))
                try:
                    self.on_flags(diff, by)
                except Exception as e:
                    self.log(f"hub: applying switches {diff}: {e!r}")
        self.flags_ver = r.get("flags_ver")
        self.since, self.epoch = r.get("seq", self.since), r.get("epoch", self.epoch)
        for q, key, levels, minutes in r.get("samples") or []:
            if self.on_samples:
                try:
                    self.on_samples(key, levels, minutes)
                    self.samples_in += 1
                except Exception as e:
                    self.log(f"hub: spot sample {key}: {e!r}")
        for n in self.remote():
            if n not in self.seen:
                self.seen.add(n)
                c = self.remote()[n]
                self.log(f"hub: sees {n} on {c['machine']} (lvl {c.get('level')}, {c.get('mode')})")

    # ── what the bot reads ──
    def connected(self):
        return bool(self.ok) and time.monotonic() - self.view_at < STALE_SEC

    def remote(self):
        """Characters on OTHER machines heard from recently: {name: {...shared fields, machine, age}}."""
        with self.lock:
            v, at = self.view, self.view_at
        if not v or time.monotonic() - at > STALE_SEC:
            return {}
        extra = time.monotonic() - at
        out = {}
        for m, d in (v.get("machines") or {}).items():
            if m == self.machine:
                continue
            age = (d.get("age") or 0) + extra
            if age > STALE_SEC:
                continue
            for n, c in (d.get("chars") or {}).items():
                out[n] = dict(c, machine=m, age=round(age, 1))
        return out

    def summary(self):
        if self.ok is None:
            return "hub: not contacted yet"
        if not self.connected():
            return f"hub: OFFLINE ({self.url})"
        rem = self.remote()
        if not rem:
            return "hub: connected, no other machine online"
        return "hub: " + " | ".join(f"{n}@{c['machine']} lvl {c.get('level')} {c.get('mode')}" + (f" BC{c['bc_tier']}" if c.get("bc_tier") else "")
                                    for n, c in sorted(rem.items()))

    def set_flag(self, k, value):
        """Switched here (console key) -> to the hub with the next heartbeat (every BEAT_EVERY s)."""
        with self.lock:
            self.flags_out[k] = bool(value)

    def send_sample(self, key, levels, minutes):
        """A spot_stats measurement made here -> the other machines add it to theirs."""
        with self.lock:
            self.outbox.append([key, round(levels, 3), round(minutes, 3)])
            del self.outbox[:-500]

    # ── claims ──
    def claim(self, key, owner, ttl=60):
        """{"ok": True/False, "owner", "machine", "left"} or None when the hub is unreachable."""
        r = self._call("/claim", {"key": key, "owner": owner, "machine": self.machine, "ttl": ttl})
        return None if "_error" in r or "ok" not in r else r

    def release(self, key, owner):
        r = self._call("/release", {"key": key, "owner": owner})
        return None if "_error" in r else r

    def trade(self, op, lead, alt, **kw):
        """Box trade session at the hub (see HubState.trade). None when the hub is unreachable."""
        r = self._call("/trade", dict(kw, op=op, lead=lead, alt=alt, machine=self.machine))
        return None if "_error" in r else r

    def trades(self):
        """Live sessions as of the last heartbeat: {lead: {"alt", "machine", "age", "ended", "alt_fields"}}."""
        with self.lock:
            return dict((self.view or {}).get("trades") or {})

    def claims(self):
        with self.lock:
            return dict((self.view or {}).get("claims") or {})


def make_link(snapshot_fn, log=print):
    cfg = load_config()
    if not cfg:
        return None
    return HubLink(cfg, snapshot_fn, log).start()


# ───────────────────────────── command line ─────────────────────────────
def _main(argv):
    cmd = argv[1] if len(argv) > 1 else "test"
    cfg = load_config()
    if cmd == "serve":
        cfg = cfg or {"port": DEFAULT_PORT}
        if not start_server(cfg):
            return 1
        print("Ctrl+C to stop")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            return 0
    if not cfg:
        print("no hub.json here - copy hub.example.json to hub.json and fill it in")
        return 1
    link = HubLink(dict(cfg, serve=False), lambda: {}, print)
    print(f"contacting {link.url} as '{link.machine}' ...")
    r = link._call("/status")
    if "_error" in r:
        print(f"FAILED: {r['_error']}")
        print("check: the PC bot (or 'python mu_hub.py serve') is running, the IP in hub.json is the PC's LAN IP, "
              "Windows Firewall allows Python on private networks, and the VPN allows local network access")
        return 1
    if r.get("error"):
        print(f"FAILED: {r['error']} (the key in hub.json must be the same on both machines)")
        return 1
    print(f"OK - hub up {r.get('uptime')} s")
    for m, d in (r.get("machines") or {}).items():
        print(f"  {m} (heard {d['age']} s ago): {', '.join(d['chars']) or 'no characters'}")
    for k, c in (r.get("claims") or {}).items():
        print(f"  claim {k}: {c['owner']}@{c['machine']} ({c['left']} s left)")
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv))

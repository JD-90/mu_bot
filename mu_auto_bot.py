"""
MU autonomous bot for https://mu.mormon.garden: every character levels, resets and runs
Blood Castle ON ITS OWN. Uses the same accounts.json, ROUTE and spot data as mu_group_bot.py
(which stays as it is; the two bots can never run at the same time).

Leveling (each character, independently):
  * picks its leg of the ROUTE from its OWN level; /reset as soon as it reaches the reset level,
    then buys Zen Chests (same as the group bot).
  * Entering a leg: if another of ours is already farming that leg, it goes to that character's
    spot (the first one there is the "anchor" and decides spot switches). Otherwise it scouts the
    spots alone and picks one.
  * Parties: two of ours on the same leg of the route, same map, within PARTY_MEET_RADIUS tiles
    party up (the higher level invites, only our own invites are accepted). A character with no party member near it
    (PARTY_APART_RADIUS) for PARTY_APART_SEC leaves the party.

Blood Castle (15 minutes before every castle, BC_PLAN_SEC):
  * One character per castle level (BC1..BC7, lower castles first).
  * Already inside a castle's level range -> HOLD: stop farming, buy the cloak, wait at the bar.
    (two of ours in the same range: the lower level one holds, the other climbs)
  * The others CLIMB to the nearest free castle above them that they can reach in time
    (or reset and climb to BC1 when close to the reset level and BC1 is free). Once inside the
    range they stop and hold. Not there 2 minutes before the castle -> skip it, keep leveling.
  * Inside: wait for an invite for BC_WAIT_INVITE_SEC after the first other player shows up;
    no invite -> invite every player in view, and keep inviting new arrivals. Then walk up the
    corridor, destroy the statue, take the weapon and deliver it (or rush back to the
    Archangel when someone else has it), pick up the Box of Kundun +5, back to leveling.

Run:   run_auto.bat           Stop: Ctrl+C in the console window.
Pause: P pauses / resumes EVERY character. Keys 1-5 pause / resume ONE character (order of
       accounts.json). A paused character's window is restored so you can play it; the others
       keep going.
"""
import math
import socket
import sys
import threading
import time
from datetime import datetime

from playwright.sync_api import sync_playwright, Error as PWError

import mu_group_bot as G
import mu_bc as B
from mu_bc import in_bc, dist

# ───────────────────────────── CONFIG ─────────────────────────────
LOG_FILE            = G.BASE_DIR / "mu_auto_bot.log"

# parties while leveling
PARTY_MEET_RADIUS   = 10    # two of ours this close (same map) -> party up
PARTY_APART_RADIUS  = 20    # a party member further than this ...
PARTY_APART_SEC     = 60    # ... for this long (nobody near me) -> I leave the party
PARTY_EVERY         = 2     # seconds between party checks
SEND_AUTOPARTY_OFF  = True  # the group bot turned /autoparty ON for the alts; turn it off once

# spot rules for a character on its own (the group bot's rules for the rest, see mu_group_bot.py)
SOLO_DEATHS_TO_SWITCH = 3   # my deaths at one spot within DEATH_WINDOW_SEC -> switch (not on the best spot)

# Blood Castle
BC_AUTO             = True
BC_TIERS            = B.BC_TIERS        # (castle, min lvl, max lvl): BC1 15-80 ... BC7 331-400
BC_PLAN_SEC         = 900   # make the plan this long before the entry window opens
BC_CLIMB_DEADLINE   = 120   # a climber not in its range this long before the window opens -> skips it
BC_ENTER_MARGIN     = 3     # climbers stop this many levels inside the range (not right at its edge)
BC_DEFAULT_RATE     = 15    # levels/min assumed when a character's recent rate is unknown
BC_RESET_OVERHEAD   = 1.5   # minutes for /reset + Zen Chests + walking out of town
BC_LOW_RATE         = 25    # levels/min right after a reset (Lorencia)
BC_WAIT_INVITE_SEC  = 30    # inside: wait this long for a stranger's invite before inviting ourselves
BC_INVITE_EVERY     = 10    # seconds between invites to the same player
BC_SHOW_WINDOW_AFTER = 2    # entry dialog not found this many times -> restore the window so you can click
# ──────────────────────────────────────────────────────────────────

_log_lock = threading.Lock()


def log(msg, who="auto"):
    line = f"[{datetime.now():%H:%M:%S}] [{who}] {msg}"
    with _log_lock:
        print(line, flush=True)
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")


G.log = log   # Char.log / set_helpers in mu_group_bot write through this (thread-safe, own log file)

# A wider search for the castle buttons than mu_bc's (its dialog id was a guess). Leaf elements
# whose text starts with "Castle N" / "Blood Castle N", ignoring the event countdown rows.
ENTER_BUTTON_JS = """
(tier) => {
  const root = document.getElementById('blood-castle-enter') || document.body;
  const vis = e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const leaves = [...root.querySelectorAll('*')].filter(e => e.childElementCount === 0 && vis(e)
      && !e.closest('.event-row-label') && !(e.parentElement && e.parentElement.querySelector('.event-row-label')));
  const any = leaves.filter(e => /^\\s*(Blood\\s+)?Castle\\s*\\d/i.test(e.textContent || ''));
  if (!any.length) return {dialog: false};
  const re = new RegExp('^\\\\s*(Blood\\\\s+)?Castle\\\\s*' + tier + '(\\\\D|$)', 'i');
  const el = any.find(e => re.test(e.textContent || ''));
  if (!el) return {dialog: true, text: any.map(e => e.textContent).join(' | ').slice(0, 200)};
  const r = el.getBoundingClientRect();
  return {dialog: true, x: r.left + r.width / 2, y: r.top + r.height / 2, label: el.textContent};
}
"""


class SharedStats(G.SpotStats):
    """spot_stats.json shared by all threads. Party rates use the group bot's keys ("Arena2 47,83"),
    solo rates get their own ("Arena2/solo 47,83")."""

    def __init__(self, path):
        super().__init__(path)
        self.lock = threading.Lock()

    def add(self, *a):
        with self.lock:
            super().add(*a)

    def ranked(self, alias, spots):
        """Solo spots nobody has measured yet are ranked by the party data (only for the ORDER;
        the switch rule compares solo rates with solo rates)."""
        if alias.endswith("/solo") and sum(self.rate(alias, s) is not None for s in spots) < 2:
            return super().ranked(alias[:-5], spots)
        return super().ranked(alias, spots)


def stats_alias(alias, in_party):
    return alias if in_party else f"{alias}/solo"


class Hub:
    """What the characters know about each other (each thread only touches its own browser)."""

    def __init__(self, names):
        self.lock = threading.Lock()
        self.names = names
        self.info = {}
        self.stats = SharedStats(G.BASE_DIR / G.STATS_FILE)
        self.reset_level = G.RESET_LEVEL
        self.detecting = False
        self.open_at = None          # next Blood Castle entry window (epoch seconds)
        self.plan = None             # {"key", "open_at", "assign": {name: (tier, role)}, "started", "out"}
        self.paused = set()
        self.stop = False

    def put(self, name, d):
        with self.lock:
            self.info[name] = d

    def snapshot(self):
        with self.lock:
            return {n: dict(i) for n, i in self.info.items()}


class Agent:
    def __init__(self, acc, idx, hub):
        self.acc, self.idx, self.hub = acc, idx, hub
        self.name = acc["character"]
        self.c = None
        self.step_key = None
        self.spot_idx = 0
        self.step_since = 0
        self.spot_since = time.time()
        self.arrived = False
        self.bad_until = {}
        self.samples = []            # (time, level) at the current spot, for the switch rule
        self.stat_point = None
        self.deaths = []
        self.history = []            # (time, level, resets) for the recent levels/min (BC plan)
        self.scouting = False
        self.scout = {}
        self.party_check = 0
        self.alone_since = None
        self.invited = {}
        self.bcs = None              # Blood Castle state while this character is assigned to one
        self.plan_seen = None
        self.paused = False
        self.show_window = False     # keep the window restored (paused / you have to click something)
        self.autoparty_off = False
        self.last_status = 0
        self.mode = "start"
        self.anchor_name = None

    def log(self, msg):
        log(msg, self.name)

    # ── thread ──
    def run(self):
        try:
            with sync_playwright() as pw:
                ctx = pw.chromium.launch_persistent_context(
                    str(G.BASE_DIR / f"browser_profile_{self.acc['user']}"),
                    headless=G.HEADLESS,
                    viewport={"width": G.WINDOW_SIZE[0], "height": G.WINDOW_SIZE[1]},
                    args=["--disable-background-timer-throttling", "--disable-renderer-backgrounding",
                          "--disable-backgrounding-occluded-windows",
                          f"--window-position={40 + self.idx * 50},{40 + self.idx * 50}"])
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                page.goto(G.URL, wait_until="domcontentloaded", timeout=180_000)
                self.c = G.Char(self.acc, page, is_leader=False)
                while not self.hub.stop:
                    try:
                        self.tick()
                    except Exception as e:
                        if "has been closed" in str(e) or page.is_closed():
                            self.log("window was closed -> this character stops (the others keep going)")
                            break
                        self.log(f"error: {e!r}")
                        time.sleep(5)
                    time.sleep(G.TICK)
                try:
                    ctx.close()
                except Exception:
                    pass
        except Exception as e:
            self.log(f"thread stopped: {e!r}")
        self.hub.put(self.name, {"name": self.name, "online": False, "paused": False, "mode": "stopped",
                                 "level": None})

    def ev(self, js, arg=None):
        try:
            return self.c.page.evaluate(js, arg) if arg is not None else self.c.page.evaluate(js)
        except Exception:
            return None

    # ── shared state ──
    def publish(self):
        c, s = self.c, self.c.s
        target = None
        if self.step_key is not None and self.mode == "level":
            _, _, _, m, spots = G.ROUTE[self.step_key]
            target = (m,) + tuple(spots[self.spot_idx])
        self.hub.put(self.name, {
            "name": self.name, "online": bool(c.online), "paused": self.paused, "mode": self.mode,
            "map": s.get("map"), "x": s.get("x"), "y": s.get("y"), "level": s.get("level"),
            "resets": s.get("resets"), "hp": s.get("hp"), "party": list(s.get("party") or []),
            "step": self.step_key, "spot": None if self.scouting else self.spot_idx,
            "scouting": self.scouting, "step_since": self.step_since, "arrived": self.arrived,
            "target": target, "rate": self.recent_rate(),
        })

    def recent_rate(self):
        h = self.history
        if len(h) < 2:
            return None
        t1, l1, r1 = h[-1]
        pts = [p for p in h if p[2] == r1 and t1 - p[0] <= 300]
        t0, l0, _ = pts[0]
        if t1 - t0 < 60:
            return None
        return (l1 - l0) / ((t1 - t0) / 60)

    # ── main tick ──
    def tick(self):
        c, hub = self.c, self.hub
        if c.page.is_closed():
            raise RuntimeError("has been closed")
        want_pause = self.name in hub.paused
        if want_pause != self.paused:
            self.pause() if want_pause else self.resume()
        if self.paused:
            self.mode = "paused"
            c.refresh()
            self.publish()
            return
        c.refresh()
        if c.online:
            c.last_ingame = time.time()
            c.was_online = True
            c.select_tries = 0
            if not c.gfx_done:
                c.set_low_graphics()
            elif G.MINIMIZE_WINDOWS and not c.minimized and not G.HEADLESS and not self.show_window:
                c.set_window("minimized")
        else:
            if c.was_online:
                c.last_ingame = time.time()
                c.was_online = False
            self.mode = "offline"
            self.publish()
            c.handle_offline()
            return
        s = c.s
        now = time.time()
        lvl = s.get("level") or 1
        if not self.history or now - self.history[-1][0] >= 10:
            self.history.append((now, lvl, s.get("resets")))
            self.history = [p for p in self.history if now - p[0] <= 600]
        if SEND_AUTOPARTY_OFF and not self.autoparty_off:
            self.autoparty_off = True
            c.chat("/autoparty off")
        if hub.reset_level is None:
            self.detect_reset_level()
            return
        if BC_AUTO:
            self.read_bc_timer()
        if self.bc_tick():
            self.publish()
            return
        if lvl >= hub.reset_level:
            self.mode = "reset"
            self.publish()
            return self.reset_tick()
        if c.shop_pending:
            self.mode = "shop"
            self.publish()
            if c.shop_tick():
                c.shop_pending = False
            return
        self.mode = "level"
        self.level_tick()
        self.publish()
        if now - self.party_check >= PARTY_EVERY:
            self.party_check = now
            self.party_tick()
        if now - self.last_status > 60:
            self.last_status = now
            self.status()

    def status(self):
        s = self.c.s
        where = "-"
        if self.step_key is not None:
            _, _, alias, _, spots = G.ROUTE[self.step_key]
            where = (f"{alias} scouting" if self.scouting else
                     f"{alias} spot {self.spot_idx + 1}/{len(spots)} {spots[self.spot_idx]}")
            if self.anchor_name:
                where += f" (with {self.anchor_name})"
        r = self.recent_rate()
        bc = f" | BC{self.bcs['tier']} {self.bcs['role']}/{self.bcs['phase']}" if self.bcs else ""
        self.log(f"lvl {s.get('level')} r{s.get('resets')} | {where} | "
                 f"{'%.1f' % r if r is not None else '?'} lvl/min | hp {s.get('hp')}/{s.get('maxHP')} | "
                 f"party {s.get('party')}{bc}")

    def detect_reset_level(self):
        hub = self.hub
        with hub.lock:
            if hub.detecting or hub.reset_level is not None:
                return
            hub.detecting = True
        c = self.c
        try:
            c.chat("/resetinfo")
            time.sleep(2)
            for line in reversed(c.page.evaluate(G.CHAT_LINES_JS)):
                m = G.re.search(r"requires level (\d+)", line)
                if m:
                    hub.reset_level = int(m.group(1))
                    self.log(f"reset level detected: {hub.reset_level}")
                    return
            hub.reset_level = G.ROUTE[-1][1]
            self.log(f"could not read /resetinfo, using {hub.reset_level}")
        finally:
            hub.detecting = False

    def reset_tick(self):
        c = self.c
        now = time.time()
        if now - c.last_reset_try < 20:
            return
        c.last_reset_try = now
        self.leave_party("reset")
        c.set_helper(False)
        if not c.online:
            return
        c.close_windows()
        c.chat("/reset")
        c.last_ingame = time.time()
        time.sleep(3)
        c.check_reset_refused()

    # ── pause ──
    def pause(self):
        self.paused = True
        self.show_window = True
        c = self.c
        self.log(f"=== PAUSED (press {self.idx + 1} again to resume this character) ===")
        try:
            if c.minimized:
                c.set_window("normal")
            if G.HELPERS_OFF_ON_PAUSE and c.refresh().get("helper"):
                c.set_helper(False)
        except Exception as e:
            self.log(f"pause: {e!r}")

    def resume(self):
        self.paused = False
        self.show_window = False
        c = self.c
        now = time.time()
        c.walk_target, c.relocating, c.following = None, True, False
        c.last_follow_check, c.last_warp, c.last_ingame = now, 0, now
        c.select_tries, c.last_reset_try, c.walk_progress_at = 0, 0, now
        if c.shop_pending:
            c.shop_started = now
        self.step_key = None          # re-pick leg and spot from the current level
        self.samples, self.stat_point, self.deaths = [], None, []
        self.history = []
        self.log("=== RESUMED ===")

    # ── leveling ──
    def peers_on_step(self):
        snap = self.hub.snapshot()
        return [i for n, i in snap.items() if n != self.name and i.get("online") and i.get("mode") == "level"
                and i.get("step") == self.step_key and not i.get("paused")]

    def find_anchor(self):
        """The first of ours who came to this leg (decides the spot); None if that's me."""
        earlier = [p for p in self.peers_on_step()
                   if (p.get("step_since", 0), p["name"]) < (self.step_since, self.name)]
        return min(earlier, key=lambda p: (p.get("step_since", 0), p["name"])) if earlier else None

    def level_tick(self):
        c, s = self.c, self.c.s
        lvl = s.get("level") or 1
        key = next((i for i, st in enumerate(G.ROUTE) if st[0] <= lvl < st[1]), len(G.ROUTE) - 1)
        frm, to, alias, map_idx, spots = G.ROUTE[key]
        if key != self.step_key:
            self.step_key, self.step_since = key, time.time()
            self.scouting = False
            self.spot_idx = self.hub.stats.ranked(stats_alias(alias, False), spots)[0]
            self.new_spot()
            anchor = self.find_anchor()
            if anchor:
                self.log(f"{alias}: {anchor['name']} is already here -> going to the same spot")
            elif G.SCOUT_ON_NEW_ZONE and len(spots) > 1:
                self.start_scouting(alias, spots)
        anchor = self.find_anchor()
        self.anchor_name = anchor["name"] if anchor else None
        if anchor:
            if self.scouting:
                self.scouting = False
            if anchor.get("scouting"):
                # he is still looking for a spot: get onto the map and wait
                if s.get("map") != map_idx:
                    c.move_tick((frm, to, alias, map_idx) + tuple(spots[self.spot_idx]))
                else:
                    c.walk_target = None
                return
            if anchor.get("spot") is not None and anchor["spot"] != self.spot_idx:
                self.spot_idx = anchor["spot"]
                self.new_spot()
        step = (frm, to, alias, map_idx) + tuple(spots[self.spot_idx])

        dead = (s.get("hp") or 0) <= 0
        if dead and not c.was_dead:
            self.deaths.append((time.time(), self.name))
            self.log(f"died at {alias} {spots[self.spot_idx]}")
        c.was_dead = dead
        if dead:
            c.walk_target = None
            return

        if self.scouting:
            return self.scout_tick(step, spots)
        follow = None
        if anchor and anchor.get("map") == map_idx and (anchor.get("hp") or 0) > 0 and anchor.get("x") is not None \
                and math.hypot(anchor["x"] - step[4], anchor["y"] - step[5]) <= G.LEASH_RADIUS:
            follow = (anchor["x"], anchor["y"])
        c.move_tick(step, follow=follow)
        self.check_spot(step, spots, is_anchor=anchor is None)

    def new_spot(self):
        self.spot_since = time.time()
        self.samples, self.stat_point, self.deaths = [], None, []
        self.arrived = False

    def start_scouting(self, alias, spots):
        now = time.time()
        order = self.hub.stats.ranked(stats_alias(alias, False), spots)
        self.scouting = True
        self.scout = {"order": order, "i": 0, "seen": {}, "started": now, "arrived_at": None,
                      "best": 999.0, "progress_at": now}
        self.log(f"SCOUTING {alias}: checking {len(spots)} spots for other players")

    def finish_scouting(self, alias, spots, reason):
        sc = self.scout
        if sc["seen"]:
            est = self.hub.stats.estimates(stats_alias(alias, False), spots)
            rank = {i: n for n, i in enumerate(sc["order"])}
            best = max(sc["seen"], key=lambda i: (est[i] / (1 + sc["seen"][i]) if sc["seen"][i] < 999 else -1,
                                                  -rank.get(i, i)))
        else:
            best = sc["order"][0] if sc["order"] else 0
        self.spot_idx = best
        self.scouting = False
        self.new_spot()
        self.c.walk_target = None
        self.log(f"SCOUTING done ({reason}) -> spot {best + 1}/{len(spots)} {spots[best]}")

    def scout_tick(self, step, spots):
        c, s, sc = self.c, self.c.s, self.scout
        now = time.time()
        _, _, alias, map_idx, _, _ = step
        if now - sc["started"] > G.SCOUT_GIVE_UP_SEC:
            return self.finish_scouting(alias, spots, "took too long")
        if sc["i"] >= len(sc["order"]):
            return self.finish_scouting(alias, spots, "all spots occupied")
        if s.get("map") != map_idx:
            if now - c.last_warp > 15:
                c.set_helper(False)
                c.chat(f"/move {alias}")
                c.last_warp = now
            sc["started"], sc["arrived_at"], sc["best"], sc["progress_at"] = now, None, 999.0, now
            return
        si = sc["order"][sc["i"]]
        tx, ty = spots[si]
        d = math.hypot(s["x"] - tx, s["y"] - ty)
        if d > G.ARRIVE_RADIUS:
            c.set_helper(False)
            sc["arrived_at"] = None
            if d < sc["best"] - 0.5:
                sc["best"], sc["progress_at"] = d, now
            elif now - sc["progress_at"] > G.SCOUT_STUCK_SEC:
                self.log(f"scout: can't reach {spots[si]} -> skipping it")
                sc["seen"][si] = 999
                return self.scout_next()
            c.page.evaluate(G.WALK_JS, [tx, ty])
            return
        if sc["arrived_at"] is None:
            sc["arrived_at"] = now
            return
        if now - sc["arrived_at"] < G.SCOUT_LOOK_SEC:
            return
        crowd = self.ev(G.CROWD_JS, [tx, ty, G.CROWD_RADIUS, self.hub.names]) or []
        sc["seen"][si] = len(crowd)
        if not crowd:
            sc["seen"], sc["order"] = {}, [si]
            return self.finish_scouting(alias, spots, "found an empty spot")
        self.log(f"scout: spot {si + 1} ({tx},{ty}) occupied by {', '.join(crowd[:4])}")
        self.scout_next()

    def scout_next(self):
        sc = self.scout
        sc["i"] += 1
        sc["arrived_at"], sc["best"], sc["progress_at"] = None, 999.0, time.time()
        self.c.walk_target = None

    def switch_spot(self, reason, alias, spots, sa):
        now = time.time()
        self.bad_until[(self.step_key, self.spot_idx)] = now + G.BAD_SPOT_COOLDOWN
        order = [i for i in self.hub.stats.ranked(sa, spots) if i != self.spot_idx]
        free = [i for i in order if self.bad_until.get((self.step_key, i), 0) <= now]
        nxt = free[0] if free else min(order, key=lambda i: self.bad_until.get((self.step_key, i), 0))
        self.log(f"SPOT SWITCH ({reason}): {spots[self.spot_idx]} -> {spots[nxt]}")
        self.spot_idx = nxt
        self.new_spot()

    def check_spot(self, step, spots, is_anchor):
        """Records this spot's levels/min; the anchor also decides when to switch spots."""
        now = time.time()
        s = self.c.s
        _, _, alias, map_idx, tx, ty = step
        d = math.hypot(s["x"] - tx, s["y"] - ty) if s.get("map") == map_idx else 999
        if d <= G.ARRIVE_RADIUS + 2:
            self.arrived = True
        lvl = s.get("level") or 1
        if not (self.arrived and d <= G.LEASH_RADIUS):
            self.spot_since = now
            self.samples, self.stat_point = [], None
            return
        in_party = len(s.get("party") or []) > 1
        sa = stats_alias(alias, in_party)
        if now - self.spot_since >= 60:
            if self.stat_point is None or self.stat_point[2] != in_party:
                self.stat_point = (now, lvl, in_party)
            elif now - self.stat_point[0] >= 60:
                t0, l0, _ = self.stat_point
                if lvl >= l0:
                    self.hub.stats.add(sa, (tx, ty), lvl - l0, (now - t0) / 60)
                self.stat_point = (now, lvl, in_party)
        if not is_anchor or len(spots) < 2:
            return
        ranked = self.hub.stats.ranked(sa, spots)
        is_best = ranked[0] == self.spot_idx
        self.deaths = [(t, n) for t, n in self.deaths if now - t < G.DEATH_WINDOW_SEC]
        if len(self.deaths) >= SOLO_DEATHS_TO_SWITCH and not (G.DEATH_KEEP_BEST and is_best):
            return self.switch_spot(f"died {len(self.deaths)}x in {G.DEATH_WINDOW_SEC // 60} min", alias, spots, sa)
        self.samples.append((now, lvl))
        self.samples = [p for p in self.samples if now - p[0] <= G.RATE_WINDOW_SEC]
        if now - self.spot_since < G.SETTLE_SEC or now - self.samples[0][0] < G.RATE_WINDOW_SEC * 0.9:
            return
        rate = (lvl - self.samples[0][1]) / ((now - self.samples[0][0]) / 60)
        others = [self.hub.stats.rate(sa, sp) for i, sp in enumerate(spots) if i != self.spot_idx]
        others = [r for r in others if r is not None]
        best_other = max(others) if others else 0.0
        if rate <= 0 or (best_other > 0 and rate < G.LOW_RATE_RATIO * best_other):
            return self.switch_spot(f"{rate:.1f} lvl/min vs {best_other:.1f} known elsewhere", alias, spots, sa)
        best = ranked[0]
        if (best != self.spot_idx and now - self.spot_since >= G.RECHECK_MAIN_SEC
                and self.bad_until.get((self.step_key, best), 0) <= now):
            self.log(f"SPOT SWITCH (re-checking best spot): {spots[self.spot_idx]} -> {spots[best]}")
            self.spot_idx = best
            self.new_spot()

    # ── parties while leveling ──
    def leave_party(self, why):
        party = self.c.s.get("party") or []
        if len(party) > 1:
            self.log(f"leaving party {party} ({why})")
            self.ev("() => window.__social.partyLeave()")
            self.alone_since = None

    def party_tick(self):
        c, s = self.c, self.c.s
        now = time.time()
        party = s.get("party") or []
        ours = set(self.hub.names)
        snap = self.hub.snapshot()
        me = snap.get(self.name) or {}

        # invites: accept only our own characters, and only when not in a party
        if s.get("partyRequest"):
            text = s.get("partyRequestText", "")
            if len(party) <= 1 and any(f'"{n}"' in text for n in ours if n != self.name):
                self.log("accepting party invite")
                self.ev("() => window.__social.partyRespond(true)")
            else:
                self.ev("() => window.__social.partyRespond(false)")

        if len(party) > 1:
            if any(n not in ours for n in party):
                return self.leave_party("a stranger is in it")
            near = False
            for n in party:
                o = snap.get(n)
                if n == self.name or not o or not o.get("online"):
                    continue
                if (o.get("hp") or 0) <= 0:
                    near = True        # dead / respawning: give him time to walk back
                elif o.get("map") == s.get("map") and o.get("x") is not None and \
                        math.hypot(o["x"] - s["x"], o["y"] - s["y"]) <= PARTY_APART_RADIUS:
                    near = True
                elif o.get("mode") == "level" and o.get("target") and o.get("target") == me.get("target"):
                    near = True        # walking back to the same spot
            if near:
                self.alone_since = None
            else:
                self.alone_since = self.alone_since or now
                if now - self.alone_since >= PARTY_APART_SEC:
                    self.leave_party(f"nobody near for {PARTY_APART_SEC}s")
                    return
        # invite: I lead my party (or have none) and outrank the one I'm inviting
        if len(party) >= 5 or (len(party) > 1 and party[0] != self.name):
            return
        my_rank = (len(party) > 1, s.get("level") or 0, self.name)
        for n, o in snap.items():
            if n == self.name or n in party or not o.get("online") or o.get("paused") or o.get("mode") != "level":
                continue
            if o.get("party") and len(o["party"]) > 1:
                continue
            if o.get("step") != self.step_key:
                continue           # different leg of the route: we won't farm together
            if o.get("map") != s.get("map") or o.get("x") is None or \
                    math.hypot(o["x"] - s["x"], o["y"] - s["y"]) > PARTY_MEET_RADIUS:
                continue
            if my_rank <= (False, o.get("level") or 0, n):
                continue           # he invites me instead
            if now - self.invited.get(n, 0) < G.INVITE_EVERY:
                continue
            self.invited[n] = now
            res = self.ev(G.INVITE_JS, n)
            self.log(f"party invite {n}: {res}")

    # ── Blood Castle ──
    def read_bc_timer(self):
        now = time.time()
        if now - getattr(self, "_timer_at", 0) < 10:
            return
        self._timer_at = now
        self.ev(B.BC_HOOK_JS)
        t = self.ev(B.BC_TIMER_JS)
        if not t or t.get("open"):
            # no countdown: the entry window is open right now (castles open on the hour).
            # Without this, a bot started inside the 5-minute window never knows a castle is open.
            since = now % 3600
            o = self.hub.open_at
            if since <= B.BC_DOOR_CLOSE_SEC and (o is None or not (now - B.BC_DOOR_CLOSE_SEC - 60 <= o <= now)):
                self.hub.open_at = now - since
                self.log(f"BC: entry window is open ({int(since)}s ago, timer says {t!r}) -> planning entries")
            return
        secs = t["secs"]
        since = 3600 - secs
        open_at = now - since if since <= B.BC_DOOR_CLOSE_SEC else now + secs
        self.hub.open_at = open_at

    def bc_tick(self):
        """True while Blood Castle takes over this character (leveling paused)."""
        if not BC_AUTO:
            return False
        plan = self.hub.plan
        if self.bcs is None and plan:
            with self.hub.lock:
                a = plan["assign"].get(self.name)
                if a and self.name not in plan["started"]:
                    plan["started"].add(self.name)
                else:
                    a = None
            if a:
                tier, role = a
                self.bcs = {"tier": tier, "role": role, "open_at": plan["open_at"],
                            "phase": "prep" if role == "hold" else "climb",
                            "cloak": False, "talk_at": 0, "no_dialog": 0, "entered": False,
                            "burst_until": 0, "tried": set(), "box": False, "box_try": None,
                            "first_stranger": None, "invited": {}, "end_at": None, "rush": False}
                self.ev("() => { window.__bcState = null; }")
        b = self.bcs
        if b is None:
            return False
        try:
            return self.bc_run(b)
        except Exception as e:
            self.log(f"BC error: {e!r}")
            return True

    def bc_done(self, why):
        self.log(f"BLOOD CASTLE {self.bcs['tier']}: {why} -> back to leveling")
        plan = self.hub.plan
        if plan and not self.bcs.get("entered"):
            with self.hub.lock:      # never got in: free the castle so someone else can take it
                plan["assign"].pop(self.name, None)
                plan["out"].add(self.name)
        self.bcs = None
        self.step_key = None
        self.c.walk_target = None
        self.c.spot = None
        if self.show_window and not self.paused:
            self.show_window = False

    def bc_run(self, b):
        c, s = self.c, self.c.s
        now = time.time()
        since_open = now - b["open_at"]
        tier = b["tier"]
        _, lo, hi = BC_TIERS[tier - 1]
        lvl = s.get("level") or 0
        if b["phase"] == "climb":
            if b["role"] == "reset" and (s.get("resets") or 0) == b.setdefault("resets0", s.get("resets") or 0):
                pass              # not reset yet: the level is still the old one
            elif min(lo + BC_ENTER_MARGIN, hi) <= lvl <= hi:
                self.log(f"BC{tier}: level {lvl} is in range ({lo}-{hi}) -> stop leveling, holding")
                b["phase"] = "prep"
            elif lvl > hi:
                self.bc_done(f"level {lvl} went past the range ({lo}-{hi})")
                return False
            if b["phase"] == "prep":
                pass
            elif since_open > -BC_CLIMB_DEADLINE:
                self.bc_done(f"did not reach level {lo + BC_ENTER_MARGIN} in time (level {lvl})")
                return False
            else:
                return False      # keep leveling (and resetting) toward the range
        self.mode = "bc"
        if b["phase"] == "prep":
            if in_bc(s.get("map")):
                b["phase"], b["entered"] = "inside", True
                self.log(f"BC{tier}: inside the castle (map {s.get('map')})")
                if self.show_window and not self.paused:
                    self.show_window = False
            elif since_open > B.BC_DOOR_CLOSE_SEC:
                self.bc_done("missed the door")
                return False
            elif not (lo <= lvl <= hi):
                self.bc_done(f"level {lvl} is out of range {lo}-{hi}")
                return False
            else:
                if (s.get("hp") or 0) <= 0:
                    return True
                self.leave_party("going to Blood Castle")
                self.prepare_and_enter(b, since_open)
                return True
        if b["phase"] == "inside":
            return self.inside(b, since_open)
        if b["phase"] == "end":
            return self.end_phase(b, now - b["end_at"])
        return True

    def go(self, map_idx, alias, x, y, radius=2):
        c, s = self.c, self.c.s
        if s.get("map") != map_idx:
            if alias and time.time() - c.last_warp > 15:
                c.set_helper(False)
                c.close_windows()
                c.chat(f"/move {alias}")
                c.last_warp = time.time()
                c.walk_target = None
            return False
        d = dist(s, x, y)
        if d <= radius:
            c.walk_target = None
            return True
        c.set_helper(False)
        if c.walk_target != (x, y):
            c.walk_target, c.walk_best, c.walk_progress_at = (x, y), d, time.time()
        if d < c.walk_best - 0.5:
            c.walk_best, c.walk_progress_at = d, time.time()
        elif time.time() - c.walk_progress_at > 45 and not in_bc(map_idx):
            self.log("stuck walking -> re-warping")
            c.chat(f"/move {alias}")
            c.last_warp, c.walk_target = time.time(), None
            return False
        self.ev(B.WALK_JS, [x, y])
        return False

    def inv(self, item):
        g, n, l = (item + (None,))[:3]
        return self.ev(B.INV_COUNT_JS, [g, n, l]) or 0

    def cloak_count(self, tier):
        try:
            return self.c.page.evaluate(B.INV_COUNT_JS, [B.CLOAK[0], B.CLOAK[1], tier]) or 0
        except Exception:
            return None

    def buy_cloak(self, tier):
        c = self.c
        item = (B.CLOAK[0], B.CLOAK[1], tier)
        before = self.inv(item)
        if before:
            return True
        found = self.ev("""(name) => {
          const e = window.__world.netObjsQuery.entities.find(e =>
              e.npcType !== undefined && (e.objectNameInWorld || '').trim() === name);
          if (!e) return false;
          window.__store.talkToNpc({netId: e.netId, name: e.objectNameInWorld, npcType: e.npcType});
          return true; }""", B.BAR_NPC)
        if not found:
            self.log(f"BC: {B.BAR_NPC} not found")
            return False
        slot = None
        for _ in range(10):
            time.sleep(0.5)
            slot = self.ev(B.BUY_JS, list(item))
            if slot is not None:
                break
        ok = False
        if slot is not None and slot >= 0:
            for _ in range(10):
                time.sleep(0.5)
                if self.inv(item) > before:
                    ok = True
                    break
        self.ev("() => { window.__store.closeNpcShop(); window.__store.inventoryEnabled = false; }")
        self.log(f"BC: cloak +{tier} {'bought' if ok else 'NOT bought (slot %s)' % slot}")
        return ok

    def prepare_and_enter(self, b, since_open):
        c = self.c
        tier = b["tier"]
        bar = (B.BAR_SPOT[0], "Lorencia", B.BAR_SPOT[1], B.BAR_SPOT[2])
        if not b["cloak"]:
            if not self.go(*bar):
                return
            if self.buy_cloak(tier):
                b["cloak"] = True
            else:
                b["fails"] = b.get("fails", 0) + 1
                if b["fails"] >= 3:
                    self.bc_done("could not buy a cloak")
            return
        have = self.cloak_count(tier)
        if have == 0 and time.time() - b["talk_at"] < 10:
            return   # just talked: cloak consumed, warp into the castle loading
        if have == 0:
            left = B.BC_DOOR_CLOSE_SEC - since_open
            b["rebuys"] = b.get("rebuys", 0) + 1
            if left >= B.BC_REBUY_NEEDS_SEC and b["rebuys"] <= B.BC_MAX_REBUYS:
                self.log(f"BC: no cloak +{tier} in the bag -> back to the bar for another")
                b["cloak"] = False
            else:
                self.bc_done(f"no cloak +{tier} and no time left to buy one")
            return
        if since_open < -75:
            self.go(*bar)            # wait at the bar (safe zone, no EXP)
            return
        m, x, y = B.SPIRIT_SPOT
        if not self.go(m, "Devias", x, y, radius=1):
            return
        if since_open < 0:
            return
        if time.time() - b["talk_at"] <= 5:
            return
        b["talk_at"] = time.time()
        btn = self.ev(ENTER_BUTTON_JS, tier) or {}
        if not btn.get("dialog"):
            c.close_windows()
            self.log("BC: talking to Spirit of Archangel")
            self.ev(B.TALK_NPC_JS, B.SPIRIT_NPC)
            for _ in range(8):
                time.sleep(0.4)
                btn = self.ev(ENTER_BUTTON_JS, tier) or {}
                if btn.get("dialog"):
                    break
        if not btn.get("dialog") or "x" not in btn:
            b["no_dialog"] += 1
            self.log(f"BC: castle {tier} button not found ({btn.get('text', 'no dialog')!r})")
            if b["no_dialog"] >= BC_SHOW_WINDOW_AFTER and not self.show_window:
                self.show_window = True
                if c.minimized:
                    c.set_window("normal")
                self.log(f"BC: >>> please click 'Castle {tier}' in {self.name}'s window <<<")
            return
        self.log(f"BC: clicking '{btn.get('label')}'")
        try:
            c.page.mouse.click(btn["x"], btn["y"])
        except Exception as e:
            self.log(f"BC: click failed {e!r}")

    def walk_in_castle(self, b, x, y, radius=2):
        c, s = self.c, self.c.s
        now = time.time()
        d = dist(s, x, y)
        if d <= radius:
            if s.get("helper") and now >= b["burst_until"]:
                c.set_helper(False)
            c.walk_target = None
            return True
        if now < b["burst_until"]:
            c.set_helper(True)
            return False
        if s.get("helper"):
            c.set_helper(False)
            c.walk_target = None
        if c.walk_target != (x, y):
            c.walk_target, c.walk_best, c.walk_progress_at = (x, y), d, now
        if d < c.walk_best - 0.5:
            c.walk_best, c.walk_progress_at = d, now
        elif now - c.walk_progress_at > B.STUCK_SEC:
            b["stuck_n"] = b.get("stuck_n", 0) + 1
            if b["stuck_n"] % 5 == 1:
                self.log(f"BC: stuck at ({s.get('x')},{s.get('y')}), {d:.0f} tiles from ({x},{y}) -> helper burst")
            b["burst_until"] = now + B.HELPER_BURST
            c.walk_target = None
            c.set_helper(True)
            return False
        if not self.ev(B.WALK_JS, [int(x), int(y)]):
            for ox, oy in ((1, 0), (-1, 0), (0, -1), (1, -1), (-1, -1), (2, 0), (0, -2), (2, -2),
                           (0, 1), (1, 1), (-1, 1)):
                if self.ev(B.WALK_JS, [int(x) + ox, int(y) + oy]):
                    break
        return False

    def bc_party(self, b):
        """Wait BC_WAIT_INVITE_SEC for a stranger's invite once someone shows up; then invite
        everyone in view (and keep inviting new arrivals) while we lead and have room."""
        s = self.c.s
        now = time.time()
        party = s.get("party") or []
        if s.get("partyRequest"):
            if len(party) <= 1:
                self.log(f"BC: accepting party invite {s.get('partyRequestText', '')[:60]}")
                self.ev("() => window.__social.partyRespond(true)")
            else:
                self.ev("() => window.__social.partyRespond(false)")
            return
        players = (self.ev(B.SCAN_JS, [self.hub.names, B.CLEAR_RADIUS, 0, 0, None]) or {}).get("players", [])
        if players and b["first_stranger"] is None:
            b["first_stranger"] = now
            self.log(f"BC: other players here ({', '.join(players[:4])}) -> waiting {BC_WAIT_INVITE_SEC}s for an invite")
        if b["first_stranger"] is None or now - b["first_stranger"] < BC_WAIT_INVITE_SEC:
            return
        if len(party) > 1 and party[0] != self.name:
            return               # in someone else's party: they invite
        if len(party) >= 5:
            return
        if not b.get("inviting"):
            b["inviting"] = True
            self.log("BC: no invite came -> inviting the players here myself")
        for n in players:
            if n in party or now - b["invited"].get(n, 0) < BC_INVITE_EVERY:
                continue
            b["invited"][n] = now
            res = self.ev(B.INVITE_BY_NAME_JS, n)
            self.log(f"BC: party invite {n}: {res}")

    def inside(self, b, since_open):
        c, s = self.c, self.c.s
        st = self.ev(B.BC_STATE_JS)
        if st and st.get("t", 0) < b["open_at"] * 1000 - 60_000:
            st = None
        if b["end_at"] is None and ((st and st.get("st") == 2) or
                                    (not in_bc(s.get("map")) and since_open > B.BC_DOOR_CLOSE_SEC)):
            b["end_at"], b["phase"] = time.time(), "end"
            self.log(f"BC{b['tier']}: castle finished -> picking up boxes")
            return True
        if not in_bc(s.get("map")):
            # thrown out / died before the castle started: try to get back in if there's time
            b["phase"] = "prep"
            return True
        if (s.get("hp") or 0) <= 0:
            return True
        self.bc_party(b)
        has_weapon = self.inv(B.WEAPON) > 0
        if has_weapon:
            if not self.walk_in_castle(b, *B.ARCHANGEL, radius=2):
                return True
            if time.time() - b["talk_at"] > 3:
                b["talk_at"] = time.time()
                c.set_helper(False)
                self.log("BC: delivering the weapon to the Archangel")
                self.ev(B.TALK_NPC_JS, B.ARCHANGEL_NPC)
            return True
        holder = st.get("own") if st else 255
        if holder not in (None, 255) and not b["rush"]:
            b["rush"] = True
            self.log("BC: another player has the weapon -> back to the Archangel")
        if b["rush"]:
            self.walk_in_castle(b, *B.ARCHANGEL, radius=2)
            return True
        if not st or st.get("st") not in (1, 4):
            self.go(s.get("map"), "", *B.BC_START, radius=2)   # waiting for the castle to start
            return True
        weapon = (self.ev(B.SCAN_JS, [self.hub.names, B.CLEAR_RADIUS, B.WEAPON[0], B.WEAPON[1], None])
                  or {}).get("items", [])
        if weapon:
            if time.time() - b.get("pick_at", 0) > 2:
                b["pick_at"] = time.time()
                c.set_helper(False)
                self.log("BC: picking up the Archangel's weapon")
                self.ev(B.PICKUP_JS, weapon[0]["id"])
            return True
        tx, ty = B.STATUE
        if dist(s, tx, ty) <= B.STATUE_RADIUS:
            c.set_helper(True)
            return True
        self.walk_in_castle(b, tx, ty, radius=B.STATUE_RADIUS)
        return True

    def end_phase(self, b, since_end):
        c, s = self.c, self.c.s
        if in_bc(s.get("map")) and not b["box"] and since_end < B.END_WAIT_SEC:
            if not self.go(s.get("map"), "", *B.ARCHANGEL, radius=4) and since_end < 20:
                return True
            now = time.time()
            if b["box_try"]:
                bid, t0, n0 = b["box_try"]
                if self.inv(B.BOX) > n0:
                    b["box"] = True
                    self.log("BC: got a Box of Kundun +5")
                    return True
                if now - t0 < B.BOX_TRY_SEC:
                    return True
                b["tried"].add(bid)
                b["box_try"] = None
            boxes = [it for it in (self.ev(B.SCAN_JS, [self.hub.names, 30] + list(B.BOX)) or {}).get("items", [])
                     if it["id"] not in b["tried"]]
            if boxes:
                b["box_try"] = (boxes[0]["id"], now, self.inv(B.BOX))
                self.ev(B.PICKUP_JS, boxes[0]["id"])
                return True
            if since_end < 25:
                return True        # boxes may still be dropping
        if in_bc(s.get("map")):
            if time.time() - c.last_warp > 15:
                c.chat("/move Lorencia")
                c.last_warp = time.time()
            return True
        party = s.get("party") or []
        if len(party) > 1:
            self.log(f"BC: leaving party {party}")
            self.ev("() => window.__social.partyLeave()")
        self.bc_done("box +5: " + ("yes" if b["box"] else "no"))
        return False


# ── the plan: which character goes to which castle ──
def build_plan(hub, open_at, taken=None, skip=()):
    """taken: {tier: name} already assigned; skip: names that are assigned or already had their shot."""
    now = time.time()
    snap = hub.snapshot()
    avail = {n: i for n, i in snap.items()
             if i.get("online") and not i.get("paused") and i.get("level") and n not in skip}
    rl = hub.reset_level or G.ROUTE[-1][1]
    left_min = (open_at - BC_CLIMB_DEADLINE - now) / 60
    taken, assign, why = dict(taken or {}), {}, {}
    for t, lo, hi in BC_TIERS:                       # holders: already in a castle's range
        inside = sorted((n for n, i in avail.items() if lo <= i["level"] <= hi),
                        key=lambda n: (avail[n]["level"], n))
        if inside and t not in taken:
            taken[t] = inside[0]
            assign[inside[0]] = (t, "hold")
            why[inside[0]] = f"lvl {avail[inside[0]]['level']}"
    # climbers: nearest free castle above them they can reach in time (lowest levels pick first)
    for n in sorted((n for n in avail if n not in assign), key=lambda n: (avail[n]["level"], n)):
        lvl = avail[n]["level"]
        rate = max(avail[n].get("rate") or BC_DEFAULT_RATE, 1.0)
        for t, lo, hi in BC_TIERS:
            if t in taken or lo <= lvl:
                continue
            need = lo + BC_ENTER_MARGIN - lvl
            if need / rate <= left_min:
                taken[t] = n
                assign[n] = (t, "climb")
                why[n] = f"lvl {lvl} -> {lo + BC_ENTER_MARGIN}, ~{need / rate:.0f} min"
            break   # only the NEAREST free castle above (the next ones are even further)
    # nobody in BC1: the leftover closest to the reset level resets and climbs to it
    if 1 not in taken:
        lo1 = BC_TIERS[0][1]
        best = None
        for n in (n for n in avail if n not in assign):
            lvl = avail[n]["level"]
            rate = max(avail[n].get("rate") or BC_DEFAULT_RATE, 1.0)
            mins = max(rl - lvl, 0) / rate + BC_RESET_OVERHEAD + (lo1 + BC_ENTER_MARGIN) / BC_LOW_RATE
            if mins <= left_min and (best is None or mins < best[0]):
                best = (mins, n, lvl)
        if best:
            mins, n, lvl = best
            taken[1] = n
            assign[n] = (1, "reset")
            why[n] = f"lvl {lvl} -> reset -> {lo1 + BC_ENTER_MARGIN}, ~{mins:.0f} min"
    return assign, why


BC_REPLAN_EVERY = 20   # seconds: re-check characters that had no castle (they may have levelled into one)


def coordinator(hub, agents):
    now = time.time()
    if not BC_AUTO or hub.open_at is None:
        return
    until = hub.open_at - now
    key = round(hub.open_at / 600)
    if until > BC_PLAN_SEC or until < -(B.BC_DOOR_CLOSE_SEC - B.BC_ENTRY_NEEDS_SEC):
        return
    plan = hub.plan
    if plan and plan["key"] == key:
        if now - plan["checked"] < BC_REPLAN_EVERY:
            return
        plan["checked"] = now
        with hub.lock:
            taken = {t: n for n, (t, r) in plan["assign"].items()}
            skip = set(plan["assign"]) | plan["started"] | plan["out"]
        if len(taken) >= len(BC_TIERS):
            return
        assign, why = build_plan(hub, hub.open_at, taken, skip)
        if not assign:
            return
        with hub.lock:
            plan["assign"].update(assign)
        log("BLOOD CASTLE PLAN update: " + " | ".join(
            f"{n}: BC{t} {r} ({why[n]})" for n, (t, r) in sorted(assign.items(), key=lambda kv: kv[1][0])))
        return
    if any(a.bcs for a in agents):
        return       # still busy with the previous castle
    assign, why = build_plan(hub, hub.open_at)
    hub.plan = {"key": key, "open_at": hub.open_at, "assign": assign,
                "started": set(), "out": set(), "checked": now}
    opens = datetime.fromtimestamp(hub.open_at).strftime("%H:%M")
    lines = [f"{n}: BC{t} {r} ({why[n]})" for n, (t, r) in sorted(assign.items(), key=lambda kv: kv[1][0])]
    rest = sorted(n for n, i in hub.snapshot().items() if n not in assign)
    log(f"BLOOD CASTLE PLAN (opens {opens}): " + " | ".join(lines or ["nobody"])
        + (f" | keep leveling (re-checked every {BC_REPLAN_EVERY}s): {', '.join(rest)}" if rest else ""))


def read_keys(hub, agents):
    """P = pause/resume everyone, 1..5 = one character. A PAUSE file pauses everyone."""
    if sys.platform == "win32":
        try:
            import msvcrt
            while msvcrt.kbhit():
                k = msvcrt.getwch().lower()
                if k == G.PAUSE_KEY:
                    if all(a.name in hub.paused for a in agents):
                        hub.paused.clear()
                        log("=== RESUMING EVERYONE ===")
                    else:
                        hub.paused.update(a.name for a in agents)
                        log("=== PAUSING EVERYONE (P again to resume) ===")
                elif k.isdigit() and 1 <= int(k) <= len(agents):
                    n = agents[int(k) - 1].name
                    if n in hub.paused:
                        hub.paused.discard(n)
                    else:
                        hub.paused.add(n)
        except Exception:
            pass
    flag = G.BASE_DIR / G.PAUSE_FILE
    if flag.exists():
        if not getattr(read_keys, "file_paused", False):
            read_keys.file_paused = True
            hub.paused.update(a.name for a in agents)
            log("=== PAUSE file found: everyone paused (delete it to resume) ===")
    elif getattr(read_keys, "file_paused", False):
        read_keys.file_paused = False
        hub.paused.clear()
        log("=== PAUSE file removed: resuming everyone ===")


def main():
    G.disable_console_quickedit()
    lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        lock.bind(("127.0.0.1", 47291))   # same lock as mu_group_bot.py: never both at once
    except OSError:
        print("A MU bot (group or auto) is already running in another window. Close that one first.")
        input("Press Enter to exit...")
        return
    log("=== MU auto bot starting ===")
    names = [a["character"] for a in G.ACCOUNTS]
    hub = Hub(names)
    agents = [Agent(acc, i, hub) for i, acc in enumerate(G.ACCOUNTS)]
    threads = []
    for a in agents:
        t = threading.Thread(target=a.run, name=a.name, daemon=True)
        t.start()
        threads.append(t)
        time.sleep(3)    # stagger the browser launches
    log("Keys in this window: P = pause/resume everyone, "
        + ", ".join(f"{i + 1} = {a.name}" for i, a in enumerate(agents)) + " (pause/resume one)")
    last_summary = 0
    try:
        while any(t.is_alive() for t in threads):
            read_keys(hub, agents)
            try:
                coordinator(hub, agents)
            except Exception as e:
                log(f"plan error: {e!r}")
            if time.time() - last_summary > 300:
                last_summary = time.time()
                snap = hub.snapshot()
                log("summary: " + " | ".join(
                    f"{n} lvl {i.get('level')} {i.get('mode')}" for n, i in snap.items()))
            time.sleep(0.3)
    except KeyboardInterrupt:
        log("stopped by user")
    finally:
        hub.stop = True
        for t in threads:
            t.join(timeout=10)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)

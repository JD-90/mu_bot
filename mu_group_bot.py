"""
MU group reset bot for https://mu.mormon.garden (OpenMUCB browser client).

Runs 3 characters at once (one browser window each), in one party, always together:
  * The group's zone/spot is chosen by the LOWEST level in the group, so nobody moves
    on to the next zone until everyone has reached the threshold (100, 200, 300...).
  * The leader invites the others into a party; the others accept automatically.
  * When every character has reached the reset level, all of them /reset together.
  * Death: that character respawns, warps back to the group's zone and walks to the spot.
  * Disconnect: that window reloads, logs back in and selects its character.

Run:  run_group.bat        Stop: Ctrl+C in the console window.
Pause: press P in the console window -> the bot stops doing anything and you can play the
       characters yourself in the open windows. Press P again to hand control back to the bot.
"""
import json
import math
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright, Error as PWError
from mu_bc import BloodCastle
from mu_recorder import Recorder

# ───────────────────────────── CONFIG ─────────────────────────────
URL    = "https://mu.mormon.garden/"
SERVER    = "Servidor Fast"       # channel on the server list (was "Server 1-1"); picked by PICK_SERVER_JS
PICK_SERVER_JS = r"""() => {
  // Server list: the channel buttons no longer carry their name as DOM text (drawn as an image), so take the
  // clickable entries of .server-sel-win that aren't the server-group button ("Mu Garden").
  const w = document.querySelector('.server-sel-win');
  if (!w) return null;
  const btns = [...w.querySelectorAll('div')].filter(e => e.onclick && !/Mu Garden/i.test(e.textContent || ''));
  const want = btns.find(e => /Servidor Fast|Servidor Final|Server 1-1/i.test(e.textContent || '')) || btns[0];
  if (!want) return null;
  document.querySelectorAll('[data-bot-server]').forEach(e => e.removeAttribute('data-bot-server'));
  want.setAttribute('data-bot-server', '1');
  return (want.textContent || '').trim() || 'channel #' + (btns.indexOf(want) + 1);
}"""


# Accounts live in accounts.json next to this script (not committed to git).
# Copy accounts.example.json to accounts.json and fill in your logins.
_ACC_FILE = Path(__file__).with_name("accounts.json")
if not _ACC_FILE.exists():
    sys.exit("accounts.json not found - copy accounts.example.json to accounts.json and fill it in.")
# First entry is the party leader.
# Entries without "user"/"character" are settings, not accounts, e.g. {"anthropic_api_key": "sk-ant-..."}.
_ACC_RAW = json.loads(_ACC_FILE.read_text(encoding="utf-8-sig"))
ACCOUNTS = [a for a in _ACC_RAW if isinstance(a, dict) and "user" in a and "character" in a]
SETTINGS = {}
for _a in _ACC_RAW:
    if isinstance(_a, dict) and _a not in ACCOUNTS:
        SETTINGS.update(_a)
# The Claude API key for chat replies (any key name containing "api_key" is accepted).
ANTHROPIC_API_KEY = (os.environ.get("ANTHROPIC_API_KEY")
                     or next((v for k, v in SETTINGS.items() if "api_key" in k.lower() and v), None))

# After every reset: walk to the Lorencia bar, talk to Liaman Barmaid, buy a Zen Chest if affordable.
SHOP_AFTER_RESET = True
SHOP_SPOT        = (0, 122, 133)        # (map, x, y) Lorencia bar; Liaman Barmaid stands at (123,135)
SHOP_NPC         = "Liaman Barmaid"
ZEN_CHEST_PRICE  = 500_000_000
ZEN_KEEP         = 700_000_000          # never buy below this: every character keeps at least this much zen
ZEN_CHEST_MAX_PER_RESET = 64          # safety cap per shopping trip
ZEN_CHEST_SLOT   = 0                    # first slot of her shop (used if the name can't be read)
SHOP_GIVE_UP_SEC = 180                  # stop trying after this long

# (from_level, to_level, /move alias, map index, [main spot, alternate spots...])
# Map indexes from the client: Lorencia=0, Arena ("Stadium")=6, Arena2=83, Arena3=84, Arena4=85
ROUTE = [
    # (from lvl, to lvl, /move alias, map index, spots). Juan 2026-10-11: out of Lorencia at 50.
    (1,   50,  "Lorencia", 0,  [(38, 58), (30, 31)]),   # (63,80) and (33,65) dropped: no good
    (50,  100, "Stadium",  6,  [(69, 81), (48, 94), (46, 74), (49, 56), (49, 39)]),   # "Arena" (/move stadium, lvl 50);
                                                                                      # coords copied from Arena4
    (100, 200, "Arena2",   83, [(47, 83), (69, 81), (51, 57)]),
    (200, 300, "Arena3",   84, [(48, 87), (69, 81), (85, 77)]),
    (300, 400, "Arena4",   85, [(69, 81), (48, 94), (46, 74), (49, 56), (49, 39)]),   # order = learned rates
]
# A spot is (x, y) on the leg's own map, or ("<alias>", x, y) on another map of this list
# (/move <alias> is used to get there). The leg's map stays the default.
MAP_INDEX = {"Lorencia": 0, "Devias": 2, "Stadium": 6, "Arena2": 83, "Arena3": 84, "Arena4": 85}


class Spot(tuple):
    """(x, y) that also knows its map: unpacks / compares as (x, y), prints as "Arena4 (69, 81)"
    when it is not on the leg's own map."""

    def __new__(cls, xy, alias, map_idx, other_map=False):
        t = super().__new__(cls, xy)
        t.alias, t.map, t.other_map = alias, map_idx, other_map
        return t

    def __repr__(self):
        return f"{self.alias} ({self[0]}, {self[1]})" if self.other_map else f"({self[0]}, {self[1]})"

    __str__ = __repr__


def _spot(leg_alias, leg_map, sp):
    if len(sp) == 3:
        return Spot((sp[1], sp[2]), sp[0], MAP_INDEX[sp[0]], other_map=sp[0] != leg_alias)
    return Spot((sp[0], sp[1]), leg_alias, leg_map)


ROUTE = [(f, t, a, m, [_spot(a, m, sp) for sp in spots]) for f, t, a, m, spots in ROUTE]


def spot_step(entry, i):
    """(frm, to, alias, map_idx, x, y) for spot i of a ROUTE entry (alias / map of that spot)."""
    sp = entry[4][i]
    return (entry[0], entry[1], sp.alias, sp.map, sp[0], sp[1])

# ── when to switch to an alternate spot ──
# A spot is judged by LEVELS PER MINUTE of the slowest character still below the reset level
# (raw EXP grows with level, so it can't compare spots). Every spot's rate is remembered across
# resets and restarts in spot_stats.json, and spots are ranked by it (best first).
RATE_WINDOW_SEC     = 240   # measure levels/min over this window
LOW_RATE_RATIO      = 0.5   # switch if levels/min < 50% of the best known rate of another spot in this range
SETTLE_SEC          = 240   # give a new spot this long before judging its rate
STATS_FILE          = "spot_stats.json"
STATS_MIN_MINUTES   = 5     # a spot needs this many measured minutes before its rate is trusted
STATS_HALF_LIFE_MIN = 120   # older measurements fade out (half weight after this many measured minutes)
SWITCH_ON_CROWD     = False # True = also leave a spot when other players stand on it (rate rule still applies)
CROWD_RADIUS        = 8     # tiles around the spot
CROWD_SEC           = 60    # outsiders standing at the spot this long -> switch
DEATHS_TO_SWITCH    = 6     # party deaths at one spot ...
DEATH_WINDOW_SEC    = 600   # ... within this many seconds ...
DEATH_MIN_CHARS     = 2     # ... by at least this many different characters -> switch
DEATH_KEEP_BEST     = True  # on the best-ranked spot, deaths alone never move the group (the rate rule still can)
DEATH_IGNORE        = []    # character names whose deaths never count, e.g. ["cucaracha"]
BAD_SPOT_COOLDOWN   = 900   # a spot we left is skipped for this long
RECHECK_MAIN_SEC    = 1200  # not on the best-ranked spot this long -> try it again

# ── scouting: on entering a new level range, the main character checks the spots alone ──
# He walks to each spot of that range (best-ranked first) and looks for other players within
# CROWD_RADIUS. The first empty spot is chosen; the alts wait at the warp point meanwhile.
# If every spot is taken, the pick weighs each spot's known rate against how many players are on it.
SCOUT_ON_NEW_ZONE   = True
SCOUT_LOOK_SEC      = 4     # seconds standing at a spot before counting players (let them load)
SCOUT_STUCK_SEC     = 30    # can't get closer to a spot this long -> skip it
SCOUT_GIVE_UP_SEC   = 300   # whole scouting run longer than this -> use the best spot seen so far

RENDER_SCALE     = 50       # Option > Quality > Render scale (%) set at login; None = leave as is
RESET_LEVEL      = None     # None = auto-detect with /resetinfo, otherwise e.g. 400
HELPER_KEY       = "Equal"  # "=" toggles MU Helper (also written into the game's key bindings)

# Game options every account gets (Juan 2026-10-14). Keys/values are the client's own
# (localStorage "mu_options"). Sliders and choices store an INDEX, not the number shown:
#   uiScale        0..8 -> 70 80 90 100 115 130 150 175 200 %
#   renderScale    0..5 -> 100 90 80 75 60 50 %
#   renderDistance 0..9 -> 40 80 120 160 200 240 280 320 400 480
#   fpsLimit       0..4 -> uncapped 30 60 90 120
#   lootZen        0..9 -> off 100 500 1k 5k 10k 50k 100k 500k 1M
#   volume 0..9 (0 = off), musicVolume 0..10, cameraFov 20..60 (degrees, stored as is)
# Written into the browser before the game loads (every page load) and set again live after
# the Low preset, so they also come back after the main character's pause settings.
GAME_OPTIONS = {
    "wsadMovement": True,       # Controls > Camera and movement: Move with W A S D
    "cameraFov": 60,            # Controls > Camera and movement: Field of view 60
    "volume": 0,                # Sound: master volume off = all sound off
    "lootFilter": True,         # Game > Loot: Filter drop names ON ...
    "lootJewels": True,
    "lootExcellent": True,
    "lootAncient": True,
    "lootHighLevel": True,
    "lootOther": False,         # ... except All other items
    "lootZen": 0,               # zen piles: off
    "hideEnteredGame": True,    # Game > Chat and alerts: hide "entered the game"
    "uiScale": 4,               # Interface > Screen: Interface size 115%
}
# Option > Video > Quality > Preset "Low" (the client's own preset values)
LOW_PRESET = {
    "renderingStyle": 0, "renderScale": 3, "upscale": 0, "msaa": 0, "anisotropy": 0, "sunShafts": 0,
    "postProcessing": False, "shadows": False, "dynamicLights": False, "clouds": False,
    "weatherEffects": False, "ambientParticles": False, "animatedWater": False, "advancedEffects": False,
    "grassDensity": 0, "renderDistance": 0, "monsterEffects": False, "otherPlayerSkillEffects": False,
    "otherPlayerItemEffects": False, "otherPlayerPets": False, "visiblePlayers": 3,
    "distantAnimations": True, "itemEffects": 1, "effectLevel": 4, "fpsLimit": 2,
}
RENDER_SCALE_STEPS = [100, 90, 80, 75, 60, 50]   # renderScale index -> %
ARRIVE_RADIUS    = 3        # tiles: close enough to start the helper / invite to party
FOLLOW_EVERY     = 10       # seconds: alts check where the main character is this often
FOLLOW_RADIUS    = 8        # tiles: alt further than this from the main ...
FOLLOW_STRIKES   = 2        # ... on this many checks in a row -> walk back next to him
FOLLOW_GIVE_UP   = 30       # seconds: stop trying to reach the main (he moved / path blocked)
LEASH_RADIUS     = 25       # tiles: helper drifted further than this -> walk back
STUCK_SECONDS    = 45       # no walking progress this long -> re-warp
DISCONNECT_RELOAD_SECONDS = 60  # out of game this long with no progress -> reload the page
SELECT_TRIES     = 3        # character-select attempts before falling back to a page reload
GRAND_RESET_CONFIRM_SEC = 20  # r0 / lvl <= 10 must read this long in a row before it counts as a grand reset
HELPER_FAILS_RELOAD = 3     # MU Helper toggle failed this many times in a row -> reload that window
INVITE_EVERY     = 10       # seconds between party invites to the same member
TICK             = 1.0      # seconds between group checks
PAUSE_KEY        = "p"      # press in the console window to pause / resume the bot
PAUSE_FILE       = "PAUSE"  # or create a file with this name next to the script to pause (delete it to resume)
HELPERS_OFF_ON_PAUSE = True # turn every MU Helper off when pausing
HEADLESS         = False
WINDOW_SIZE      = (800, 500)
MINIMIZE_WINDOWS = True     # minimize each game window once it is in game and set up; pausing (P) restores them
BASE_DIR         = Path(__file__).parent
LOG_FILE         = BASE_DIR / "mu_group_bot.log"
# ──────────────────────────────────────────────────────────────────


def log(msg, who="group"):
    line = f"[{datetime.now():%H:%M:%S}] [{who}] {msg}"
    print(line, flush=True)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def render_scale_index(percent):
    """Render scale % -> the client's slider index (nearest step)."""
    return min(range(len(RENDER_SCALE_STEPS)), key=lambda i: abs(RENDER_SCALE_STEPS[i] - percent))


# Runs in every game page before the client's own code: writes GAME_OPTIONS into the saved
# options and binds MU Helper to HELPER_KEY, so the client starts with them.
OPTIONS_INIT_JS = """
(() => { try {
  if (!/mormon/.test(location.hostname)) return;
  const want = __OPTIONS__, helperKey = __HELPER_KEY__;
  let o = {};
  try { o = JSON.parse(localStorage.getItem('mu_options') || '{}') || {}; } catch (e) {}
  Object.assign(o, want);
  // without this the client "migrates" old saves on load (it would turn W A S D back off)
  o.defaultsRevision = Math.max(+o.defaultsRevision || 0, 3);
  localStorage.setItem('mu_options', JSON.stringify(o));
  if (helperKey) {
    const names = Object.keys(localStorage).filter(k => k === 'mu_keys' || k.startsWith('mu_keys:'));
    if (!names.includes('mu_keys')) names.push('mu_keys');
    for (const k of names) {
      let b = {};
      try { b = JSON.parse(localStorage.getItem(k) || '{}') || {}; } catch (e) {}
      for (const a of Object.keys(b)) if (b[a] === helperKey && a !== 'muHelper') b[a] = '';
      b.muHelper = helperKey;
      localStorage.setItem(k, JSON.stringify(b));
    }
  }
} catch (e) {} })();
"""


def prepare_context(ctx):
    """Call right after launching a game browser, before the first page.goto()."""
    js = (OPTIONS_INIT_JS.replace("__OPTIONS__", json.dumps(GAME_OPTIONS))
                         .replace("__HELPER_KEY__", json.dumps(HELPER_KEY or "")))
    ctx.add_init_script(script=js)


# With the Option window open: finds the window's own "set" function (React props of an option
# row) and sets every given option through it - same as moving the slider / clicking the toggle,
# so it applies live. Returns the options that did not end up with the wanted value.
SET_OPTIONS_JS = """
(vals) => {
  let actions = null;
  for (const el of document.querySelectorAll('[data-row-id], [data-row-id] *')) {
    const fk = Object.keys(el).find(k => k.startsWith('__reactFiber$'));
    for (let f = fk && el[fk], d = 0; f && d < 40; f = f.return, d++) {
      const p = f.memoizedProps;
      if (p && p.actions && typeof p.actions.set === 'function') { actions = p.actions; break; }
    }
    if (actions) break;
  }
  if (!actions) return {ok: false, why: 'option rows not found'};
  for (const [k, v] of Object.entries(vals)) {
    try { actions.set(k, v); } catch (e) {}
  }
  let saved = {};
  try { saved = JSON.parse(localStorage.getItem('mu_options') || '{}'); } catch (e) {}
  const off = Object.keys(vals).filter(k => k in saved && saved[k] !== vals[k]);
  return {ok: true, off};
}
"""


STATE_JS = """
() => {
  const s = window.__store, w = window.__world, soc = window.__social;
  if (!s) return {ready:false};
  const p = s.playerData || {};
  const hero = w && w.playerEntity;
  return {
    ready: true,
    inGame: !!hero && !s.sceneLoading,
    sceneLoading: !!s.sceneLoading,
    connectionLost: !!s.connectionLost,
    chars: (s.charactersList || []).map(c => c.Name),
    name: p.name, level: p.level, resets: p.resets, exp: Number(p.exp) || 0,
    x: hero ? Math.floor(hero.transform.pos.x) : p.x,
    y: hero ? Math.floor(hero.transform.pos.z) : p.y, hp: p.currentHP, maxHP: p.maxHP, money: p.money,
    vit: p.sta, points: p.points,
    map: w ? w.mapIndex : -1,
    helper: !!(s.muHelper && s.muHelper.active),
    loginVisible: !!document.querySelector('input[type=password]'),
    party: soc ? (soc.partyMembers || []).map(m => m.name) : [],
    partyRequest: !!(soc && soc.partyRequest),
    partyRequestText: soc && soc.partyRequest ? JSON.stringify(soc.partyRequest) : "",
  };
}
"""

WALK_JS = """
([x, y]) => {
  const w = window.__world, hero = w && w.playerEntity;
  if (!hero || !w.isWalkable(x, y)) return false;
  const m = hero.playerMoveTo, pf = hero.pathfinding;
  m.point.x = x; m.point.y = y; m.handled = false; m.sendToServer = true;
  pf.from.x = hero.transform.pos.x; pf.from.y = hero.transform.pos.z;
  pf.to.x = x; pf.to.y = y; pf.calculated = false;
  return true;
}
"""

INVITE_JS = """
(name) => {
  const w = window.__world;
  const e = w.netObjsQuery.entities.find(e => e.objectNameInWorld === name && !e.localPlayer);
  if (!e) return "not in view";
  window.__social.partyInvite({netId: e.netId, name});
  return "invited";
}
"""

CROWD_JS = """
([x, y, r, ours]) => {
  const w = window.__world;
  return w.netObjsQuery.entities.filter(e =>
      !e.localPlayer && e.npcType === undefined && !e.dying &&
      (!!e.playerAnimation || e.skin !== undefined) &&
      !ours.includes(e.objectNameInWorld) &&
      Math.hypot(e.transform.pos.x - x, e.transform.pos.z - y) <= r)
    .map(e => e.objectNameInWorld || ('#' + e.netId));
}
"""

CHAT_LINES_JS = """
() => (window.__social.chatLines || []).slice(-30).map(l =>
  typeof l === 'string' ? l : [l.text, l.message, l.msg, l.content].filter(Boolean).join(' ') || JSON.stringify(l))
"""

# names of client UI flags that look like an open window (diagnostic for "Cannot do reset with any windows opened")
OPEN_WINDOWS_JS = """
() => {
  const s = window.__store, out = [];
  for (const k of Object.keys(s)) {
    if (k === 'minimapEnabled' || typeof s[k] === 'function') continue;
    if (/(Enabled|Open|Opened|Visible|Win|Window)$/.test(k) && s[k]) out.push(k);
  }
  if (s.npcShop) out.push('npcShop');
  if (window.__commands && window.__commands.windowOpen) out.push('commands.windowOpen=' + window.__commands.windowOpen);
  return out;
}
"""


class SpotStats:
    """Levels/min per farming spot, remembered across resets and restarts (spot_stats.json).
    Key: "<alias> x,y". Value: {"levels": decayed levels gained, "minutes": decayed minutes measured}."""

    def __init__(self, path):
        self.path = path
        try:
            self.data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.data = {}

    @staticmethod
    def key(alias, spot):
        # a ROUTE spot always knows its own map ("Arena4" spots in the Arena3 leg are stored as Arena4)
        if isinstance(spot, Spot):
            alias = spot.alias + ("/solo" if alias.endswith("/solo") else "")
        return f"{alias} {spot[0]},{spot[1]}"

    def add(self, alias, spot, levels, minutes):
        if minutes <= 0 or levels < 0:
            return
        d = self.data.setdefault(self.key(alias, spot), {"levels": 0.0, "minutes": 0.0})
        keep = 0.5 ** (minutes / STATS_HALF_LIFE_MIN)
        d["levels"] = d["levels"] * keep + levels
        d["minutes"] = d["minutes"] * keep + minutes
        try:
            self.path.write_text(json.dumps(self.data, indent=1, sort_keys=True), encoding="utf-8")
        except OSError:
            pass

    def rate(self, alias, spot):
        """Known levels/min, or None if the spot hasn't been measured long enough."""
        d = self.data.get(self.key(alias, spot))
        if not d or d["minutes"] < STATS_MIN_MINUTES:
            return None
        return d["levels"] / d["minutes"]

    def estimates(self, alias, spots):
        """Rate per spot index; unmeasured spots get the median of the measured ones (so they get tried)."""
        known = {i: self.rate(alias, s) for i, s in enumerate(spots)}
        vals = sorted(v for v in known.values() if v is not None)
        fill = vals[len(vals) // 2] if vals else 1.0
        return {i: (v if v is not None else fill) for i, v in known.items()}

    def ranked(self, alias, spots):
        """Spot indexes, best known rate first (list order breaks ties)."""
        est = self.estimates(alias, spots)
        return sorted(range(len(spots)), key=lambda i: (-est[i], i))


def set_helpers(chars, on):
    """Turn MU Helper on/off for several characters at once: press the key in every window,
    then wait ONCE for all of them (instead of up to 8 s per character, one after the other)."""
    todo = [c for c in chars if c.s.get("helper") != on]
    if not todo:
        return
    word = "ON" if on else "OFF"

    def wait(cs):
        for _ in range(8):  # up to 4 s for the server to confirm
            time.sleep(0.5)
            cs = [c for c in cs if c.refresh().get("helper") != on]
            if not cs:
                break
        return cs

    for c in todo:
        c.log(f"helper -> {word}")
        c.page.evaluate("() => { const c=document.querySelector('canvas'); c && c.focus(); }")
        c.page.keyboard.press(HELPER_KEY)
    todo = wait(todo)
    for c in todo:
        c.page.evaluate("() => window.__store.toggleMuHelper()")
    todo = wait(todo)
    for c in chars:
        if c in todo:
            c.helper_fails += 1
            c.log(f"helper did not turn {word.lower()} ({c.helper_fails}x in a row)")
            if c.helper_fails >= HELPER_FAILS_RELOAD:
                c.reload_page(f"MU Helper stuck after {c.helper_fails} tries")
        else:
            c.helper_fails = 0


class Char:
    def __init__(self, acc, page, is_leader):
        self.acc, self.page, self.is_leader = acc, page, is_leader
        self.name = acc["character"]
        self.s = {"ready": False}
        self.last_ingame = time.time()
        self.was_online = False      # online on the previous tick (to start the disconnect timer fresh)
        self.walk_target = None
        self.walk_best = 0
        self.walk_progress_at = time.time()
        self.last_warp = 0
        self.last_reset_try = 0
        self.last_invite = {}
        self.autoparty_sent = False
        self.next_login_at = 0
        self.login_fails = 0
        self.select_tries = 0       # character-select attempts since last in game
        self.spot = None
        self.relocating = False
        self.was_dead = False
        self.gfx_done = False
        self.shop_pending = False
        self.shop_started = 0
        self.following = False      # alt walking back to the main character
        self.follow_since = 0
        self.last_follow_check = 0
        self.follow_strikes = 0     # consecutive follow checks too far from the main
        self.helper_fails = 0       # consecutive failed MU Helper toggles
        self.known = (-1, 0)       # (resets, highest level seen at that reset count)
        self.grand_seen = self.grand_last = None  # first / last time the client read r0 / lvl <= 10 (possible grand reset)
        self.stale_logged = False
        self.minimized = False
        self.bc = None              # Blood Castle per-character state (mu_bc.py)
        self.rec = Recorder(page, self.name, self.log)   # records your play while paused (mu_recorder.py)

    def log(self, msg):
        log(msg, self.name)

    def set_window(self, state):
        """state: 'minimized' or 'normal' (Chrome DevTools Browser.setWindowBounds)."""
        try:
            cdp = self.page.context.new_cdp_session(self.page)
            win = cdp.send("Browser.getWindowForTarget")["windowId"]
            cdp.send("Browser.setWindowBounds", {"windowId": win, "bounds": {"windowState": state}})
            cdp.detach()
            self.minimized = state == "minimized"
            self.log(f"window -> {state}")
        except Exception as e:
            self.minimized = state == "minimized"   # don't retry every tick
            self.log(f"could not set window {state}: {e!r}")

    # ── state / actions ──
    def refresh(self):
        try:
            self.s = self.page.evaluate(STATE_JS)
        except PWError:
            self.s = {"ready": False}
        # The client's level can go stale after a map-server switch / reconnect (it re-reads the
        # character list from login time, e.g. "10" right after a reset) and characters that
        # don't kill anything never get a level-up packet to correct it.
        # Rule: within the same reset count, the level can only go up. The reset count only goes
        # down on a grand reset (0 resets, level 1); any other drop is stale data.
        lvl, rs = self.s.get("level"), self.s.get("resets")
        if self.s.get("inGame") and lvl and rs is not None:
            k_rs, k_lvl = self.known
            if rs >= k_rs:
                self.grand_seen = None
            if rs > k_rs:
                if k_rs >= 0:  # reset counter went up -> a reset really happened
                    self.log(f"RESET confirmed -> resets={rs}")
                    self.walk_target = None
                    if SHOP_AFTER_RESET:
                        self.shop_pending, self.shop_started = True, time.time()
                self.known = (rs, lvl)
            elif rs < k_rs:
                now = time.time()
                if rs == 0 and k_rs >= 2 and lvl <= 10 and self.s.get("maxHP") and self.grand_seen \
                        and now - self.grand_last <= 10 and now - self.grand_seen >= GRAND_RESET_CONFIRM_SEC:
                    # grand reset (done by hand): back to level 1 / 0 resets -> start tracking from scratch.
                    # Only after it reads "r0" for GRAND_RESET_CONFIRM_SEC in a row: a single r0/lvl 1
                    # read while the client loads made three false "GRAND RESET" on wizzardo.
                    self.log(f"GRAND RESET detected (resets {k_rs} -> 0, level {lvl})")
                    self.known, self.stale_logged, self.grand_seen = (rs, lvl), False, None
                    self.walk_target = None
                    self.spot = None
                else:
                    if rs == 0 and k_rs >= 2 and lvl <= 10:
                        if not self.grand_seen or now - self.grand_last > 10:
                            self.grand_seen = now          # (re)start: the reads must be back to back
                        self.grand_last = now
                    else:
                        self.grand_seen = None
                    # a lower reset count that isn't a grand reset = stale client data: keep what we know
                    if not self.stale_logged:
                        self.log(f"client shows r{rs} lvl {lvl}, keeping known r{k_rs} lvl {k_lvl}")
                        self.stale_logged = True
                    self.s["resets"], self.s["level"] = k_rs, k_lvl
            elif rs == k_rs and lvl < k_lvl:
                if not self.stale_logged:
                    self.log(f"client shows level {lvl}, keeping known level {k_lvl}")
                    self.stale_logged = True
                self.s["level"] = k_lvl
            elif rs == k_rs:
                self.known, self.stale_logged = (rs, lvl), False
        return self.s

    @property
    def online(self):
        s = self.s
        return s.get("ready") and s.get("inGame") and not s.get("connectionLost")

    def chat(self, text):
        self.log(f"chat: {text}")
        self.page.evaluate("(t) => window.__social.sendChat(t)", text)
        time.sleep(1.2)

    def set_helper(self, on):
        set_helpers([self], on)

    def reload_page(self, reason):
        """Reload this game window; the next ticks log in and select the character again."""
        if self.page.is_closed():
            raise RuntimeError(f"{self.name} window has been closed")
        self.log(f"reloading page ({reason})")
        try:
            self.page.reload(wait_until="domcontentloaded", timeout=120_000)
        except PWError as e:
            if "has been closed" in str(e) or self.page.is_closed():
                raise  # the window is gone: let main() stop instead of retrying forever
            self.log(f"reload error: {e}")
        self.s = {"ready": False}
        self.last_ingame = time.time()
        self.select_tries = 0
        self.autoparty_sent = False
        self.gfx_done = False
        self.helper_fails = 0
        self.walk_target = None
        self.following = False

    def set_options(self, values, what="options"):
        """Opens the Option window, sets `values` live (see SET_OPTIONS_JS), closes it.
        True when it worked."""
        page = self.page
        r = None
        try:
            page.evaluate("() => { window.__store.optionsEnabled = true; }")
            for _ in range(4):
                time.sleep(0.6)
                r = page.evaluate(SET_OPTIONS_JS, values)
                if r and r.get("ok"):
                    break
        except Exception as e:
            r = {"ok": False, "why": repr(e)}
        finally:
            try:
                page.evaluate("() => { window.__store.optionsEnabled = false; }")
            except Exception:
                pass
        if r and r.get("ok"):
            off = r.get("off") or []
            self.log(f"{what} set" + (f" (did not stick: {', '.join(off)})" if off else ""))
            return True
        self.log(f"{what}: could not set them ({(r or {}).get('why')})")
        return False

    def set_low_graphics(self):
        """Low preset + bot render scale + GAME_OPTIONS, live. Done once per session (and again
        after the main character's pause settings). Falls back to clicking the preset."""
        self.gfx_done = True
        vals = dict(LOW_PRESET)
        if RENDER_SCALE:
            vals["renderScale"] = render_scale_index(RENDER_SCALE)
        vals.update(GAME_OPTIONS)
        if self.set_options(vals, f"graphics Low (render scale {RENDER_SCALE}%) + game options"):
            return
        page = self.page
        try:
            page.evaluate("() => { window.__store.optionsEnabled = true; }")
            time.sleep(1.0)
            page.get_by_text("Quality", exact=True).first.click()   # left menu, under VIDEO
            time.sleep(0.5)
            low = page.get_by_text("Low", exact=True)                # Preset: Low | Classic | Enhanced | Ultra
            if low.count():
                low.first.click()
                self.log("graphics preset -> Low")
            else:
                self.log("graphics preset: Low button not found")
            time.sleep(0.5)
            if RENDER_SCALE:
                self.set_render_scale(RENDER_SCALE)
        except Exception as e:
            self.log(f"graphics preset error: {e!r}")
        finally:
            try:
                close = page.get_by_text("Close", exact=True)
                if close.count():
                    close.first.click()
                page.evaluate("() => { window.__store.optionsEnabled = false; }")
            except Exception:
                pass

    def set_render_scale(self, target):
        """Clicks along the Render scale slider until its label reads `target`%."""
        page = self.page
        info = page.evaluate("""() => {
          const label = [...document.querySelectorAll('*')].find(e =>
              e.childElementCount === 0 && e.textContent.trim() === 'Render scale');
          if (!label) return null;
          let row = label;
          while (row.parentElement && !/\\d+\\s*%/.test(row.textContent)) row = row.parentElement;
          row.setAttribute('data-bot-row', 'render-scale');
          const range = row.querySelector('input[type=range]');
          if (range) return {range: true};
          const lr = label.getBoundingClientRect();
          let best = null;
          for (const e of row.querySelectorAll('*')) {
            const r = e.getBoundingClientRect();
            if (r.left <= lr.right || r.width < 50 || r.height > 24 || r.height < 3) continue;
            if (e.textContent.trim()) continue;          // track has no text of its own
            if (!best || r.width > best.w) best = {x: r.left, y: r.top, w: r.width, h: r.height};
          }
          return best ? {track: best} : null;
        }""")
        read = lambda: page.evaluate("""() => {
          const m = (document.querySelector('[data-bot-row=render-scale]')?.textContent || '').match(/(\\d+)\\s*%/);
          return m ? +m[1] : null; }""")
        if not info:
            self.log("render scale: slider not found")
            return
        if info.get("range"):
            page.evaluate("""(v) => {
              const el = document.querySelector('[data-bot-row=render-scale] input[type=range]');
              const set = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
              set.call(el, String(v));
              el.dispatchEvent(new Event('input', {bubbles: true}));
              el.dispatchEvent(new Event('change', {bubbles: true}));
            }""", target)
        else:
            t = info["track"]
            lo, hi = 0.0, 1.0
            for _ in range(8):  # binary search along the track
                f = (lo + hi) / 2
                page.mouse.click(t["x"] + f * t["w"], t["y"] + t["h"] / 2)
                time.sleep(0.3)
                v = read()
                if v is None or v == target:
                    break
                if v > target:
                    hi = f
                else:
                    lo = f
        self.log(f"render scale -> {read()}%")

    # ── Zen Chest after reset ──
    def shop_tick(self):
        """Returns True when the shopping trip is finished (bought, skipped or gave up)."""
        s = self.s
        if time.time() - self.shop_started > SHOP_GIVE_UP_SEC:
            self.log("zen chest: gave up (took too long)")
            return True
        money = s.get("money") or 0
        if money - ZEN_CHEST_PRICE < ZEN_KEEP:
            self.log(f"zen chest: skipped, only {money:,} zen (keeping {ZEN_KEEP:,})")
            return True
        m, x, y = SHOP_SPOT
        if s["map"] != m:
            if time.time() - self.last_warp > 15:
                self.set_helper(False)
                self.chat("/move Lorencia")
                self.last_warp = time.time()
            return False
        if math.hypot(s["x"] - x, s["y"] - y) > 2:
            self.set_helper(False)
            if self.walk_target != (x, y):
                self.log(f"walking to the bar ({x},{y})")
                self.walk_target = (x, y)
            self.page.evaluate(WALK_JS, [x, y])
            return False
        self.walk_target = None
        # at the bar: talk to the barmaid
        found = self.page.evaluate("""(name) => {
          const e = window.__world.netObjsQuery.entities.find(e =>
              e.npcType !== undefined && (e.objectNameInWorld || '').trim() === name);
          if (!e) return false;
          window.__store.talkToNpc({netId: e.netId, name: e.objectNameInWorld, npcType: e.npcType});
          return true;
        }""", SHOP_NPC)
        if not found:
            self.log(f"zen chest: {SHOP_NPC} not found nearby")
            return False
        slot = None
        for _ in range(10):
            time.sleep(0.5)
            slot = self.page.evaluate("""(fallback) => {
              const shop = window.__store.npcShop;
              if (!shop || !shop.items.some(Boolean)) return null;
              // Zen Chest = item group 14, number 11, level 4
              const i = shop.items.findIndex(it => it && it.group === 14 && it.num === 11 && it.lvl === 4);
              return i >= 0 ? i : (shop.items[fallback] ? fallback : -1);
            }""", ZEN_CHEST_SLOT)
            if slot is not None:
                break
        if slot is None or slot < 0:
            self.log("zen chest: shop did not open / item not found")
            self.page.evaluate("() => window.__store.closeNpcShop()")
            return False
        # buy as many as the zen allows; stop when zen runs out or a purchase fails (inventory full)
        money_js = "() => Number(window.__store.playerData.money) || 0"
        bought, zen = 0, money
        start = money
        while zen - ZEN_CHEST_PRICE >= ZEN_KEEP and bought < ZEN_CHEST_MAX_PER_RESET:
            self.page.evaluate("(i) => window.__store.buyItemFromNpc(i)", slot)
            after = zen
            for _ in range(10):  # wait up to 5 s for the zen to drop
                time.sleep(0.5)
                after = self.page.evaluate(money_js)
                if after <= zen - ZEN_CHEST_PRICE * 0.9:
                    break
            if after > zen - ZEN_CHEST_PRICE * 0.9:
                self.log(f"zen chest: purchase #{bought + 1} failed (inventory full?)")
                break
            bought += 1
            zen = after
            for _ in range(10):  # let the client finish the previous buy
                if not self.page.evaluate("() => !!window.__store.shopBuyPending"):
                    break
                time.sleep(0.3)
        self.page.evaluate("() => { window.__store.closeNpcShop(); window.__store.inventoryEnabled = false; }")
        self.log(f"zen chest: bought {bought} (zen {start:,} -> {zen:,}, keeping {ZEN_KEEP:,})")
        return True

    def close_windows(self):
        self.page.evaluate("""() => {
          const s = window.__store;
          for (const k of Object.keys(s)) if (/Enabled$/.test(k) && k !== 'minimapEnabled' && s[k] === true) s[k] = false;
          if (s.npcShop && s.closeNpcShop) s.closeNpcShop();
          if (s.msgWin && s.closeMsgWin) s.closeMsgWin();
          if (window.__commands && window.__commands.windowOpen) window.__commands.closeWindow();
        }""")
        time.sleep(0.5)

    # ── login / reconnect ──
    def handle_offline(self):
        s = self.s
        # Character select (e.g. right after /reset) is a normal state, not a disconnect:
        # just pick the character again. Only fall back to a page reload if that keeps failing.
        at_char_select = (s.get("ready") and not s.get("sceneLoading") and not s.get("connectionLost")
                          and not s.get("loginVisible") and self.name in (s.get("chars") or []))
        if at_char_select and self.select_tries < SELECT_TRIES:
            self.select_tries += 1
            self.log("selecting character")
            self.page.evaluate("(n) => window.__store.selectCharacterRequest(n)", self.name)
            self.last_ingame = time.time()  # progress: restart the disconnect timer
            time.sleep(5)
            return
        if s.get("connectionLost") or time.time() - self.last_ingame > DISCONNECT_RELOAD_SECONDS:
            self.reload_page("connection lost" if s.get("connectionLost") else "out of game too long")
            return
        if not s.get("ready") or s.get("sceneLoading"):
            return
        if s.get("loginVisible"):
            if time.time() < self.next_login_at:
                return
            self.log(f"logging in as {self.acc['user']}")
            self.page.evaluate("() => { window.__store.loginError = undefined; }")
            self.page.fill('input[type=text]', self.acc["user"])
            self.page.fill('input[type=password]', self.acc["password"])
            self.page.press('input[type=password]', "Enter")
            # wait for the server's answer and log it
            err = None
            for _ in range(20):
                time.sleep(0.5)
                r = self.page.evaluate(
                    "() => ({p: !!window.__store.loginProcessing, e: window.__store.loginError || null,"
                    " form: !!document.querySelector('input[type=password]')})")
                if not r["p"] and (r["e"] or not r["form"]):
                    err = r["e"]
                    break
            if err:
                self.login_fails += 1
                wait = min(30 * self.login_fails, 300)
                self.log(f"LOGIN FAILED: {err} -> retrying in {wait}s")
                self.next_login_at = time.time() + wait
            else:
                self.login_fails = 0
            self.last_ingame = time.time()  # don't reload while logging in
            return
        srv = self.page.evaluate(PICK_SERVER_JS)
        if srv:
            self.log(f"choosing server ({srv})")
            try:
                self.page.click("[data-bot-server]", timeout=5000)
            except PWError as e:
                self.log(f"server click failed: {e!r}"[:200])
            time.sleep(2)

    # ── party ──
    def party_tick(self, group):
        s = self.s
        leader = group.leader
        if not self.is_leader:
            if not self.autoparty_sent:
                self.chat("/autoparty on")
                self.autoparty_sent = True
            if s.get("partyRequest"):
                if leader.name in s.get("partyRequestText", "") or "{" not in s.get("partyRequestText", ""):
                    self.log("accepting party invite")
                    self.page.evaluate("() => window.__social.partyRespond(true)")
            return
        # leader: invite anyone missing who is standing nearby
        for m in group.members:
            if m is self or not m.online or m.name in s.get("party", []):
                continue
            if time.time() - self.last_invite.get(m.name, 0) < INVITE_EVERY:
                continue
            if math.hypot(s["x"] - m.s["x"], s["y"] - m.s["y"]) > 8 or s["map"] != m.s["map"]:
                continue
            res = self.page.evaluate(INVITE_JS, m.name)
            self.last_invite[m.name] = time.time()
            self.log(f"party invite {m.name}: {res}")

    # ── reset ──
    def check_reset_refused(self):
        """Called a few seconds after /reset. A successful /reset sends us to character select
        (confirmed later by the reset counter). Still in game -> the server refused; show why."""
        if not self.refresh().get("inGame"):
            return
        lines = self.page.evaluate(CHAT_LINES_JS)[-4:]
        for line in lines:
            self.log(f"  server: {line}")
        if any("windows opened" in l for l in lines):
            try:
                opened = self.page.evaluate(OPEN_WINDOWS_JS)
            except PWError:
                opened = []
            self.log(f"reset refused: open windows -> {opened or 'none visible in the client'}; closing and retrying soon")
            self.close_windows()
            self.last_reset_try = time.time() - 15   # retry in ~5 s instead of 20

    # ── movement toward the group spot ──
    def move_tick(self, step, follow=None):
        """follow = (x, y) of the main character when he is farming at the spot (alts only)."""
        s = self.s
        _, _, alias, map_idx, tx, ty = step
        if s["map"] != map_idx:
            if time.time() - self.last_warp > 15:
                self.set_helper(False)
                self.chat(f"/move {alias}")
                self.last_warp = time.time()
                self.walk_target = None
            return
        dist = math.hypot(s["x"] - tx, s["y"] - ty)
        if self.spot != (map_idx, tx, ty):  # group changed spot: go there even if the helper is busy
            self.spot, self.relocating = (map_idx, tx, ty), True
        # alts: every FOLLOW_EVERY s check the main's position and walk back next to him
        if follow and not self.relocating:
            fx, fy = follow
            fdist = math.hypot(s["x"] - fx, s["y"] - fy)
            now = time.time()
            if not self.following and now - self.last_follow_check >= FOLLOW_EVERY:
                self.last_follow_check = now
                self.follow_strikes = self.follow_strikes + 1 if fdist > FOLLOW_RADIUS else 0
                if self.follow_strikes >= FOLLOW_STRIKES:
                    self.log(f"{fdist:.0f} tiles from main -> walking back to ({fx},{fy})")
                    self.following, self.follow_since, self.follow_strikes = True, now, 0
            if self.following:
                if fdist <= 2 or now - self.follow_since > FOLLOW_GIVE_UP:
                    self.following = False
                    self.last_follow_check = now
                    self.set_helper(True)
                    return
                self.set_helper(False)
                self.page.evaluate(WALK_JS, [int(fx), int(fy)])
                return
        if s["helper"] and dist <= LEASH_RADIUS and not self.relocating:
            self.walk_target = None
            return
        if dist <= ARRIVE_RADIUS:
            self.walk_target = None
            self.relocating = False
            self.set_helper(True)
            return
        self.set_helper(False)
        if self.walk_target != (tx, ty):
            self.log(f"walking to ({tx},{ty}) on {alias}, at ({s['x']},{s['y']}) lvl {s['level']}")
            self.walk_target, self.walk_best, self.walk_progress_at = (tx, ty), dist, time.time()
        if dist < self.walk_best - 0.5:
            self.walk_best, self.walk_progress_at = dist, time.time()
        elif time.time() - self.walk_progress_at > STUCK_SECONDS:
            self.log("stuck walking -> re-warping")
            self.chat(f"/move {alias}")
            self.last_warp = time.time()
            self.walk_target = None
            return
        if not self.page.evaluate(WALK_JS, [tx, ty]):
            self.log(f"({tx},{ty}) not walkable / no hero")


class Group:
    def __init__(self, members):
        self.members = members
        self.leader = members[0]
        self.reset_level = RESET_LEVEL
        self.reset_round = set()   # names that still have to /reset in the current round
        self.last_status = 0
        self.first_check = True
        # alternate-spot tracking
        self.step_key = None       # index of the current ROUTE entry
        self.spot_idx = 0          # which spot of that entry we use
        self.spot_since = time.time()
        self.bad_until = {}        # (step_key, spot_idx) -> time it may be used again
        self.stats = SpotStats(BASE_DIR / STATS_FILE)
        self.lvl_samples = []      # (time, slowest level) while farming at the current spot
        self.stat_point = None     # (time, slowest level) last point added to spot_stats.json
        self.crowd_since = None
        self.deaths = []           # (time, name) of party deaths at the current spot
        self.arrived = False
        self.paused = False
        self.key_paused = False
        # scouting (see SCOUT_* config)
        self.scouting = False
        self.scout_i = 0           # position in scout_order of the spot being checked
        self.scout_order = []      # spot indexes in the order to check them (best-ranked first)
        self.scout_seen = {}       # spot index -> players counted there
        self.scout_started = 0
        self.scout_arrived_at = None
        self.scout_best = 999.0    # closest distance reached to the current spot
        self.scout_progress_at = 0
        self.bcastle = BloodCastle(self)

    def follow_point(self, step):
        """Main character's position, if he is alive and farming at the group spot."""
        lead = self.leader.s
        _, _, _, map_idx, tx, ty = step
        if (lead.get("map") != map_idx or (lead.get("hp") or 0) <= 0 or self.leader.shop_pending
                or math.hypot(lead["x"] - tx, lead["y"] - ty) > LEASH_RADIUS):
            return None
        return (lead["x"], lead["y"])

    # ── pause / manual control ──
    def pause(self):
        self.paused = True
        log("=== PAUSED: the bot is not touching the game. Play the characters yourself. "
            f"Press {PAUSE_KEY.upper()} in this window to resume. ===")
        if MINIMIZE_WINDOWS:
            for c in self.members:
                if c.minimized:
                    c.set_window("normal")   # bring the windows back so you can play (re-minimized on resume)
        if HELPERS_OFF_ON_PAUSE:
            for c in self.members:
                try:
                    if c.refresh().get("helper"):
                        c.set_helper(False)
                except Exception as e:
                    c.log(f"could not turn helper off: {e!r}")
        for c in self.members:
            c.rec.start()

    def resume(self):
        self.paused = False
        now = time.time()
        for c in self.members:
            c.rec.stop()
        for c in self.members:
            # forget everything the bot was in the middle of; it re-reads the game state next tick
            c.walk_target = None
            c.relocating = True          # walk back to the group spot even if the helper is on
            c.following = False
            c.last_follow_check = now
            c.last_warp = 0
            c.last_ingame = now          # don't treat the pause as a disconnect
            c.select_tries = 0
            c.last_reset_try = 0
            c.walk_progress_at = now
            if c.shop_pending:
                c.shop_started = now     # pause time doesn't count toward the shop give-up timer
        self.lvl_samples = []            # levels from manual play must not judge the spot
        self.stat_point = None
        self.spot_since = now
        self.arrived = False
        self.crowd_since = None
        self.deaths = []
        if self.scouting:                # restart checking the current spot
            self.scout_started, self.scout_arrived_at = now, None
            self.scout_best, self.scout_progress_at = 999.0, now
        log("=== RESUMED: the bot is in control again ===")

    # ── alternate spots ──
    def current_step(self, level):
        key = next((i for i, st in enumerate(ROUTE) if st[0] <= level < st[1]), len(ROUTE) - 1)
        if key != self.step_key:
            self.step_key = key
            alias, spots = ROUTE[key][2], ROUTE[key][4]
            self.spot_idx = self.stats.ranked(alias, spots)[0]   # best known spot first
            self._arrived_new_spot()
            if SCOUT_ON_NEW_ZONE and len(spots) > 1:
                self.start_scouting()
        return spot_step(ROUTE[key], self.spot_idx), ROUTE[key][4]

    def rate_text(self, alias, spot):
        r = self.stats.rate(alias, spot)
        return f"{r:.1f}" if r is not None else "?"

    # ── scouting: the main character finds an empty spot before the group settles ──
    def start_scouting(self):
        now = time.time()
        alias, spots = ROUTE[self.step_key][2], ROUTE[self.step_key][4]
        self.scout_order = self.stats.ranked(alias, spots)
        self.scouting, self.scout_i, self.scout_seen = True, 0, {}
        self.scout_started, self.scout_arrived_at = now, None
        self.scout_best, self.scout_progress_at = 999.0, now
        order = ", ".join(f"{spots[i]} {self.rate_text(alias, spots[i])} lvl/min" for i in self.scout_order)
        log(f"SCOUTING {alias}: {self.leader.name} checks the spots for other players (order: {order})")

    def finish_scouting(self, spots, reason):
        alias = ROUTE[self.step_key][2]
        if self.scout_seen:
            # every checked spot is taken: weigh the spot's known rate against the players on it
            # (an empty spot is taken right away, so this only runs when all are occupied / gave up)
            est = self.stats.estimates(alias, spots)
            rank = {i: n for n, i in enumerate(self.scout_order)}
            best = max(self.scout_seen,
                       key=lambda i: (est[i] / (1 + self.scout_seen[i]) if self.scout_seen[i] < 999 else -1,
                                      -rank.get(i, i)))
        else:
            best = self.scout_order[0] if self.scout_order else 0
        self.spot_idx = best
        self.scouting = False
        self._arrived_new_spot()
        self.leader.walk_target = None
        seen = ", ".join(f"{spots[i]}: {n if n < 999 else 'unreachable'}" for i, n in sorted(self.scout_seen.items()))
        log(f"SCOUTING done ({reason}) -> spot {best + 1}/{len(spots)} {spots[best]} "
            f"({self.rate_text(alias, spots[best])} lvl/min) | players seen: {seen or '-'}")

    def scout_tick(self, step, spots):
        c = self.leader
        s = c.s
        now = time.time()
        if now - self.scout_started > SCOUT_GIVE_UP_SEC:
            return self.finish_scouting(spots, "took too long")
        if self.scout_i >= len(self.scout_order):
            return self.finish_scouting(spots, "all spots occupied")
        sp = spots[self.scout_order[self.scout_i]]
        alias, map_idx = sp.alias, sp.map          # the spot being checked may be on another map
        if s["map"] != map_idx:
            if now - c.last_warp > 15:
                c.set_helper(False)
                c.chat(f"/move {alias}")
                c.last_warp = now
            self.scout_started = now     # the give-up timer only counts time spent on the map
            self.scout_arrived_at = None
            self.scout_best, self.scout_progress_at = 999.0, now
            return
        si = self.scout_order[self.scout_i]
        tx, ty = spots[si]
        dist = math.hypot(s["x"] - tx, s["y"] - ty)
        if dist > ARRIVE_RADIUS:
            c.set_helper(False)
            self.scout_arrived_at = None
            if dist < self.scout_best - 0.5:
                self.scout_best, self.scout_progress_at = dist, now
            elif now - self.scout_progress_at > SCOUT_STUCK_SEC:
                c.log(f"scout: can't reach {spots[si]} -> skipping it")
                self.scout_seen[si] = 999
                return self._scout_next()
            if c.walk_target != (tx, ty):
                c.log(f"scout: walking to spot {si + 1}/{len(spots)} ({tx},{ty})")
                c.walk_target = (tx, ty)
            c.page.evaluate(WALK_JS, [tx, ty])
            return
        # standing at the spot: give other players a moment to appear, then count them
        if self.scout_arrived_at is None:
            self.scout_arrived_at = now
            return
        if now - self.scout_arrived_at < SCOUT_LOOK_SEC:
            return
        ours = [m.name for m in self.members]
        try:
            crowd = c.page.evaluate(CROWD_JS, [tx, ty, CROWD_RADIUS, ours])
        except PWError:
            crowd = []
        self.scout_seen[si] = len(crowd)
        if not crowd:
            c.log(f"scout: spot {si + 1} ({tx},{ty}) is empty")
            self.scout_seen = {}         # take this one (no weighing needed)
            self.scout_order = [si]
            return self.finish_scouting(spots, "found an empty spot")
        c.log(f"scout: spot {si + 1} ({tx},{ty}) occupied by {', '.join(crowd[:4])}")
        self._scout_next()

    def _scout_next(self):
        self.scout_i += 1
        self.scout_arrived_at = None
        self.scout_best, self.scout_progress_at = 999.0, time.time()
        self.leader.walk_target = None

    def _arrived_new_spot(self):
        self.spot_since = time.time()
        self.lvl_samples, self.stat_point, self.crowd_since, self.deaths = [], None, None, []
        self.arrived = False

    def switch_spot(self, reason, spots):
        """Leave the current spot for the best-ranked spot that isn't on cooldown."""
        now = time.time()
        self.bad_until[(self.step_key, self.spot_idx)] = now + BAD_SPOT_COOLDOWN
        alias = ROUTE[self.step_key][2]
        order = [i for i in self.stats.ranked(alias, spots) if i != self.spot_idx]
        free = [i for i in order if self.bad_until.get((self.step_key, i), 0) <= now]
        nxt = free[0] if free else min(order, key=lambda i: self.bad_until.get((self.step_key, i), 0))
        log(f"SPOT SWITCH ({reason}): {spots[self.spot_idx]} -> {spots[nxt]} "
            f"({self.rate_text(alias, spots[nxt])} lvl/min known)")
        self.spot_idx = nxt
        self._arrived_new_spot()

    def slowest_level(self):
        """Lowest level among members still below the reset level (None if everyone is capped)."""
        cap = self.reset_level or 10**9
        lv = [c.s.get("level") or 1 for c in self.members]
        below = [l for l in lv if l < cap]
        return min(below) if below else None

    def check_spot(self, step, spots):
        now = time.time()
        _, _, alias, map_idx, tx, ty = step
        lead = self.leader.s
        d = math.hypot(lead["x"] - tx, lead["y"] - ty) if lead["map"] == map_idx else 999
        if d <= ARRIVE_RADIUS + 2:
            self.arrived = True
        at_spot = self.arrived and d <= LEASH_RADIUS
        lvl = self.slowest_level()
        if not at_spot or lvl is None:
            self.spot_since = now  # still travelling (or everyone capped): don't judge yet
            self.lvl_samples, self.stat_point = [], None
            return

        # 0) remember this spot's levels/min (spot_stats.json), once a minute after the first minute here
        if now - self.spot_since >= 60:
            if self.stat_point is None:
                self.stat_point = (now, lvl)
            elif now - self.stat_point[0] >= 60:
                t0, l0 = self.stat_point
                if lvl >= l0:
                    self.stats.add(alias, (tx, ty), lvl - l0, (now - t0) / 60)
                self.stat_point = (now, lvl)

        if len(spots) < 2:
            return
        est = self.stats.estimates(alias, spots)
        is_best = self.stats.ranked(alias, spots)[0] == self.spot_idx

        # 1) deaths (only counted names; needs several different characters dying)
        self.deaths = [(t, n) for t, n in self.deaths if now - t < DEATH_WINDOW_SEC]
        names = {n for _, n in self.deaths}
        if len(self.deaths) >= DEATHS_TO_SWITCH and len(names) >= DEATH_MIN_CHARS:
            if DEATH_KEEP_BEST and is_best and self.leader.name not in names:
                if not getattr(self, "_death_keep_logged", False):
                    log(f"{len(self.deaths)} deaths in {DEATH_WINDOW_SEC // 60} min, but this is the best "
                        f"known spot ({est[self.spot_idx]:.1f} lvl/min) -> staying")
                    self._death_keep_logged = True
            else:
                return self.switch_spot(f"{len(self.deaths)} deaths in {DEATH_WINDOW_SEC // 60} min "
                                        f"by {', '.join(sorted(names))}", spots)
        else:
            self._death_keep_logged = False

        # 2) other players standing at the spot (off by default: only the rate matters)
        if SWITCH_ON_CROWD:
            ours = [c.name for c in self.members]
            try:
                crowd = self.leader.page.evaluate(CROWD_JS, [tx, ty, CROWD_RADIUS, ours])
            except PWError:
                crowd = []
            if crowd:
                self.crowd_since = self.crowd_since or now
                if now - self.crowd_since >= CROWD_SEC:
                    return self.switch_spot(f"occupied by {', '.join(crowd[:3])}", spots)
            else:
                self.crowd_since = None

        # 3) levels/min of the slowest character vs the best known rate of the OTHER spots
        self.lvl_samples.append((now, lvl))
        self.lvl_samples = [p for p in self.lvl_samples if now - p[0] <= RATE_WINDOW_SEC]
        if now - self.spot_since < SETTLE_SEC or now - self.lvl_samples[0][0] < RATE_WINDOW_SEC * 0.9:
            return
        rate = (lvl - self.lvl_samples[0][1]) / ((now - self.lvl_samples[0][0]) / 60)
        others = [self.stats.rate(alias, s) for i, s in enumerate(spots) if i != self.spot_idx]
        others = [r for r in others if r is not None]
        best_other = max(others) if others else 0.0
        if rate <= 0 or (best_other > 0 and rate < LOW_RATE_RATIO * best_other):
            return self.switch_spot(f"{rate:.1f} lvl/min vs {best_other:.1f} known elsewhere", spots)

        # 4) not on the best-ranked spot for a while -> try it again
        best = self.stats.ranked(alias, spots)[0]
        if (best != self.spot_idx and now - self.spot_since >= RECHECK_MAIN_SEC
                and self.bad_until.get((self.step_key, best), 0) <= now):
            log(f"SPOT SWITCH (re-checking best spot): {spots[self.spot_idx]} -> {spots[best]} "
                f"({self.rate_text(alias, spots[best])} lvl/min known)")
            self.spot_idx = best
            self._arrived_new_spot()

    def detect_reset_level(self):
        c = self.leader
        c.chat("/resetinfo")
        time.sleep(2)
        for line in reversed(c.page.evaluate(CHAT_LINES_JS)):
            m = re.search(r"requires level (\d+)", line)
            if m:
                self.reset_level = int(m.group(1))
                log(f"reset level detected: {self.reset_level}")
                return
        self.reset_level = ROUTE[-1][1]
        log(f"could not read /resetinfo, using {self.reset_level}")

    def tick(self):
        for c in self.members:
            if c.page.is_closed():
                raise RuntimeError(f"{c.name} window has been closed")
            c.refresh()
            if c.online:
                c.last_ingame = time.time()
                c.was_online = True
                c.select_tries = 0
                if not c.gfx_done:
                    c.set_low_graphics()
                elif MINIMIZE_WINDOWS and not c.minimized and not HEADLESS:
                    c.set_window("minimized")
            else:
                if c.was_online:
                    # First tick we see it out of game (map warp, /reset, real drop): start the
                    # disconnect timer NOW. last_ingame can be stale if the previous tick was long
                    # (scouting warps, a reset round), which used to reload windows that were only
                    # on a loading screen.
                    c.last_ingame = time.time()
                    c.was_online = False
                c.handle_offline()

        online = [c for c in self.members if c.online]
        if len(online) < len(self.members):
            # hold everyone who is in game at their current activity until the group is complete
            return

        if self.reset_level is None:
            self.detect_reset_level()
            return

        levels = {c.name: c.s["level"] or 1 for c in self.members}
        rl = self.reset_level

        # Start a reset round when everyone has reached the reset level.
        # On startup, also finish an interrupted round (some at reset level, others already back at the start).
        if not self.reset_round:
            if all(l >= rl for l in levels.values()):
                self.reset_round = {n for n in levels}
                log(f"everyone reached {rl} -> resetting together")
            elif self.first_check and min(levels.values()) < ROUTE[0][1] and any(l >= rl for l in levels.values()):
                self.reset_round = {n for n, l in levels.items() if l >= rl}
                log(f"finishing interrupted reset round for {sorted(self.reset_round)}")
        self.first_check = False

        for c in self.members:
            if c.name in self.reset_round and levels[c.name] < rl:
                self.reset_round.discard(c.name)
        if self.reset_round:
            self.reset_together([c for c in self.members if c.name in self.reset_round])
            return

        # Blood Castle detour (mu_bc.py): pauses normal leveling while it runs
        try:
            if self.bcastle.tick():
                return
        except Exception as e:
            log(f"BC tick error: {e!r}")

        # Group step = step for the LOWEST level, so nobody moves on alone.
        step, spots = self.current_step(min(self.refresh_levels().values()))
        scouting = self.scouting
        for c in self.members:
            dead = (c.s.get("hp") or 0) <= 0
            if dead and not c.was_dead:
                if c.name not in DEATH_IGNORE:
                    self.deaths.append((time.time(), c.name))
                log(f"{c.name} died at {step[2]} ({step[4]},{step[5]})")
            c.was_dead = dead
            if dead:
                c.walk_target = None
                continue  # dead: wait for respawn
            if c.shop_pending:
                if c.shop_tick():
                    c.shop_pending = False
                continue
            if scouting:
                if c is self.leader:
                    self.scout_tick(step, spots)
                elif c.s["map"] != step[3]:
                    c.move_tick(step)        # warps to the zone (and nothing else while off the map)
                else:
                    c.walk_target = None     # on the zone map: wait where we are for the scout
                c.party_tick(self)
                continue
            c.move_tick(step, follow=self.follow_point(step) if c is not self.leader else None)
            c.party_tick(self)
        if not scouting:
            self.check_spot(step, spots)

        if time.time() - self.last_status > 60:
            self.last_status = time.time()
            rate = ""
            if len(self.lvl_samples) > 1:
                (t0, l0), (t1, l1) = self.lvl_samples[0], self.lvl_samples[-1]
                if t1 > t0:
                    rate = (f" lvl/min {(l1 - l0) / ((t1 - t0) / 60):.1f}"
                            f" (spot avg {self.rate_text(step[2], (step[4], step[5]))})")
            where = (f"SCOUTING {min(self.scout_i, len(spots) - 1) + 1}/{len(spots)}" if self.scouting
                     else f"spot {self.spot_idx + 1}/{len(spots)} ({step[4]},{step[5]})")
            log(f"zone {step[2]} {where}{rate} | " + " | ".join(
                f"{c.name} lvl {c.s['level']} r{c.s['resets']} hp {c.s['hp']}/{c.s['maxHP']} "
                f"helper {'on' if c.s['helper'] else 'off'}" for c in self.members)
                + f" | party {self.leader.s.get('party')}")

    def reset_together(self, chars):
        """/reset every character of the round at once (helpers off together, one wait),
        instead of one after the other (~1.5 min for 5 characters)."""
        now = time.time()
        todo = [c for c in chars if c.online and now - c.last_reset_try >= 20]
        if not todo:
            return
        for c in todo:
            c.last_reset_try = now
        set_helpers(todo, False)
        todo = [c for c in todo if c.online]   # a window may have been reloaded by set_helpers
        for c in todo:
            c.close_windows()
        time.sleep(0.5)
        for c in todo:
            c.chat("/reset")
            c.last_ingame = time.time()  # going to character select now is expected, not a disconnect
        time.sleep(3)
        for c in todo:
            c.check_reset_refused()

    def refresh_levels(self):
        return {c.name: c.s["level"] or 1 for c in self.members}


def disable_console_quickedit():
    """On Windows, clicking/selecting text in the console pauses the program until a key is
    pressed. Turn that off so an accidental click can't freeze the bot."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.GetStdHandle(-10)  # STD_INPUT_HANDLE
        mode = ctypes.c_uint32()
        if k32.GetConsoleMode(h, ctypes.byref(mode)):
            # clear ENABLE_QUICK_EDIT_MODE (0x40), keep ENABLE_EXTENDED_FLAGS (0x80)
            k32.SetConsoleMode(h, (mode.value & ~0x0040) | 0x0080)
    except Exception:
        pass


def pause_requested(group):
    """Console key P toggles pause; a PAUSE file next to the script also pauses while it exists."""
    pressed = False
    try:
        from console_keys import read_keys as get_console_keys
        pressed = sum(k == PAUSE_KEY for k in get_console_keys()) % 2 == 1
    except Exception:
        pass
    flag = BASE_DIR / PAUSE_FILE
    if pressed:
        if group.paused:          # resume: also clear the file so it can't re-pause
            group.key_paused = False
            try:
                flag.unlink()
            except OSError:
                pass
        else:
            group.key_paused = True
    return group.key_paused or flag.exists()


def main():
    disable_console_quickedit()
    import socket
    lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        lock.bind(("127.0.0.1", 47291))  # held for the life of this process
    except OSError:
        print("The group bot is already running in another window. Close that one first.")
        input("Press Enter to exit...")
        return
    log("=== MU group bot starting ===")
    with sync_playwright() as pw:
        members = []
        for i, acc in enumerate(ACCOUNTS):
            ctx = pw.chromium.launch_persistent_context(
                str(BASE_DIR / f"browser_profile_{acc['user']}"),
                headless=HEADLESS,
                viewport={"width": WINDOW_SIZE[0], "height": WINDOW_SIZE[1]},
                args=[
                    "--disable-background-timer-throttling",
                    "--disable-renderer-backgrounding",
                    "--disable-backgrounding-occluded-windows",
                    f"--window-position={40 + i * 50},{40 + i * 50}",
                ],
            )
            prepare_context(ctx)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(URL, wait_until="domcontentloaded", timeout=180_000)
            members.append(Char(acc, page, is_leader=(i == 0)))
        group = Group(members)
        log(f"Press {PAUSE_KEY.upper()} in this console window to pause the bot and play yourself.")
        while True:
            want_pause = pause_requested(group)
            if want_pause and not group.paused:
                group.pause()
            elif not want_pause and group.paused:
                group.resume()
            if group.paused:
                if any(c.page.is_closed() for c in group.members):
                    log("a game window was closed -> stopping")
                    return
                for c in group.members:
                    c.rec.tick()
                time.sleep(0.3)
                continue
            try:
                group.tick()
            except KeyboardInterrupt:
                raise
            except Exception as e:
                if "has been closed" in str(e):
                    log("a game window was closed -> stopping")
                    return
                log(f"error: {e!r}")
                time.sleep(5)
            time.sleep(TICK)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("stopped by user")
        sys.exit(0)

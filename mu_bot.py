"""
MU reset bot for https://mu.mormon.garden (OpenMUCB browser client).

Loop:  level up on route spots -> warp to next zone -> walk to spot -> MU Helper (=)
       -> at reset level: /reset -> start over.
Death: waits for respawn, then resumes the current step (step is chosen by level).
Disconnect / stuck: reloads the page, logs in, selects the character.

Run:  run.bat   (first run installs Python deps + Chromium)
Stop: Ctrl+C in the console window.
"""
import json
import math
import re
import sys
import random
import time
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright, Error as PWError

# Juan 2026-10-15: wait a random 1-5 s before every /move so the warps don't look scripted.
MOVE_DELAY_RANGE = (1.0, 5.0)

# ───────────────────────────── CONFIG ─────────────────────────────
URL       = "https://mu.mormon.garden/"

# Accounts live in accounts.json next to this script (not committed to git).
# Copy accounts.example.json to accounts.json and fill in your logins.
_ACC_FILE = Path(__file__).with_name("accounts.json")
if not _ACC_FILE.exists():
    sys.exit("accounts.json not found - copy accounts.example.json to accounts.json and fill it in.")
_ACC      = json.loads(_ACC_FILE.read_text(encoding="utf-8"))[0]   # first account
USERNAME  = _ACC["user"]
PASSWORD  = _ACC["password"]
CHARACTER = _ACC["character"]
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

# (from_level, to_level, /move alias, map index, x, y)
# Map indexes from the client: Lorencia=0, Arena2=83, Arena3=84, Arena4=85
ROUTE = [
    (1,   100, "Lorencia", 0,  34, 50),
    (100, 200, "Arena2",   83, 47, 83),
    (200, 300, "Arena3",   84, 48, 74),
    (300, 400, "Arena3",   84, 48, 37),
]

RESET_LEVEL      = None   # None = auto-detect with /resetinfo, otherwise e.g. 400
HELPER_KEY       = "Equal"  # the "=" key toggles MU Helper
ARRIVE_RADIUS    = 3      # tiles: close enough to the spot to start the helper
LEASH_RADIUS     = 25     # tiles: if the helper drifts further than this, walk back
STUCK_SECONDS    = 45     # no progress walking for this long -> re-warp
DISCONNECT_RELOAD_SECONDS = 60  # not in game this long -> reload page
TICK             = 1.5    # seconds between checks
HEADLESS         = False  # keep False so you can watch it
PROFILE_DIR      = Path(__file__).with_name("browser_profile")  # keeps game assets cached
LOG_FILE         = Path(__file__).with_name("mu_bot.log")
# ──────────────────────────────────────────────────────────────────


def log(msg):
    line = f"[{datetime.now():%H:%M:%S}] {msg}"
    print(line, flush=True)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


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
    uiState: s.uiState,
    chars: (s.charactersList || []).map(c => c.Name),
    name: p.name, level: p.level, resets: p.resets,
    x: p.x, y: p.y, hp: p.currentHP, maxHP: p.maxHP, money: p.money,
    map: w ? w.mapIndex : -1,
    helper: !!(s.muHelper && s.muHelper.active),
    loginVisible: !!document.querySelector('input[type=password]'),
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

CHAT_JS = "(t) => window.__social.sendChat(t)"

CHAT_LINES_JS = """
() => (window.__social.chatLines || []).slice(-30).map(l =>
  typeof l === 'string' ? l : [l.text, l.message, l.msg, l.content].filter(Boolean).join(' ') || JSON.stringify(l))
"""


class Bot:
    def __init__(self, page):
        self.page = page
        self.reset_level = RESET_LEVEL
        self.last_ingame = time.time()
        self.walk_target = None
        self.walk_best = None
        self.walk_progress_at = time.time()
        self.last_warp = 0
        self.last_reset_try = 0

    # ── helpers ──
    def st(self):
        try:
            return self.page.evaluate(STATE_JS)
        except PWError:
            return {"ready": False}

    def chat(self, text):
        if text.lower().startswith("/move"):
            delay = random.uniform(*MOVE_DELAY_RANGE)
            log(f"waiting {delay:.1f}s before {text}")
            time.sleep(delay)
        log(f"chat: {text}")
        self.page.evaluate(CHAT_JS, text)
        time.sleep(1.2)  # client rate-limits chat

    def press_helper(self):
        self.page.evaluate("() => { const c=document.querySelector('canvas'); c && c.focus(); }")
        self.page.keyboard.press(HELPER_KEY)
        time.sleep(1.0)

    def set_helper(self, on, s):
        if s["helper"] == on:
            return
        log(f"helper -> {'ON' if on else 'OFF'}")
        self.press_helper()
        if self.st().get("helper") != on:  # key didn't land; use the client's own toggle
            self.page.evaluate("() => window.__store.toggleMuHelper()")
            time.sleep(1.0)

    def step_for(self, level):
        for step in ROUTE:
            if step[0] <= level < step[1]:
                return step
        return ROUTE[-1]

    # ── login / reconnect ──
    def handle_out_of_game(self, s):
        if not s.get("ready"):
            return
        if s.get("sceneLoading"):
            return
        if s.get("loginVisible"):
            log("logging in")
            self.page.fill('input[type=text]', USERNAME)
            self.page.fill('input[type=password]', PASSWORD)
            self.page.press('input[type=password]', "Enter")
            time.sleep(4)
            return
        if CHARACTER in (s.get("chars") or []):
            log(f"selecting character {CHARACTER}")
            self.page.evaluate("(n) => window.__store.selectCharacterRequest(n)", CHARACTER)
            time.sleep(6)
            return
        srv = self.page.evaluate(PICK_SERVER_JS)
        if srv:
            log(f"choosing server ({srv})")
            try:
                self.page.click("[data-bot-server]", timeout=5000)
            except PWError as e:
                log(f"server click failed: {e!r}"[:200])
            time.sleep(3)

    def reload(self):
        log("reloading page")
        try:
            self.page.reload(wait_until="domcontentloaded", timeout=120_000)
        except PWError as e:
            log(f"reload error: {e}")
        self.last_ingame = time.time()
        time.sleep(10)

    # ── reset ──
    def detect_reset_level(self):
        self.chat("/resetinfo")
        time.sleep(2)
        for line in reversed(self.page.evaluate(CHAT_LINES_JS)):
            m = re.search(r"requires level (\d+)", line)
            if m:
                self.reset_level = int(m.group(1))
                log(f"reset level detected: {self.reset_level}")
                return
        self.reset_level = ROUTE[-1][1]
        log(f"could not read /resetinfo, using {self.reset_level}")

    def do_reset(self, s):
        if time.time() - self.last_reset_try < 20:
            return
        self.last_reset_try = time.time()
        self.set_helper(False, s)
        time.sleep(1)
        self.chat("/reset")
        time.sleep(5)
        after = self.st()
        if after.get("level", 999) < s["level"]:
            log(f"RESET done -> resets={after.get('resets')}")
            self.walk_target = None
        else:
            for line in self.page.evaluate(CHAT_LINES_JS)[-5:]:
                log(f"  server: {line}")

    # ── main tick ──
    def tick(self):
        s = self.st()
        if not s.get("ready") or not s.get("inGame") or s.get("connectionLost"):
            if s.get("connectionLost") or time.time() - self.last_ingame > DISCONNECT_RELOAD_SECONDS:
                self.reload()
                return
            self.handle_out_of_game(s)
            return
        self.last_ingame = time.time()

        if self.reset_level is None:
            self.detect_reset_level()
            return

        if (s.get("hp") or 0) <= 0:
            self.walk_target = None
            return  # dead: wait for respawn, then the step resumes by level

        level = s["level"] or 1
        if level >= self.reset_level:
            self.do_reset(s)
            return

        _, _, alias, map_idx, tx, ty = self.step_for(level)

        # wrong map -> warp
        if s["map"] != map_idx:
            if time.time() - self.last_warp > 15:
                self.set_helper(False, s)
                self.chat(f"/move {alias}")
                self.last_warp = time.time()
                self.walk_target = None
            return

        dist = math.hypot(s["x"] - tx, s["y"] - ty)

        # helper running near the spot -> leave it alone
        if s["helper"] and dist <= LEASH_RADIUS:
            self.walk_target = None
            return

        # at the spot -> start helper
        if dist <= ARRIVE_RADIUS:
            self.walk_target = None
            self.set_helper(True, s)
            return

        # walk to the spot (stop helper first, it cancels walking anyway)
        self.set_helper(False, s)
        if self.walk_target != (tx, ty):
            log(f"walking to ({tx},{ty}) on {alias}, now at ({s['x']},{s['y']}) lvl {level}")
            self.walk_target, self.walk_best, self.walk_progress_at = (tx, ty), dist, time.time()
        if dist < self.walk_best - 0.5:
            self.walk_best, self.walk_progress_at = dist, time.time()
        elif time.time() - self.walk_progress_at > STUCK_SECONDS:
            log("stuck walking -> re-warping")
            self.chat(f"/move {alias}")
            self.last_warp = time.time()
            self.walk_target = None
            return
        if not self.page.evaluate(WALK_JS, [tx, ty]):
            log(f"({tx},{ty}) not walkable / no hero")


def main():
    log("=== MU bot starting ===")
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            str(PROFILE_DIR),
            headless=HEADLESS,
            viewport={"width": 1280, "height": 800},
            args=[  # keep the game running when the window is in the background
                "--disable-background-timer-throttling",
                "--disable-renderer-backgrounding",
                "--disable-backgrounding-occluded-windows",
            ],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(URL, wait_until="domcontentloaded", timeout=180_000)
        bot = Bot(page)
        last_status = 0
        while True:
            try:
                bot.tick()
                if time.time() - last_status > 60:
                    s = bot.st()
                    if s.get("inGame"):
                        log(f"status: lvl {s['level']} resets {s['resets']} map {s['map']} "
                            f"pos ({s['x']},{s['y']}) hp {s['hp']}/{s['maxHP']} helper {s['helper']}")
                    last_status = time.time()
            except KeyboardInterrupt:
                raise
            except Exception as e:  # never die; log and keep going
                log(f"error: {e!r}")
                time.sleep(5)
            time.sleep(TICK)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("stopped by user")
        sys.exit(0)

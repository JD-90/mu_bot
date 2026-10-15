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
  * Strangers' party invites are accepted while leveling (LEVEL_ACCEPT_STRANGERS, not for pairs, only when
    not in a party). Once in, say LEVEL_AFK_TEXT once (at most every LEVEL_AFK_EVERY s); the party is
    left when we move on to another spot / leg, go to reset, Blood Castle, Golden Invasion or a box trade.
  * Accepting another player's party invite (leveling or Blood Castle; not when WE invited):
    PARTY_THANKS_DELAY s later say one of PARTY_THANKS_TEXTS ("thx", "gracias", ...).

Blood Castle (15 minutes before every castle, BC_PLAN_SEC):
  * One character per castle level (BC1..BC7, lower castles first).
  * Already inside a castle's level range -> HOLD: keep farming until BC_FARM_UNTIL_SEC before the
    castle opens (or BC_FARM_TOP_MARGIN levels below the top of the range, whichever comes first),
    then stop, buy the cloak, wait at the bar. Climbers that get into their range early do the same.
    (two of ours in the same range: the lower level one holds, the other climbs)
  * The others CLIMB to the nearest free castle above them that they can reach in time
    (or reset and climb to BC1 when close to the reset level and BC1 is free). Once inside the
    range they stop and hold. Not there 2 minutes before the castle -> skip it, keep leveling.
  * Inside: once another player shows up, say "party?" and wait BC_WAIT_INVITE_SEC for an invite;
    no invite -> invite the players in view who are NOT already in a party (the game's reply to
    an invite tells us), and keep inviting new arrivals. Then walk up the
    corridor, destroy the statue, take the weapon and deliver it (or rush back to the
    Archangel when someone else has it), pick up the Box of Kundun +5, back to leveling.

Leveling pairs (PAIRS, for characters you grand reset by hand):
  * PAIRS = {levelee: leveler}. Until the levelee has ASSIST_UNTIL_RESETS resets, the two farm
    together in their own party at the spot for the LOWER of their levels (the leveler picks and
    switches spots, the levelee follows him), and /reset together once the levelee reaches the reset
    level (the leveler holds at the reset level meanwhile).
  * The levelee skips Blood Castle. The leveler only takes a castle he is already in range of
    (never climbs / resets for one); the levelee waits at the Lorencia bar while he is away,
    paused or offline.
  * A grand reset is noticed when the reset counter drops to 0 (mu_group_bot.py Char.refresh).

Box trading (TRADE_BOXES):
  * An alt carrying Boxes of Kundun (+1..+5) that meets the main character (first account in
    accounts.json) while both are leveling (same map, within TRADE_MEET_RADIUS tiles) asks him for
    a trade right there: both turn the helper off, the alt walks up to the main, sends the request,
    the main accepts only the alt's request, the alt puts every box in and confirms only once the
    MAIN's own trade window shows those boxes (so it can't be trading with a stranger), the main
    confirms, and both go back to leveling at the same spot. One trade at a time; never around Blood
    Castle time. A failed trade is retried later (TRADE_RETRY_SEC), and after TRADE_MAX_FAILS
    failures in a row that alt waits TRADE_GIVEUP_SEC.
  * The main publishes his free bag cells every 30 s. Nobody offers him boxes when he has less than
    TRADE_MIN_FREE free, an alt gives only as many as fit, and a trade that fails although it should
    have fit marks him full until his free space goes up (or TRADE_FULL_SEC passes).

Run:   run_auto.bat           Stop: Ctrl+C in the console window.
       run_auto.bat starts mu_supervisor.py, which runs this bot and restarts it on request
       (RESTART file / restart_bot.bat / R key), after a crash, or when it freezes.
       STOP_BOT file (written by the supervisor) -> the bot closes its browsers and exits.
       Exit codes: 0 = stopped on purpose, 2 = another bot holds the lock, 3 = every character stopped.
Pause: P pauses / resumes EVERY character. Keys 1-5 pause / resume ONE character (order of
       accounts.json). A paused character's window is restored so you can play it; the others
       keep going. While paused, everything you do in that window is written to mu_record.log
       (mu_recorder.py) so new events can be learned from it. Characters in BIG_ON_PAUSE get a
       fullscreen 1920x1200 window with BIG_OPTIONS (100% render scale, effects, 120 fps, sound on)
       while paused (back to 800x500 / Low / sound off on resume).
Options: every account gets GAME_OPTIONS (mu_group_bot.py: W A S D, loot filter, FOV 60, 115%
       interface, sound off, hide "entered the game") and MU Helper on HELPER_KEY.
"""
import math
import os
import random
import re
import socket
import sys
import threading
import time
from datetime import datetime

from playwright.sync_api import sync_playwright, Error as PWError

import mu_group_bot as G
import mu_bc as B
import mu_gi as GI
import mu_bb as BB
import mu_chat as CH
import mu_hub as HUB
import roster as R
from mu_bc import in_bc, dist

# ───────────────────────────── CONFIG ─────────────────────────────
LOG_FILE            = G.BASE_DIR / "mu_auto_bot.log"

# parties while leveling
PARTY_MEET_RADIUS   = 10    # two of ours this close (same map) -> party up
PARTY_APART_RADIUS  = 20    # a party member further than this ...
PARTY_APART_SEC     = 60    # ... for this long (nobody near me) -> I leave the party
PARTY_EVERY         = 2     # seconds between party checks
SEND_AUTOPARTY_OFF  = True  # the group bot turned /autoparty ON for the alts; turn it off once
LEVEL_ACCEPT_STRANGERS = True   # leveling: accept party invites from other players (not while in a party,
                                # not for leveling pairs); the party is left when we move to another spot/map
LEVEL_AFK_TEXT      = "me quedo afk"   # said once after joining a stranger's party ("" = say nothing) ...
LEVEL_AFK_EVERY     = 900       # ... and at most once per this many seconds per character (Juan 2026-10-12)
LEVEL_AFK_PARTY_CHAT = True     # say it in party chat (~); False = normal chat
LEVEL_AFK_DELAY     = (7, 14)   # seconds after joining before saying it (random; after the thanks below)
# thanks after accepting another player's party invite (leveling + Blood Castle; never for our own
# characters' invites, never when we were the ones inviting) (Juan 2026-10-14)
PARTY_THANKS_TEXTS  = ["thx", "gracias", "ty", "gracia"]   # one picked at random ([] = off)
PARTY_THANKS_DELAY  = (2, 5)    # seconds after accepting (random)
PARTY_THANKS_PARTY_CHAT = False # False = normal chat; True = party chat (~)
PARTY_THANKS_MIN_GAP = 60       # at most once per this many seconds per character (kicked + re-invited...)

# spot rules for a character on its own (the group bot's rules for the rest, see mu_group_bot.py)
SOLO_DEATHS_TO_SWITCH = 3   # my deaths at one spot within DEATH_WINDOW_SEC -> switch (not on the best spot)

# leveling pairs (after a grand reset a character can't kill anything: another one carries it)
# levelee: leveler. While the levelee has fewer than ASSIST_UNTIL_RESETS resets:
#   * both farm together (spot picked by the LOWER level of the two; the leveler decides the spot),
#     in their own party, and /reset together once both are at the reset level
#     (the leveler holds at the reset level until the levelee gets there)
#   * the levelee never goes to Blood Castle; the leveler may (only if already in a castle's range,
#     it never climbs or resets for one) and the levelee waits at the Lorencia bar meanwhile
#   * at ASSIST_UNTIL_RESETS resets the pair breaks up by itself; a later grand reset starts it again
PAIRS               = {
    "mogulin": "invalido",
}
ASSIST_UNTIL_RESETS = 5
CAP_FIGHT           = True  # (2026-10-14) since the server update the MU Helper turns off at the reset level and
                            # can't be turned on again until /reset. A paired character holding at that level
                            # (the leveler waiting for its levelee) fights on its own instead: the Blood Castle
                            # no-helper loop (mu_bc.BC_GRAB_JS) in "guard" mode at the spot (hits anything in
                            # skill range, potions on Q, buffs). False = old behaviour (helper, which now fails)
PAIR_LEVELER_GRACE  = 30    # leveler offline (reload / map change) this long -> the levelee waits at the bar
LEVELEE_VIT_MAX     = 1500  # every levelee in PAIRS puts its free points into VIT ("/add vit N") until it has
                            # this much (also after it graduates); above it nothing is added
LEVELEE_VIT_EVERY   = 60    # seconds between checks for free points

# Blood Castle
BC_AUTO             = True    # (Juan 2026-10-14: on by default) Blood Castle on/off AT STARTUP. Press B in the console to switch it live
                              # (off until the bots seem human enough; Juan 2026-10-11)
BC_TOGGLE_KEY       = "b"
BB_AUTO             = False   # (Juan 2026-10-15: OFF by default) Big Boss on/off AT STARTUP (mu_bb.py) ...
BB_TOGGLE_KEY       = "o"     # ... press O in the console to switch it live
BC_TIERS            = B.BC_TIERS        # (castle, min lvl, max lvl): BC1 15-80 ... BC7 331-400
BC_PLAN_SEC         = 900   # make the plan this long before the entry window opens
BC_FARM_UNTIL_SEC   = 180   # holders keep farming until this long before the window opens (:57) ...
BC_DEVIAS_SEC       = 30    # wait at the Lorencia bar with the cloak, warp to the Spirit in Devias this long before
                            # the door opens (was 75; Juan 2026-10-14: we stood outside the castle too long)
BC_FARM_TOP_MARGIN  = 5     # ... or until they are this many levels below the top of the range (Juan 2026-10-11)
BC_CLIMB_DEADLINE   = 120   # a climber not in its range this long before the window opens -> skips it
BC_ENTER_MARGIN     = 3     # climbers stop this many levels inside the range (not right at its edge)
BC_DEFAULT_RATE     = 15    # levels/min assumed when a character's recent rate is unknown
BC_RESET_OVERHEAD   = 1.5   # minutes for /reset + Zen Chests + walking out of town
BC_LOW_RATE         = 25    # levels/min right after a reset (Lorencia)
BC_WAIT_INVITE_SEC  = 8     # inside: after asking "party?", wait this long for an invite before inviting ourselves (was 30; Juan 2026-10-11)
BC_ASK_PARTY_TEXT   = "party?"   # said once in normal chat when the first other player shows up ...
BC_ASK_PARTY        = False      # ... only if True (Juan 2026-10-12: don't say it any more; we still wait, then invite)
BC_WEAPON_TEXT      = ""       # stay quiet when the Archangel's weapon is picked up
                                            # (Juan 2026-10-12); "" = off
BC_INVITE_EVERY     = 10    # seconds between invites to the same player
BC_SHOW_WINDOW_AFTER = 2    # entry dialog not found this many times -> restore the window so you can click
DEVIAS_MIN_LEVEL    = 20    # /move Devias (Spirit of Archangel) needs this level: BC1 entries wait for it
BC_INVITE_MAX       = 2     # inside: invite the same stranger at most this many times
BC_ENTER_FAILS_RELOAD = 4   # this many castle clicks without getting in -> reload + log in again (fixed it live)
BC_ENTER_MAX_RELOADS  = 2   # ... at most this many times per castle

# idle wiggle: while a character just stands around waiting (Lorencia bar before Blood Castle /
# levelee waiting for its leveler, Devias before the door opens, inside the castle by the bridge,
# Golden Invasion waiting spot) it takes a small random step now and then, so it doesn't stand
# frozen on one tile like a bot (Juan 2026-10-11)
IDLE_WIGGLE         = True
IDLE_WIGGLE_EVERY   = (15, 40)  # seconds between steps (random in this range)
IDLE_WIGGLE_STEP    = (1, 1) # tiles per step (random in this range; (1, 1) = always 1)
IDLE_WIGGLE_LEASH   = 2         # never further than this many tiles from the waiting spot
IDLE_WIGGLE_DOOR_SEC = 15       # Devias: stop wiggling and stand on the spot this long before the door opens

# windows
WINDOW_CHECK_EVERY  = 30    # seconds between window checks
BIG_ON_PAUSE        = {"wizzardo"}  # these characters get a maximized, sharp window when you pause
                                    # them with 1-5 (back to 800x500 / low graphics on resume)
BIG_RENDER_SCALE    = 100   # Render scale (%) while big; None = leave it as the bot set it
BIG_FULLSCREEN      = True  # True = fullscreen window while big (no browser bars); False = maximized
BIG_RESOLUTION      = (1920, 1200)  # game size while big; capped to what the screen fits
# Game options while big (Juan 2026-10-14); index meanings are listed above GAME_OPTIONS in
# mu_group_bot.py. On resume the Low preset + GAME_OPTIONS come back (sound off again).
BIG_OPTIONS = {
    "otherPlayerItemEffects": True,   # World and effects: other players' gear effects ON
    "otherPlayerSkillEffects": True,  # World and effects: other players' skill effects ON
    "renderDistance": 1,              # World and effects: render distance 80
    "fpsLimit": 4,                    # FPS limit 120
    "volume": 5,                      # Sound: master volume 5
    "musicVolume": 1,                 # Sound: music 1
}
REMINIMIZE_RESTORED = False # True = re-minimize any window that got restored (the old behaviour).
                            # False = a window you restore by hand stays open until you minimize it again

# who sent the incoming trade request: "alt", "other" (a name/netId of someone else in view) or "unknown"
TRADE_FROM_JS = """
([alt, ours]) => {
  const r = window.__eco.tradeRequest; if (!r) return {from: 'none'};
  let raw = ''; try { raw = JSON.stringify(r); } catch (e) { raw = String(r); }
  const ents = window.__world.netObjsQuery.entities.filter(e => e.objectNameInWorld && !e.localPlayer);
  const vals = new Set(); const walk = (o, d) => { if (o == null || d > 2) return;
    if (typeof o !== 'object') { vals.add(o); return; } for (const k in o) walk(o[k], d + 1); };
  walk(r, 0);
  const hit = e => vals.has(e.objectNameInWorld) || (e.netId != null && vals.has(e.netId));
  if ([...vals].includes(alt) || ents.some(e => e.objectNameInWorld === alt && hit(e))) return {from: 'alt', raw};
  if (ents.some(e => !ours.includes(e.objectNameInWorld) && !e.npcType && hit(e))) return {from: 'other', raw};
  return {from: 'unknown', raw};
}
"""

# what the other side put in my trade window: Kundun boxes / anything else
TRADE_YOURS_JS = """
(minLvl) => { const it = Object.values(window.__eco.yourTradeItems || {}).filter(Boolean);
  const g = i => i.item || i;   // an item whose fields we can't read counts as a box (not as "other")
  const other = i => g(i).group !== undefined && !(g(i).group === 14 && g(i).num === 11 && (g(i).lvl || 0) >= minLvl);
  return {boxes: it.filter(i => !other(i)).length, other: it.filter(other).length}; }
"""

# alts give their Boxes of Kundun to the main character when they meet while leveling
TRADE_BOXES         = True
TRADE_TO            = None  # character that receives the boxes; None = first account in accounts.json
TRADE_MEET_RADIUS   = 15    # same map, this close, both leveling -> trade
TRADE_NEAR          = 3     # the alt walks this close to the main (where he is leveling) and trades there
TRADE_TIMEOUT       = 60    # seconds for the whole thing before giving up
TRADE_OFFLINE_GRACE = 10    # the other side may look offline this long (map loading) before we give up
TRADE_RETRY_SEC     = 600   # a failed trade: that alt tries again after this long
TRADE_MAX_FAILS     = 3     # ... and after this many failures in a row ...
TRADE_GIVEUP_SEC    = 3600  # ... it waits this long
TRADE_MIN_FREE      = 1     # the main needs at least this many free bag cells, or nobody offers him boxes
TRADE_FULL_SEC      = 7200  # a trade the main failed to take while his bag looked big enough -> treat him
                            # as full until his free space goes up (or this long passes)
TRADE_BC_QUIET      = 180   # no new trades from BC_PLAN_SEC + this before a castle opens until the door closes

# restarts (mu_supervisor.py)
SUPERVISED          = os.environ.get("MU_SUPERVISED") == "1"   # started by mu_supervisor.py
STOP_BOT_FILE       = G.BASE_DIR / "STOP_BOT"   # supervisor -> bot: close the browsers and exit
RESTART_FILE        = G.BASE_DIR / "RESTART"    # bot/you -> supervisor: restart ("now" inside = right away)
RESTART_KEY         = "r"                       # in the console: restart right away (under the supervisor)
EXIT_STOPPED, EXIT_OTHER_BOT, EXIT_ALL_DEAD = 0, 2, 3
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


def bc_entry_level(tier):
    """Level a climber stops at: a few levels inside the range, and never below the level
    /move Devias needs (BC1 starts at 15 but Devias needs 20: four BC1 runs were lost to that)."""
    _, lo, hi = BC_TIERS[tier - 1]
    return min(max(lo + BC_ENTER_MARGIN, DEVIAS_MIN_LEVEL), hi)


def stats_alias(alias, in_party):
    return alias if in_party else f"{alias}/solo"


class Hub:
    """What the characters know about each other (each thread only touches its own browser)."""

    def __init__(self, names):
        self.lock = threading.RLock()   # re-entrant: a thread that already holds it can call snapshot() etc. without freezing everyone
        self.names = names                       # characters THIS machine runs (accounts.json order)
        self.friends = R.friends(names)          # + our characters on other machines (roster.json)
        self.link = None                         # mu_hub.HubLink when hub.json exists (other machines)
        self.info = {}
        self.stats = SharedStats(G.BASE_DIR / G.STATS_FILE)
        self.reset_level = G.RESET_LEVEL
        self.detecting = False
        self.open_at = None          # next Blood Castle entry window (epoch seconds)
        self.plan = None             # {"key", "open_at", "assign": {name: (tier, role)}, "started", "out"}
        self.trade = None            # box trade in progress: {"alt", "lead", "phase", "start", "phase_at", "n", ...}
        self.trade_to = TRADE_TO or (names[0] if names else None)
        self.lead_full = None        # {"free", "at"}: the main couldn't take boxes -> no trades until he has more room
        self.pair_round = set()      # paired characters that /reset now (the whole pair reached the reset level)
        self.pair_deaths = {}        # leveler -> [(time, name)] deaths of its levelees (for its spot switching)
        self.paused = set()
        self.pause_all = False       # P / PAUSE file: everyone paused, windows stay minimized
        self.bc_on = BC_AUTO         # B in the console switches Blood Castle on/off
        self.bb_on = BB_AUTO         # O in the console switches the Big Boss on/off
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
        self.stranger_party = None   # leveling in a stranger's party: {"area", "joined", "say_at", "said"}
        self.afk_said_at = 0         # last time LEVEL_AFK_TEXT was said
        self.thanks = None           # pending thanks after accepting a stranger's invite: {"at", "until", "who"}
        self.thanks_said_at = 0
        self.thanks_last = None      # last text used (not twice in a row)
        self.bcs = None              # Blood Castle state while this character is assigned to one
        self.plan_seen = None
        self.paused = False
        self.show_window = False     # keep the window restored (paused / you have to click something)
        self.autoparty_off = False
        self.last_status = 0
        self.mode = "start"
        self.anchor_name = None
        self.win_check = 0
        self.user_opened = False     # you restored this window by hand: keep_minimized leaves it open
        self.big = False             # window maximized for manual play (BIG_ON_PAUSE)
        self.msgwin_at = 0
        self.helper_press_at = 0     # last non-blocking MU Helper key press (helper_async)
        self.cap_fight = {}          # in-page fight loop while holding at the reset level (CAP_FIGHT)
        self.cap_armed = False       # armed it during this tick
        self.open_logged = None
        self.npc_talk_since = 0      # when the Archangel's dialog was first seen open
        self.boxes = 0               # Boxes of Kundun (+1..+5) in my bag (checked every 30 s)
        self.bag_free = None         # main only: free bag cells (checked every 30 s)
        self.bag_free_at = 0
        self.bag_keys_logged = False
        self.bag_low_logged = False
        self.boxes_at = 0
        self.my_trade = None
        self.trade_next = 0
        self.trade_fails = 0
        self.trade_sent = 0
        self.trade_filled = False
        self.leveler_name = PAIRS.get(self.name)    # who carries me after a grand reset (None = nobody)
        self.was_assisted = None
        self.pair_parked = False     # levelee waiting at the bar (leveler in Blood Castle / paused / offline)
        self.leveler_off_since = None
        self.vit_check = 0
        self.vit_fails = 0
        self.wig_home = None         # waiting spot I'm idle-wiggling around: (map, x, y)
        self.wig_next = 0
        # Golden Invasion hunter (mu_gi.py): GI.GI_CHARS + every character with max stats (checked live)
        self.gi = GI.GoldenInvasion(self) if GI.GI_ENABLED else None
        # Big Boss (mu_bb.py): every available character goes, one party of ours, back to normal after
        self.bb = BB.BigBoss(self) if BB.BB_ENABLED else None
        # Chat replies to other players (mu_chat.py; dry run = log only, see CH.CHAT_SEND)
        self.chat = CH.ChatAgent(self, hub.chat) if getattr(hub, "chat", None) else None

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
                G.prepare_context(ctx)     # GAME_OPTIONS + MU Helper key, before the game loads
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
                    time.sleep((self.bb.sleep() if self.bb else None) or (self.gi.sleep() if self.gi else None)
                               or self.bc_sleep() or G.TICK)   # 4x/s during the invasion / inside BC
                try:
                    ctx.close()
                except Exception:
                    pass
        except Exception as e:
            self.log(f"thread stopped: {e!r}")
        self.hub.put(self.name, {"name": self.name, "online": False, "paused": False, "mode": "stopped",
                                 "level": None})

    def window_state(self):
        try:
            cdp = self.c.page.context.new_cdp_session(self.c.page)
            win = cdp.send("Browser.getWindowForTarget")["windowId"]
            st = cdp.send("Browser.getWindowBounds", {"windowId": win})["bounds"].get("windowState")
            cdp.detach()
            return st
        except Exception:
            return None

    def go_big(self):
        """Fullscreen (or maximized) window with the game at BIG_RESOLUTION and the BIG_OPTIONS
        quality / sound settings, for playing it yourself."""
        c, page = self.c, self.c.page
        self.big = True
        try:
            cdp = page.context.new_cdp_session(page)
            win = cdp.send("Browser.getWindowForTarget")["windowId"]
            cdp.send("Browser.setWindowBounds", {"windowId": win, "bounds": {"windowState": "normal"}})
            time.sleep(0.5)
            b = cdp.send("Browser.getWindowBounds", {"windowId": win})["bounds"]
            vp = page.viewport_size or {"width": G.WINDOW_SIZE[0], "height": G.WINDOW_SIZE[1]}
            extra_w, extra_h = b["width"] - vp["width"], b["height"] - vp["height"]   # borders + toolbar
            state = "fullscreen" if BIG_FULLSCREEN else "maximized"
            cdp.send("Browser.setWindowBounds", {"windowId": win, "bounds": {"windowState": state}})
            time.sleep(0.8)
            b = cdp.send("Browser.getWindowBounds", {"windowId": win})["bounds"]
            cdp.detach()
            c.minimized = False
            if state == "fullscreen":
                w, h = b["width"], b["height"]          # no browser bars in fullscreen
            else:
                w, h = b["width"] - extra_w, b["height"] - extra_h
            if BIG_RESOLUTION:
                w, h = min(w, BIG_RESOLUTION[0]), min(h, BIG_RESOLUTION[1])
            w, h = max(800, w), max(500, h)
            page.set_viewport_size({"width": w, "height": h})
            self.log(f"window {state} for manual play ({w}x{h})")
        except Exception as e:
            self.log(f"could not enlarge the window: {e!r}")
        vals = dict(BIG_OPTIONS)
        if BIG_RENDER_SCALE:
            vals["renderScale"] = G.render_scale_index(BIG_RENDER_SCALE)
        if not c.set_options(vals, "manual-play options") and BIG_RENDER_SCALE:
            self.options_render_scale(BIG_RENDER_SCALE)

    def go_small(self):
        """Back to the bot's 800x500 window, low graphics and sound off (then it gets minimized as usual)."""
        c, page = self.c, self.c.page
        self.big = False
        try:
            page.set_viewport_size({"width": G.WINDOW_SIZE[0], "height": G.WINDOW_SIZE[1]})
            c.set_window("normal")      # un-maximize; the main tick minimizes it again
            self.user_opened = False
        except Exception as e:
            self.log(f"could not shrink the window: {e!r}")
        c.gfx_done = False              # main tick re-applies the Low preset + bot render scale + GAME_OPTIONS

    def options_render_scale(self, value):
        page = self.c.page
        try:
            page.evaluate("() => { window.__store.optionsEnabled = true; }")
            time.sleep(1.0)
            page.get_by_text("Quality", exact=True).first.click()
            time.sleep(0.5)
            self.c.set_render_scale(value)
        except Exception as e:
            self.log(f"render scale error: {e!r}")
        finally:
            try:
                close = page.get_by_text("Close", exact=True)
                if close.count():
                    close.first.click()
                page.evaluate("() => { window.__store.optionsEnabled = false; }")
            except Exception:
                pass

    def keep_minimized(self):
        """Windows get restored behind the bot's back (Windows focus, a reload, a click on the
        taskbar...). Every WINDOW_CHECK_EVERY seconds: not paused with 1-5 / no click needed ->
        minimize it again. With REMINIMIZE_RESTORED off, a window you restored is left open until
        you minimize it yourself."""
        if not G.MINIMIZE_WINDOWS or G.HEADLESS or self.show_window:
            return
        now = time.time()
        if now - self.win_check < WINDOW_CHECK_EVERY:
            return
        self.win_check = now
        st = self.window_state()
        if not REMINIMIZE_RESTORED:
            if st and st != "minimized" and not self.user_opened:
                self.user_opened = True
                self.log(f"window restored by hand ({st}) -> leaving it open (minimize it to hand it back to the bot)")
            elif st == "minimized" and self.user_opened:
                self.user_opened = False
                self.log("window minimized again -> bot keeps it minimized")
            return
        if st and st != "minimized":
            self.log(f"window was {st} -> minimizing it again")
            self.c.set_window("minimized")

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
            sp = G.ROUTE[self.step_key][4][self.spot_idx]
            target = (sp.map,) + tuple(sp)
        bc_as = ((self.hub.plan or {}).get("assign") or {}).get(self.name) or (None, None)
        self.hub.put(self.name, {
            "name": self.name, "online": bool(c.online), "paused": self.paused, "mode": self.mode,
            "map": s.get("map"), "x": s.get("x"), "y": s.get("y"), "level": s.get("level"),
            "resets": s.get("resets"), "hp": s.get("hp"), "party": list(s.get("party") or []),
            "step": self.step_key, "spot": None if self.scouting else self.spot_idx,
            "scouting": self.scouting, "step_since": self.step_since, "arrived": self.arrived,
            "target": target, "rate": self.recent_rate(),
            "bc": self.bcs is not None,
            "bc_away": self.bcs is not None and self.bcs.get("phase") not in ("climb", "farm"), "boxes": self.boxes, "bag_free": self.bag_free,
            "assisted": self.assisted(), "leveler": self.leveler_name if self.assisted() else None,
            "pair_lead": bool(self.my_levelees()), "parked": self.pair_parked,
            "gi_hunter": bool(self.gi and self.gi.is_hunter()),
            "bc_tier": bc_as[0], "bc_role": bc_as[1],     # shared with the other machines (mu_hub)
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
        self.cap_armed = False
        try:
            return self._tick()
        finally:
            # left the reset-level fight (reset, Blood Castle, Golden Invasion, pause, trade...) -> stop it
            if self.cap_fight.get("grab_on") and not self.cap_armed:
                try:
                    bc_owns = bool((self.bcs or {}).get("grab_on"))   # BC re-armed the same loop: leave it
                    self.cap_fight_off(f"now {self.mode}", send=not bc_owns)
                except Exception as e:
                    self.log(f"cap fight off: {e!r}")

    def _tick(self):
        c, hub = self.c, self.hub
        if c.page.is_closed():
            raise RuntimeError("has been closed")
        want_pause = self.name in hub.paused
        if want_pause != self.paused:
            self.pause() if want_pause else self.resume()
        if self.paused:
            self.mode = "paused"
            c.refresh()
            c.rec.tick()
            self.keep_minimized()
            self.publish()
            return
        c.refresh()
        if c.online:
            c.last_ingame = time.time()
            c.was_online = True
            c.select_tries = 0
            self.check_msgwin()
            if TRADE_BOXES and self.name == hub.trade_to:
                self.update_bag_free()
            if not c.gfx_done:
                c.set_low_graphics()
            elif G.MINIMIZE_WINDOWS and not c.minimized and not G.HEADLESS and not self.show_window:
                c.set_window("minimized")
            else:
                self.keep_minimized()
        else:
            if c.was_online:
                c.last_ingame = time.time()
                c.was_online = False
            # A reload / re-login restores the window on its own. Unless you had opened it by hand,
            # forget that it was minimized so the bot minimizes it again once back in game
            # (instead of keep_minimized mistaking it for a window you restored yourself).
            if not self.user_opened:
                c.minimized = False
            self.mode = "offline"
            self.publish()
            c.handle_offline()
            return
        s = c.s
        now = time.time()
        if self.chat:                       # before everything else: also answers during Blood Castle
            try:
                self.chat.tick()
            except Exception as e:
                self.log(f"chat error: {e!r}")
        self.thanks_tick()                  # any mode: leveling, Blood Castle, Golden Invasion...
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
        self.pair_check(hub.snapshot())
        if lvl < hub.reset_level:
            self.hold_logged = False
            if self.name in hub.pair_round:
                with hub.lock:
                    hub.pair_round.discard(self.name)    # my part of the pair's reset round is done
        self.read_bc_timer()                    # always: so switching BC on mid-hour works at once
        if self.bb and self.bb.tick():          # Big Boss: drops everything else (not inside a Blood Castle)
            self.publish()
            return
        if self.gi and self.gi.tick():          # Golden Invasion (:49 -> :53) comes first
            self.publish()
            return
        if self.bc_tick():
            self.publish()
            return
        if self.trade_tick():
            if self.stranger_party and self.my_trade and self.name == self.my_trade["alt"]:
                self.leave_party("walking over to trade boxes")
            self.publish()
            if now - self.last_status > 60:
                self.last_status = now
                self.status()
            return
        self.add_vit()
        if lvl >= hub.reset_level and self.bb and self.bb.hold_reset():
            if not getattr(self, "bb_hold_logged", False):
                self.bb_hold_logged = True
                self.log("reset level, but the Big Boss is close (or up) -> no /reset until it's over "
                         "(a reset would put me below the Tarkan /move level)")
        elif lvl >= hub.reset_level and self.pair_may_reset():
            self.bb_hold_logged = False
            self.mode = "reset"
            self.publish()
            return self.reset_tick()
        if c.shop_pending:
            self.mode = "shop"
            self.publish()
            if c.shop_tick():
                c.shop_pending = False
            return
        if self.pair_wait_tick():
            if self.stranger_party:
                self.leave_party("leaving the spot")
            self.publish()
            if now - self.last_status > 60:
                self.last_status = now
                self.status()
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
        if self.my_trade:
            t = self.my_trade
            bc += f" | box trade {t['alt']} -> {t['lead']} ({t['phase']})"
        elif self.boxes:
            bc += f" | {self.boxes} Kundun box(es)"
        if self.assisted():
            bc += (f" | carried by {self.leveler_name} (r{self.c.known[0]}/{ASSIST_UNTIL_RESETS})"
                   + (" - waiting at the bar" if self.pair_parked else ""))
        elif getattr(self, "was_levelees", None):
            bc += f" | carrying {', '.join(self.was_levelees)}"
        if self.name in PAIRS:
            bc += f" | VIT {s.get('vit')} ({s.get('points')} free pts)"
        if self.cap_fight.get("grab_on"):
            bc += " | fighting without helper (reset level)"
        if self.name == self.hub.trade_to and self.bag_free is not None:
            bc += f" | bag {self.bag_free} free" + (" (FULL for trades)" if self.hub.lead_full else "")
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
        self.show_window = not self.hub.pause_all    # P = everyone: windows stay minimized
        c = self.c
        self.log(f"=== PAUSED (press {self.idx + 1} again to resume this character) ===")
        try:
            if c.minimized and self.show_window:
                c.set_window("normal")
            if G.HELPERS_OFF_ON_PAUSE and c.refresh().get("helper"):
                c.set_helper(False)
        except Exception as e:
            self.log(f"pause: {e!r}")
        if self.show_window and self.name.lower() in {n.lower() for n in BIG_ON_PAUSE} and not G.HEADLESS:
            self.go_big()
        c.rec.start()               # write down what you do while you play it (mu_recorder.py)
        if self.gi:
            self.gi.on_pause()          # the in-page fight loop must not fight while you play
        if self.bb:
            self.bb.on_pause()          # same for the Big Boss loop (it switches back on when you resume)

    def resume(self):
        self.paused = False
        self.show_window = False
        c = self.c
        c.rec.stop()
        if self.big:
            self.go_small()
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
                and i.get("step") == self.step_key and not i.get("paused")
                and not i.get("assisted") and not i.get("pair_lead")]   # pairs farm on their own

    def find_anchor(self):
        """The first of ours who came to this leg (decides the spot); None if that's me."""
        earlier = [p for p in self.peers_on_step()
                   if (p.get("step_since", 0), p["name"]) < (self.step_since, self.name)]
        return min(earlier, key=lambda p: (p.get("step_since", 0), p["name"])) if earlier else None

    def level_tick(self):
        c, s = self.c, self.c.s
        lvl = s.get("level") or 1
        snap = self.hub.snapshot()
        grp = self.pair_group(snap)
        levelee = bool(grp) and grp[0] != self.name
        leg_lvl = lvl
        if grp:
            # the pair farms where its LOWEST level can go (so the /move level limits fit both)
            others = [(snap.get(n) or {}).get("level") for n in [grp[0]] + grp[1] if n != self.name]
            leg_lvl = min([lvl] + [l for l in others if l])
            if not levelee:
                with self.hub.lock:
                    self.deaths += self.hub.pair_deaths.pop(self.name, [])   # my levelees' deaths count too

        def anchor_now():
            if not grp:
                return self.find_anchor()
            if not levelee:
                return None                  # the leveler decides the pair's spot
            L = snap.get(grp[0]) or {}
            if L.get("online") and not L.get("paused") and L.get("mode") == "level" and L.get("step") == self.step_key:
                return L
            return None

        key = next((i for i, st in enumerate(G.ROUTE) if st[0] <= leg_lvl < st[1]), len(G.ROUTE) - 1)
        frm, to, alias, map_idx, spots = G.ROUTE[key]
        if key != self.step_key:
            self.step_key, self.step_since = key, time.time()
            self.scouting = False
            self.spot_idx = self.hub.stats.ranked(stats_alias(alias, False), spots)[0]
            self.new_spot()
            anchor = anchor_now()
            if anchor:
                self.log(f"{alias}: {anchor['name']} is already here -> going to the same spot")
            elif G.SCOUT_ON_NEW_ZONE and len(spots) > 1 and not levelee:
                self.start_scouting(alias, spots)
        anchor = anchor_now()
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
        step = G.spot_step(G.ROUTE[key], self.spot_idx)     # alias / map of THIS spot (Arena4 spots in the Arena3 leg)

        dead = (s.get("hp") or 0) <= 0
        if dead and not c.was_dead:
            self.deaths.append((time.time(), self.name))
            self.log(f"died at {alias} {spots[self.spot_idx]}")
            if levelee:
                with self.hub.lock:
                    self.hub.pair_deaths.setdefault(grp[0], []).append((time.time(), self.name))
        c.was_dead = dead
        if dead:
            c.walk_target = None
            return

        if self.scouting:
            return self.scout_tick(step, spots)
        follow = None
        if anchor and anchor.get("map") == step[3] and (anchor.get("hp") or 0) > 0 and anchor.get("x") is not None \
                and math.hypot(anchor["x"] - step[4], anchor["y"] - step[5]) <= G.LEASH_RADIUS:
            follow = (anchor["x"], anchor["y"])
        if CAP_FIGHT and self.hub.reset_level and lvl >= self.hub.reset_level:
            self.cap_fight_tick(step, lvl)       # holding for the pair's reset: no MU Helper any more
        else:
            if self.cap_fight.get("grab_on"):
                self.cap_fight_off(f"lvl {lvl}")
            c.move_tick(step, follow=follow)
        self.check_spot(step, spots, is_anchor=anchor is None and not levelee)

    def cap_fight_tick(self, step, lvl):
        """At the reset level the server keeps the MU Helper off until /reset (2026-10-14). A paired
        character holding there (leveler waiting for its levelee) walks to the spot as usual (helper
        off) and then fights with the in-page loop in guard mode: anything in skill range, back to the
        spot if pushed off, potions, buffs."""
        c, s = self.c, self.c.s
        _, _, alias, map_idx, tx, ty = step
        f = self.cap_fight
        on = bool(f.get("grab_on"))
        d = (math.hypot(s["x"] - tx, s["y"] - ty)
             if s.get("map") == map_idx and s.get("x") is not None else 999)
        if d > (G.LEASH_RADIUS if on else G.ARRIVE_RADIUS):
            if on:
                self.cap_fight_off(f"{d:.0f} tiles from the spot -> walking back")
            c.move_tick(step)                    # warp / walk; never within ARRIVE_RADIUS here, so no helper ON
            return
        if s.get("helper"):
            self.helper_async(False)
        if not on:
            self.log(f"lvl {lvl}: MU Helper stays off until the reset -> fighting myself at {alias} ({tx},{ty})")
            c.walk_target, c.relocating = None, False
        r = self.grab(f, True, goal=(tx, ty), goal_r=G.ARRIVE_RADIUS, mode="guard", weapon=False,
                      statue=False, fight=True, zone=False, tag="CAP")
        self.fight_log(f, r, tag="CAP fight")
        self.cap_armed = True

    def cap_fight_off(self, why, send=True):
        f = self.cap_fight
        if not f.get("grab_on"):
            return
        if send:
            self.grab(f, False, fight=True, zone=False, tag="CAP")
        f["grab_on"] = False
        self.log(f"CAP: fight loop off ({why})")

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
        si = sc["order"][sc["i"]]
        map_idx, warp = spots[si].map, spots[si].alias     # the spot being checked may be on another map
        if s.get("map") != map_idx:
            if now - c.last_warp > 15:
                c.set_helper(False)
                c.chat(f"/move {warp}")
                c.last_warp = now
            sc["started"], sc["arrived_at"], sc["best"], sc["progress_at"] = now, None, 999.0, now
            return
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
        crowd = self.ev(G.CROWD_JS, [tx, ty, G.CROWD_RADIUS, self.hub.friends]) or []
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
        # at the reset level I gain no EXP: no rate samples (they'd save 0 lvl/min for the spot) and no
        # rate-based switching (it would always look dead); deaths still count
        capped = bool(self.hub.reset_level) and lvl >= self.hub.reset_level
        if capped:
            self.samples, self.stat_point = [], None
        elif now - self.spot_since >= 60:
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
        if capped:
            return
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

    # ── leveling pairs (PAIRS) ──
    def assisted(self):
        """I'm a levelee that still needs carrying (fewer than ASSIST_UNTIL_RESETS resets).
        Uses the last reset count seen in game, so a reload doesn't break the pair up."""
        if not self.leveler_name or self.leveler_name not in self.hub.names or self.c is None:
            return False
        rs = self.c.known[0]
        return 0 <= rs < ASSIST_UNTIL_RESETS

    def my_levelees(self, snap=None):
        """Characters I'm carrying right now (not paused, running)."""
        snap = snap if snap is not None else self.hub.snapshot()
        return sorted(n for n, i in snap.items() if n != self.name and i.get("assisted")
                      and i.get("leveler") == self.name and not i.get("paused") and i.get("mode") != "stopped")

    def pair_group(self, snap):
        """(leveler, [levelees]) when I'm in an active pair, else None."""
        if self.assisted():
            L = snap.get(self.leveler_name) or {}
            if L.get("assisted"):
                return None              # my leveler needs carrying himself: config mistake, level alone
            others = [n for n, i in snap.items() if n != self.name and i.get("assisted")
                      and i.get("leveler") == self.leveler_name and not i.get("paused") and i.get("mode") != "stopped"]
            return self.leveler_name, sorted(others + [self.name])
        lev = self.my_levelees(snap)
        return (self.name, lev) if lev else None

    def pair_check(self, snap):
        """Logs when a pair starts / ends (grand reset done, graduated, partner paused...)."""
        a = self.assisted()
        if a != self.was_assisted:
            rs = self.c.known[0]
            if a:
                self.log(f"PAIR: {self.leveler_name} levels me until {ASSIST_UNTIL_RESETS} resets (now r{rs}); "
                         "no Blood Castle until then")
            elif self.was_assisted is not None:
                self.log(f"PAIR: reached {rs} resets -> leveling on my own, Blood Castle from now on")
            self.was_assisted = a
            self.step_key = None
            self.pair_parked = False
        lev = self.my_levelees(snap)
        if lev != getattr(self, "was_levelees", []):
            self.log(f"PAIR: carrying {', '.join(lev)}" if lev else "PAIR: nobody to carry -> leveling on my own")
            self.was_levelees = lev
            self.step_key = None

    def pair_may_reset(self):
        """Paired characters /reset together: a round starts when every levelee is at the reset
        level (the leveler joins it if he is there too; otherwise he keeps going). Outside a round a
        paired character at the reset level holds at the spot."""
        snap = self.hub.snapshot()
        grp = self.pair_group(snap)
        if not grp:
            return True
        leveler, levelees = grp
        rl = self.hub.reset_level
        lv = lambda n: (self.c.s.get("level") or 0) if n == self.name else ((snap.get(n) or {}).get("level") or 0)
        started = None
        with self.hub.lock:
            if self.name not in self.hub.pair_round and all(lv(n) >= rl for n in levelees):
                started = set(levelees) | ({leveler} if lv(leveler) >= rl else set())
                self.hub.pair_round |= started
            ok = self.name in self.hub.pair_round
        if started:
            self.log(f"PAIR: {', '.join(levelees)} at {rl} -> resetting: {', '.join(sorted(started))}")
        elif not ok and not getattr(self, "hold_logged", False):
            self.hold_logged = True
            self.log(f"PAIR: at {rl}, holding until {', '.join(levelees)} get{'s' if len(levelees) == 1 else ''} there")
        return ok

    def add_vit(self):
        """Levelees in PAIRS (grand-reset characters): every LEVELEE_VIT_EVERY s, free level-up points
        -> VIT until VIT reaches LEVELEE_VIT_MAX. Carried or not (also after graduating)."""
        now = time.time()
        if self.name not in PAIRS or now < self.vit_check:
            return
        self.vit_check = now + LEVELEE_VIT_EVERY
        c, s = self.c, self.c.s
        vit, pts = s.get("vit"), s.get("points")
        if vit is None or not pts or pts <= 0 or vit >= LEVELEE_VIT_MAX or (s.get("hp") or 0) <= 0:
            return
        n = int(min(pts, LEVELEE_VIT_MAX - vit))
        c.chat(f"/add vit {n}")
        time.sleep(1.5)
        after = c.refresh().get("vit")
        if after is not None and after > vit:
            self.vit_fails = 0
            self.log(f"VIT {vit} -> {after} (+{after - vit} of {pts} free points, up to {LEVELEE_VIT_MAX})")
            return
        self.vit_fails += 1
        lines = self.ev(G.CHAT_LINES_JS) or []
        self.log(f"/add vit {n} did not raise VIT ({vit}) - server: {' | '.join(lines[-2:])}")
        if self.vit_fails >= 3:
            self.vit_fails = 0
            self.vit_check = now + 600
            self.log("VIT: 3 failures in a row -> trying again in 10 min")

    def pair_wait_tick(self):
        """Levelee whose leveler can't carry it (Blood Castle, paused, offline): wait at the bar."""
        if not self.assisted():
            self.pair_parked = False
            return False
        L = self.hub.snapshot().get(self.leveler_name) or {}
        now = time.time()
        why = None
        if L.get("online"):
            self.leveler_off_since = None
        if not L or L.get("mode") == "stopped":
            why = "is not running"
        elif L.get("paused"):
            why = "is paused"
        elif L.get("bc_away"):
            why = "is busy with Blood Castle"
        elif L.get("mode") == "gi":
            why = "is hunting the Golden Invasion"
        elif L.get("mode") == "bb":
            why = "is at the Big Boss"
        elif not L.get("online"):
            self.leveler_off_since = self.leveler_off_since or now
            if now - self.leveler_off_since > PAIR_LEVELER_GRACE:
                why = "is offline"
        c, s = self.c, self.c.s
        if why is None:
            if self.pair_parked:
                self.pair_parked = False
                self.step_key, c.walk_target, c.spot = None, None, None
                self.log(f"PAIR: {self.leveler_name} is back -> leveling with him again")
            return False
        if not self.pair_parked:
            self.pair_parked = True
            self.log(f"PAIR: {self.leveler_name} {why} -> waiting at the Lorencia bar")
        self.mode = "wait"
        if (s.get("hp") or 0) <= 0:
            c.walk_target = None
            return True
        m, x, y = B.BAR_SPOT
        if self.park(m, "Lorencia", x, y, radius=3):
            c.set_helper(False)
        return True

    def pair_party_tick(self, grp, snap):
        """The pair's own party: the leveler invites his levelees, levelees accept only him,
        nobody else gets in."""
        leveler, levelees = grp
        c, s = self.c, self.c.s
        party = s.get("party") or []
        mine = {leveler, *levelees}
        now = time.time()
        if s.get("partyRequest"):
            text = s.get("partyRequestText", "")
            if self.name != leveler and len(party) <= 1 and f'"{leveler}"' in text:
                self.log(f"accepting {leveler}'s party invite")
                self.ev("() => window.__social.partyRespond(true)")
            else:
                self.ev("() => window.__social.partyRespond(false)")
        if len(party) > 1:
            if any(n not in mine for n in party):
                return self.leave_party("pair: someone else is in it")
            if self.name != leveler and leveler not in party:
                return self.leave_party(f"pair: {leveler} is not in it")
        if self.name != leveler or len(party) >= 5:
            return
        for n in levelees:
            o = snap.get(n) or {}
            if n in party or not o.get("online") or o.get("paused") or o.get("parked"):
                continue
            if o.get("party") and len(o["party"]) > 1:
                continue           # he leaves that party on his own first
            if o.get("map") != s.get("map") or o.get("x") is None or \
                    math.hypot(o["x"] - s["x"], o["y"] - s["y"]) > PARTY_MEET_RADIUS:
                continue
            if now - self.invited.get(n, 0) < G.INVITE_EVERY:
                continue
            self.invited[n] = now
            self.log(f"pair: party invite {n}: {self.ev(G.INVITE_JS, n)}")

    # ── thanks after accepting another player's invite ──
    def invite_from_ours(self, text):
        return any(f'"{n}"' in text for n in self.hub.friends if n != self.name)

    def queue_thanks(self, text):
        """Just accepted a party invite: if it came from another player, say thanks in a few seconds."""
        if not PARTY_THANKS_TEXTS or self.invite_from_ours(text):
            return
        now = time.time()
        if now - self.thanks_said_at < PARTY_THANKS_MIN_GAP:
            return
        at = now + random.uniform(*PARTY_THANKS_DELAY)
        self.thanks = {"at": at, "until": at + 8, "who": text[:60]}

    def thanks_tick(self):
        t = self.thanks
        if not t:
            return
        now = time.time()
        if now < t["at"]:
            return
        if len(self.c.s.get("party") or []) <= 1:
            if now > t["until"]:             # never got in (invite expired / party full / broke up)
                self.thanks = None
            return
        self.thanks = None
        self.thanks_said_at = now
        opts = [x for x in PARTY_THANKS_TEXTS if x != self.thanks_last] or PARTY_THANKS_TEXTS
        msg = random.choice(opts)
        self.thanks_last = msg
        self.log(f"joined a party ({t['who']}) -> saying '{msg}'")
        self.c.chat(("~" if PARTY_THANKS_PARTY_CHAT else "") + msg)

    # ── parties while leveling ──
    def leave_party(self, why):
        party = self.c.s.get("party") or []
        if len(party) > 1:
            self.log(f"leaving party {party} ({why})")
            self.ev("() => window.__social.partyLeave()")
            self.alone_since = None
        self.stranger_party = None

    def stranger_party_tick(self, party, now):
        """In a stranger's party while leveling: say LEVEL_AFK_TEXT once (rate limited), and leave
        as soon as we move to another spot / leg of the route."""
        sp = self.stranger_party
        area = (self.step_key, self.spot_idx)
        if sp is None:                   # joined without us seeing the accept (e.g. after a restart)
            sp = self.stranger_party = {"area": area, "joined": now, "said": True, "say_at": 0}
            self.log(f"in a stranger's party {party} -> staying while we farm here")
        if sp["area"] != area:
            return self.leave_party("moving on to another area")
        if not sp["said"] and now >= sp["say_at"]:
            sp["said"] = True            # once per accepted invite, even if the rate limit skips it
            if LEVEL_AFK_TEXT and now - self.afk_said_at >= LEVEL_AFK_EVERY:
                self.afk_said_at = now
                self.log(f"joined {party} -> saying '{LEVEL_AFK_TEXT}'")
                self.c.chat(("~" if LEVEL_AFK_PARTY_CHAT else "") + LEVEL_AFK_TEXT)

    def party_tick(self):
        c, s = self.c, self.c.s
        now = time.time()
        party = s.get("party") or []
        ours = set(self.hub.friends)
        snap = self.hub.snapshot()
        me = snap.get(self.name) or {}
        grp = self.pair_group(snap)
        if grp:
            return self.pair_party_tick(grp, snap)

        # invites: accept our own characters, and strangers (LEVEL_ACCEPT_STRANGERS), only when not in a party
        if s.get("partyRequest"):
            text = s.get("partyRequestText", "")
            from_ours = any(f'"{n}"' in text for n in ours if n != self.name)
            if len(party) <= 1 and (from_ours or LEVEL_ACCEPT_STRANGERS):
                self.log(f"accepting party invite{'' if from_ours else ' from a stranger'}: {text[:60]}")
                self.ev("() => window.__social.partyRespond(true)")
                self.queue_thanks(text)
                if not from_ours:
                    self.stranger_party = {"area": (self.step_key, self.spot_idx), "joined": now,
                                           "said": False, "say_at": now + random.uniform(*LEVEL_AFK_DELAY)}
            else:
                self.ev("() => window.__social.partyRespond(false)")

        if len(party) <= 1 and self.stranger_party and now - self.stranger_party["joined"] > 15:
            self.stranger_party = None   # the party broke up / we were kicked / the invite expired
        if len(party) > 1:
            if any(n not in ours for n in party):
                if not LEVEL_ACCEPT_STRANGERS:
                    return self.leave_party("a stranger is in it")
                return self.stranger_party_tick(party, now)
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
            if o.get("assisted") or o.get("pair_lead"):
                continue           # pairs keep their own party
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


    # ── alts give their Boxes of Kundun to the main character ──
    def boxes_count(self):
        now = time.time()
        if now - self.boxes_at >= 30:
            self.boxes_at = now
            slots = self.ev(B.KUNDUN_SLOTS_JS, B.KUNDUN_MIN_LVL)
            if slots is not None:
                self.boxes = len(slots)
        return self.boxes

    def update_bag_free(self):
        """Main only: free bag cells, published so the alts know whether a trade can fit."""
        now = time.time()
        if now - self.bag_free_at < 30:
            return
        self.bag_free_at = now
        r = self.ev(B.BAG_FREE_JS)
        if not r:
            return
        prev, self.bag_free = self.bag_free, r["free"]
        hub = self.hub
        if not self.bag_keys_logged and r.get("keys"):
            self.bag_keys_logged = True
            self.log(f"bag: {r['free']} free cells ({r.get('sized')} item(s) bigger than 1x1 found) | "
                     f"item fields: {r['keys']}")
        if hub.lead_full and (self.bag_free > hub.lead_full["free"] or now - hub.lead_full["at"] > TRADE_FULL_SEC):
            self.log(f"bag: {self.bag_free} free cells now -> taking boxes again")
            hub.lead_full = None
        if self.bag_free < 5 and not self.bag_low_logged:
            self.bag_low_logged = True
            self.log(f"bag: only {self.bag_free} free cell(s) left -> make room for more Boxes of Kundun")
        elif self.bag_free >= 5:
            self.bag_low_logged = False

    def trade_quiet(self):
        """True around Blood Castle time: no new trades then."""
        o = self.hub.open_at
        if o is not None and self.hub.bc_on:        # Blood Castle off: trades go on through BC time
            until = o - time.time()
            if -B.BC_DOOR_CLOSE_SEC - 60 <= until <= BC_PLAN_SEC + TRADE_BC_QUIET:
                return True
        return GI.GI_ENABLED and 47 <= time.localtime().tm_min <= 54     # Golden Invasion (:50)

    def maybe_start_trade(self):
        hub, c, s = self.hub, self.c, self.c.s
        now = time.time()
        lvl = s.get("level") or 0
        if self.bcs or now < self.trade_next or (s.get("hp") or 0) <= 0 or c.shop_pending \
                or lvl >= (hub.reset_level or 10 ** 6) or s.get("x") is None:
            return None
        if self.boxes_count() <= 0 or self.trade_quiet():
            return None
        lead = hub.snapshot().get(hub.trade_to)
        if not lead or not lead.get("online") or lead.get("paused") or lead.get("mode") != "level" \
                or lead.get("bc") or (lead.get("hp") or 0) <= 0:
            return None
        if lead.get("map") != s.get("map") or lead.get("x") is None or \
                math.hypot(lead["x"] - s["x"], lead["y"] - s["y"]) > TRADE_MEET_RADIUS:
            return None
        free = lead.get("bag_free")
        if hub.lead_full or (free is not None and free < TRADE_MIN_FREE):
            if now - getattr(self, "full_logged", 0) > 1800:
                self.full_logged = now
                self.log(f"{hub.trade_to}'s bag is full ({free} free) -> keeping my {self.boxes} box(es) for now")
            return None
        with hub.lock:
            if hub.trade is not None:
                return None
            t = {"alt": self.name, "lead": hub.trade_to, "phase": "meet", "start": now, "phase_at": now,
                 "n": self.boxes, "requests": 0, "ok": False, "lead_free": free}
            hub.trade = t
        self.log(f"met {hub.trade_to} with {self.boxes} Box(es) of Kundun -> trading right here")
        return t

    def trade_phase(self, t, phase):
        with self.hub.lock:
            if self.hub.trade is t:
                t["phase"], t["phase_at"] = phase, time.time()

    def trade_end(self, t, ok, why):
        with self.hub.lock:
            if self.hub.trade is not t:
                return
            self.hub.trade = None
            t["ok"] = ok
        self.log(f"box trade {t['alt']} -> {t['lead']}: {why}")

    def trade_cleanup(self, t):
        """My side after a trade ended (either side may have ended it)."""
        c = self.c
        self.ev("() => { const e = window.__eco; if (!e) return;"
                " if (e.tradeOpen) e.cancelTrade(); if (e.tradeRequest) e.answerTradeRequest(false); }")
        self.my_trade = None
        self.trade_filled = False
        c.walk_target = None          # same spot as before: move_tick walks back / turns the helper on
        self.spot_since, self.samples, self.stat_point = time.time(), [], None   # don't count the pause
        if self.name == t["alt"]:
            self.boxes_at = 0
            now = time.time()
            if t.get("ok"):
                self.trade_fails = 0
                self.trade_next = now + 60
            else:
                self.trade_fails += 1
                wait = TRADE_GIVEUP_SEC if self.trade_fails >= TRADE_MAX_FAILS else TRADE_RETRY_SEC
                self.trade_next = now + wait
                self.log(f"box trade failed ({self.trade_fails} in a row) -> next try in {wait // 60} min")

    def trade_tick(self):
        """True while a box trade takes over this character (leveling paused)."""
        if not TRADE_BOXES:
            return False
        hub = self.hub
        t = self.my_trade
        if t is None:
            t = hub.trade
            if t is None and self.name != hub.trade_to:
                t = self.maybe_start_trade()
            if t is None or self.name not in (t["alt"], t["lead"]):
                return False
            self.my_trade, self.trade_sent, self.trade_filled = t, 0, False
            self.c.set_helper(False)
            self.c.walk_target = None
            if self.name == t["lead"]:
                self.log(f"{t['alt']} has {t['n']} Box(es) of Kundun for me -> stopping here to trade")
        if hub.trade is not t:
            self.trade_cleanup(t)
            return False
        now = time.time()
        other = hub.snapshot().get(t["lead"] if self.name == t["alt"] else t["alt"]) or {}
        off_key = "off_" + self.name
        if other.get("online") and not other.get("paused"):
            t.pop(off_key, None)
        else:
            t.setdefault(off_key, now)      # a few seconds offline (map loading) is fine
        if self.bcs or other.get("bc"):
            self.trade_end(t, False, "Blood Castle comes first")
        elif other.get("paused") or now - t.get(off_key, now) > TRADE_OFFLINE_GRACE:
            self.trade_end(t, False, "the other character is offline / paused")
        elif other.get("map") is not None and other.get("map") != self.c.s.get("map"):
            self.trade_end(t, False, "not on the same map any more")
        elif now - t["start"] > TRADE_TIMEOUT:
            self.trade_end(t, False, f"took more than {TRADE_TIMEOUT}s (phase {t['phase']})")
        if hub.trade is not t:
            self.trade_cleanup(t)
            return False
        self.mode = "trade"
        if (self.c.s.get("hp") or 0) <= 0:
            return True
        try:
            if self.name == t["alt"]:
                self.trade_alt(t, other)
            else:
                self.trade_lead(t)
        except Exception as e:
            self.log(f"box trade error: {e!r}")
        return True

    def trade_alt(self, t, lead):
        """Walk up to the main where he stands, send the request, put the boxes in, confirm --
        but only once the MAIN's own window shows the boxes arriving (= it's really him)."""
        c, s = self.c, self.c.s
        now = time.time()
        if t["phase"] == "meet":
            c.set_helper(False)
            lx, ly = lead.get("x"), lead.get("y")
            if lx is None or s.get("x") is None:
                return
            if math.hypot(lx - s["x"], ly - s["y"]) <= TRADE_NEAR:
                c.walk_target = None
                self.trade_phase(t, "request")
                return
            self.go(s.get("map"), None, int(lx), int(ly), radius=TRADE_NEAR - 1)
            return
        st = self.ev(B.TRADE_STATE_JS) or {}
        if t["phase"] == "request":
            if st.get("open"):
                self.trade_phase(t, "fill")
                return
            if now - self.trade_sent < 6:
                return
            if t["requests"] >= 3:
                return self.trade_end(t, False, f"{t['lead']} never opened the trade window")
            c.set_helper(False)
            c.close_windows()
            res = self.ev(B.TRADE_REQUEST_JS, t["lead"])
            t["requests"] += 1
            self.trade_sent = now
            self.log(f"box trade: request to {t['lead']}: {res}")
            if res == "not in view":
                self.trade_phase(t, "meet")     # he moved: walk up to him again
            return
        if not st.get("open"):
            if t["phase"] == "confirm":
                time.sleep(1)
                left = len(self.ev(B.KUNDUN_SLOTS_JS, B.KUNDUN_MIN_LVL) or [])
                self.boxes = left
                before = t.get("before", t["n"])
                if left < before:
                    return self.trade_end(t, True, f"done, {before - left} box(es) given, {left} left")
                # both confirmed and nothing moved: most likely his bag had no room
                self.mark_lead_full(t)
                return self.trade_end(t, False, "trade window closed, boxes still in my bag")
            return self.trade_end(t, False, "trade window closed before confirming")
        if t["phase"] == "fill":
            if not t.get("lead_open"):
                # my window is open but the main's isn't (yet): wait a bit, then it's someone else
                if now - t["phase_at"] > 8:
                    self.trade_end(t, False, f"my trade window opened but {t['lead']}'s didn't -> "
                                             "not trading with someone else")
                return
            if not self.trade_filled:
                slots = self.ev(B.KUNDUN_SLOTS_JS, B.KUNDUN_MIN_LVL) or []
                if not slots:
                    return self.trade_end(t, False, "no boxes in the bag any more")
                t["before"] = len(slots)
                cap = t.get("lead_free")
                if cap is not None and cap < len(slots):
                    self.log(f"box trade: {t['lead']} has room for {cap} -> giving {cap} of {len(slots)}")
                    slots = slots[:max(1, cap)]
                t["n"] = len(slots)
                self.log(f"box trade: putting {len(slots)} box(es) in the trade window")
                for slot in slots:     # Inventory (0) -> my trade window (1)
                    for _ in range(8):
                        if not (self.ev(B.TRADE_STATE_JS) or {}).get("busy"):
                            break
                        time.sleep(0.4)
                    self.ev("(i) => window.__store.autoMoveItem(0, i, 1)", slot)
                    time.sleep(0.6)
                self.trade_filled = True
                t["filled_at"] = time.time()
                return
            # the safety check: confirm only when the MAIN sees exactly my boxes in his window
            if st.get("mine", 0) >= t["n"] and t.get("lead_boxes", 0) >= t["n"]:
                time.sleep(0.5)
                self.log(f"box trade: {t['lead']} sees my {t['n']} box(es) -> confirming")
                self.ev("() => window.__eco.setMyConfirm(true)")
                self.trade_phase(t, "confirm")
            elif now - t.get("filled_at", now) > 12:
                self.trade_end(t, False, f"{st.get('mine', 0)}/{t['n']} boxes in my window, "
                                         f"{t.get('lead_boxes', 0)} seen by {t['lead']}")
            return
        if t["phase"] == "confirm" and now - t["phase_at"] > 15:
            self.trade_end(t, False, f"{t['lead']} did not confirm")

    def mark_lead_full(self, t):
        """A trade that should have fit didn't go through: stop offering boxes to the main until
        his free space goes up (he sold / stored something) or TRADE_FULL_SEC passes."""
        hub = self.hub
        free = (hub.snapshot().get(t["lead"]) or {}).get("bag_free")
        hub.lead_full = {"free": free if free is not None else 0, "at": time.time()}
        self.log(f"box trade: {t['lead']} could not take the boxes (bag says {free} free) -> "
                 "nobody offers him boxes until he has more room")

    def trade_lead(self, t):
        """Stand still (helper off); accept only the alt's request; confirm when his boxes are in."""
        c = self.c
        c.set_helper(False)
        c.walk_target = None
        st = self.ev(B.TRADE_STATE_JS) or {}
        if st.get("req") and not st.get("open"):
            who = self.ev(TRADE_FROM_JS, [t["alt"], self.hub.friends]) or {}
            if who.get("from") == "other":
                self.log(f"box trade: declining a trade request from someone else {who.get('raw', '')[:80]}")
                self.ev("() => window.__eco.answerTradeRequest(false)")
                return
            if who.get("from") != "alt":
                self.log(f"box trade: can't tell who sent this request {who.get('raw', '')[:80]} -> "
                         f"accepting, {t['alt']} only confirms if I see his boxes")
            else:
                self.log(f"box trade: accepting {t['alt']}'s trade request")
            self.ev("() => window.__eco.answerTradeRequest(true)")
            return
        if not st.get("open"):
            t["lead_open"], t["lead_boxes"] = False, 0
            return
        box = self.ev(TRADE_YOURS_JS, B.KUNDUN_MIN_LVL) or {}
        if box.get("other"):
            self.log("box trade: something other than Kundun boxes in the window -> cancelling")
            self.ev("() => window.__eco.cancelTrade()")
            return self.trade_end(t, False, "unexpected items in the trade window")
        t["lead_open"], t["lead_boxes"] = True, box.get("boxes", 0)
        if t["phase"] == "confirm" and not st.get("myOk") and st.get("yours", 0) >= t["n"] and st.get("yourOk"):
            time.sleep(0.5)
            self.log(f"box trade: {st.get('yours')} box(es) in the window -> confirming")
            self.ev("() => window.__eco.setMyConfirm(true)")

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
                if self.open_logged != round(now - since):
                    self.open_logged = round(now - since)
                    self.log(f"BC: entry window is open ({int(since)}s ago, timer says {t!r}) -> planning entries")
            return
        secs = t["secs"]
        since = 3600 - secs
        open_at = now - since if since <= B.BC_DOOR_CLOSE_SEC else now + secs
        self.hub.open_at = open_at

    def bc_tick(self):
        """True while Blood Castle takes over this character (leveling paused)."""
        if not self.hub.bc_on:
            b = self.bcs
            if b is None:
                return False
            if not b.get("entered"):            # switched off before getting in: drop it
                self.bc_done("Blood Castle switched off")
                return False
            # already inside: finish this castle (leaving mid-event looks worse than staying)
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
                            "phase": "farm" if role == "hold" else "climb",
                            "cloak": False, "talk_at": 0, "no_dialog": 0, "entered": False,
                            "burst_until": 0, "tried": set(), "box": False, "box_try": None,
                            "first_stranger": None, "invited": {}, "end_at": None, "rush": False,
                            "asked": False, "in_party": set()}
                self.ev("() => { window.__bcState = null; }")
        b = self.bcs
        if b is None:
            return False
        try:
            return self.bc_run(b)
        except Exception as e:
            self.log(f"BC error: {e!r}")
            return True

    def gi_left_for_bc(self, b):
        """True when the Golden Invasion hunt just ended early for this castle (mu_gi GI_BC_LEAVE_SEC):
        go straight to the cloak + bar instead of farming until BC_FARM_UNTIL_SEC."""
        left = getattr(self.gi, "left_for_bc", None) if self.gi else None
        return left is not None and abs(left - b["open_at"]) < 120

    def bc_done(self, why, late=False):
        self.log(f"BLOOD CASTLE {self.bcs['tier']}: {why} -> back to leveling")
        plan = self.hub.plan
        if plan and not self.bcs.get("entered"):
            with self.hub.lock:      # never got in: free the castle so someone else can take it
                plan["assign"].pop(self.name, None)
                if late:
                    # only missed the climb deadline: keeps leveling and may still land in a free
                    # castle's range before the door closes -> the re-plan may assign it as a holder
                    plan["started"].discard(self.name)
                else:
                    plan["out"].add(self.name)
            release_bc_claim(self.hub, self.bcs.get("tier"), self.name)   # the other machine may take it
        self.grab(self.bcs, False)
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
            elif bc_entry_level(tier) <= lvl <= hi:
                self.log(f"BC{tier}: level {lvl} is in range ({lo}-{hi}) -> holding")
                b["phase"] = "farm"
            elif lvl > hi:
                self.bc_done(f"level {lvl} went past the range ({lo}-{hi})")
                return False
            if b["phase"] == "farm":
                pass
            elif since_open > -BC_CLIMB_DEADLINE:
                self.bc_done(f"did not reach level {bc_entry_level(tier)} in time (level {lvl})", late=True)
                return False
            else:
                return False      # keep leveling (and resetting) toward the range
        if b["phase"] == "farm":
            # in range: keep farming (no EXP at the bar) until :57 or a few levels below the top
            top = max(hi - BC_FARM_TOP_MARGIN, lo)
            if in_bc(s.get("map")):
                b["phase"] = "prep"
            elif lvl > hi:
                self.bc_done(f"level {lvl} went past the range ({lo}-{hi})")
                return False
            elif since_open >= -BC_FARM_UNTIL_SEC or lvl >= top or self.gi_left_for_bc(b):
                why = (f"level {lvl} (stop at {top})" if lvl >= top
                       else f"left the Golden Invasion, {int(-since_open)}s before the door" if since_open < -BC_FARM_UNTIL_SEC
                       else f"{int(-since_open)}s before the door")
                self.log(f"BC{tier}: {why} -> stop farming, cloak + bar")
                b["phase"] = "prep"
            else:
                if not b.get("farm_logged"):
                    b["farm_logged"] = True
                    self.log(f"BC{tier}: level {lvl}, farming until level {top} or "
                             f"{BC_FARM_UNTIL_SEC // 60} min before the door")
                return False
        self.mode = "bc"
        if b["phase"] == "prep" and lvl < DEVIAS_MIN_LEVEL and not in_bc(s.get("map")):
            self.log(f"BC{tier}: level {lvl} is too low for /move Devias ({DEVIAS_MIN_LEVEL}) -> leveling a bit first")
            b["phase"] = "climb"
            return False
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
        if b["phase"] != "inside":
            self.grab(b, False)
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
        elif alias and time.time() - c.walk_progress_at > 45 and not in_bc(map_idx):
            self.log("stuck walking -> re-warping")
            c.chat(f"/move {alias}")
            c.last_warp, c.walk_target = time.time(), None
            return False
        self.ev(B.WALK_JS, [x, y])
        return False

    def park(self, map_idx, alias, x, y, radius=2):
        """go() for a spot where the character only WAITS: once there, it idle-wiggles around it
        (and counts as arrived while within IDLE_WIGGLE_LEASH tiles of it)."""
        c, s = self.c, self.c.s
        home = (map_idx, x, y)
        if (IDLE_WIGGLE and self.wig_home == home and s.get("map") == map_idx
                and dist(s, x, y) <= max(radius, IDLE_WIGGLE_LEASH + 0.5)):
            c.walk_target = None
            self.idle_wiggle(home)
            return True
        if not self.go(map_idx, alias, x, y, radius):
            return False
        self.idle_wiggle(home)
        return True

    def idle_wiggle(self, home):
        """Standing at a waiting spot: every IDLE_WIGGLE_EVERY seconds take a 1-2 tile step to a random
        walkable tile, never further than IDLE_WIGGLE_LEASH from home = (map, x, y)."""
        if not IDLE_WIGGLE:
            return
        s = self.c.s
        now = time.time()
        if self.wig_home != home:            # just arrived: first step only after a pause
            self.wig_home = home
            self.wig_next = now + random.uniform(*IDLE_WIGGLE_EVERY)
            return
        if now < self.wig_next:
            return
        self.wig_next = now + random.uniform(*IDLE_WIGGLE_EVERY)
        x, y = s.get("x"), s.get("y")
        if x is None or y is None or (s.get("hp") or 0) <= 0 or s.get("helper"):
            return
        _, hx, hy = home
        rng = IDLE_WIGGLE_STEP if isinstance(IDLE_WIGGLE_STEP, (tuple, list)) else (IDLE_WIGGLE_STEP, IDLE_WIGGLE_STEP)
        step = random.randint(*rng)
        cands = [(x + dx, y + dy) for dx in range(-step, step + 1) for dy in range(-step, step + 1)
                 if max(abs(dx), abs(dy)) == step
                 and math.hypot(x + dx - hx, y + dy - hy) <= IDLE_WIGGLE_LEASH]
        if not cands and step > 1:           # 2 tiles would leave the leash: try 1
            cands = [(x + dx, y + dy) for dx in (-1, 0, 1) for dy in (-1, 0, 1) if (dx or dy)
                     and math.hypot(x + dx - hx, y + dy - hy) <= IDLE_WIGGLE_LEASH]
        random.shuffle(cands)
        for tx, ty in cands[:8]:
            if self.ev(B.WALK_JS, [int(tx), int(ty)]):
                return

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
        if ok:
            self.log(f"BC: cloak +{tier} bought")
        else:
            note = self.ev("() => (window.__store.notifications || []).slice(-2)"
                           ".map(n => n.text || n.message || JSON.stringify(n))") or []
            chat = (self.ev(G.CHAT_LINES_JS) or [])[-2:]
            self.log(f"BC: cloak +{tier} NOT bought (slot {slot}) | zen {self.c.s.get('money')} | "
                     f"game says: {note} {chat}")
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
        if since_open < -BC_DEVIAS_SEC:
            self.park(*bar)          # wait at the bar (safe zone, no EXP)
            return
        m, x, y = B.SPIRIT_SPOT
        if since_open < -IDLE_WIGGLE_DOOR_SEC:
            self.park(m, "Devias", x, y, radius=1)     # wait by the Spirit, wiggling a little
            return
        if not self.go(m, "Devias", x, y, radius=1):   # back on the spot for the click
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
        b["clicks"] = b.get("clicks", 0) + 1
        if b["clicks"] <= 2 or b["clicks"] % 6 == 0:
            self.log(f"BC: clicking '{btn.get('label')}' (try {b['clicks']})")
        t_click = int(time.time() * 1000)
        try:
            c.page.mouse.click(btn["x"], btn["y"])
        except Exception as e:
            self.log(f"BC: click failed {e!r}")
        self.bc_entry_check(b, t_click)

    def bc_entry_check(self, b, t_click):
        """After a castle click: log what the server answered (the bot used to be blind here: at
        06:00 wizzardo and cucaracha clicked 57 times each and never got in), and after
        BC_ENTER_FAILS_RELOAD clicks without getting in, reload + log in again -- that is what got
        wizzardo in at 07:03 after the server hiccup that made every client show level 10."""
        c = self.c
        time.sleep(1.5)
        if in_bc((c.refresh() or {}).get("map")):
            return
        r = self.ev(B.BC_ENTER_RESULT_JS, t_click) or {}
        e, msg = r.get("enter"), r.get("msg")
        if e or msg:
            what = (f"enter result {e['code']} = {B.BC_ENTER_RESULTS.get(e['code'], '?')} [{e['hex']}]"
                    if e else "no enter result")
            line = f"{what}" + (f" | server: {msg}" if msg else "")
            if line != b.get("enter_said"):
                b["enter_said"] = line
                self.log(f"BC: not in yet - {line} | client level {c.s.get('level')}")
        elif b["clicks"] in (2, BC_ENTER_FAILS_RELOAD):
            self.log(f"BC: not in yet - the server sent no answer to the click | client level {c.s.get('level')}")
        if b["clicks"] >= BC_ENTER_FAILS_RELOAD and b.get("reloads", 0) < BC_ENTER_MAX_RELOADS:
            b["reloads"] = b.get("reloads", 0) + 1
            b["clicks"] = 0
            b["talk_at"] = time.time() + 20     # don't count the cloak as consumed while logging back in
            c.reload_page(f"Blood Castle: {BC_ENTER_FAILS_RELOAD} clicks without getting in "
                          f"(reload {b['reloads']}/{BC_ENTER_MAX_RELOADS})")

    def bc_sleep(self):
        """Faster ticks while inside Blood Castle (the weapon grab itself runs in the page)."""
        b = self.bcs
        return B.BC_FAST_TICK if b and b.get("phase") == "inside" else None

    def helper_async(self, on):
        """Toggle MU Helper WITHOUT waiting for the server (set_helper blocks ~5 s per toggle).
        Presses the key at most once per HELPER_ASYNC_GUARD so a slow confirm isn't toggled back."""
        c = self.c
        if bool(c.s.get("helper")) == on:
            self.helper_tries = 0
            return
        now = time.time()
        if now - self.helper_press_at < B.HELPER_ASYNC_GUARD:
            return
        self.helper_press_at = now
        # 2026-10-12: the key press alone never landed in the minimized windows (BC 00:00: all four
        # characters pressed "ON" every 4.5 s for 3 min, helper stayed off, stuck at the gate).
        # set_helpers() always had the client's own toggle as fallback; here we alternate: the
        # client toggle first (works minimized), the key on the next try.
        self.helper_tries = getattr(self, "helper_tries", 0) + 1
        how = "toggle" if self.helper_tries % 2 else "key"
        self.log(f"helper -> {'ON' if on else 'OFF'} (no wait, {how})")
        try:
            if how == "toggle":
                c.page.evaluate("() => window.__store.toggleMuHelper()")
            else:
                c.page.evaluate("() => { const c=document.querySelector('canvas'); c && c.focus(); }")
                c.page.keyboard.press(G.HELPER_KEY)
        except Exception as e:
            self.log(f"helper {how}: {e!r}")

    def grab(self, b, on, goal=None, goal_r=2, mode="advance", weapon=True, statue=True,
             fight=None, zone=True, tag="BC"):
        """Arm/disarm the in-page loop (mu_bc.BC_GRAB_JS): weapon grab + statue attack, and without
        the helper (B.BC_USE_HELPER False) also walking to `goal` and fighting what is in the way
        (mode advance / rush / guard / hold). Logs its events. Returns its status, or None if the
        page didn't answer."""
        if not on and not b.get("grab_on"):
            return None
        fight = (not B.BC_USE_HELPER) if fight is None else fight
        cfg = {"version": B.GRAB_VERSION, "on": bool(on), "g": B.WEAPON[0], "n": B.WEAPON[1],
               "re": B.WEAPON_NAME_RE, "radius": B.GRAB_RADIUS, "loopMs": B.GRAB_LOOP_MS,
               "pickMs": B.GRAB_PICK_MS, "weapon": bool(weapon), "statue": bool(statue),
               "statueTypes": B.STATUE_TYPES, "statueRadius": B.STATUE_ATTACK_RADIUS,
               "castMs": B.STATUE_CAST_MS if not fight else B.FIGHT_CAST_MS,
               "stand": list(B.STATUE), "standR": B.STATUE_RADIUS + 0.5,
               "fight": fight, "goal": list(goal) if (fight and goal) else None, "goalR": goal_r,
               "mode": mode, "gateTypes": B.GATE_TYPES, "npcTypes": B.NPC_TYPES,
               "mageSkill": B.FIGHT_MAGE_SKILL, "combo": B.FIGHT_KNIGHT_COMBO,
               "comboStep": B.FIGHT_COMBO_STEP_MS, "mageRange": B.FIGHT_MAGE_RANGE,
               "knightRange": B.FIGHT_KNIGHT_RANGE, "otherRange": B.FIGHT_OTHER_RANGE,
               "range": B.FIGHT_RANGES.get(self.name.lower(), 0), "coneHalf": B.FIGHT_CONE_HALF,
               "rushRange": B.FIGHT_RUSH_RANGE, "stuckMs": B.FIGHT_STUCK_MS, "huntMs": B.FIGHT_HUNT_MS,
               "leash": B.FIGHT_LEASH, "walkMs": B.FIGHT_WALK_MS, "potKey": B.FIGHT_POT_KEY,
               "potHp": B.FIGHT_POT_HP, "potMs": B.FIGHT_POT_MS, "buffs": B.FIGHT_BUFFS,
               "buffEvery": B.FIGHT_BUFF_EVERY_MS, "buffTries": B.FIGHT_BUFF_TRIES,
               # v7: kill-phase zone at the bridge + self-start from the waiting spot
               "zone": list(B.BRIDGE_EDGE) if zone else None, "zoneR": B.BRIDGE_ZONE_R, "killPhase": bool(b.get("kill_phase")),
               "stateSince": int((b.get("open_at") or 0) * 1000 - 60_000),
               "startGoal": list(B.STATUE), "startMode": "advance", "startR": B.STATUE_RADIUS}
        if on and not fight and b.get("statue_solo_over"):
            cfg["statue"] = False                 # our own attack took too long: the helper finishes it
        r = self.ev(B.BC_GRAB_JS, cfg)
        if r is None:
            return None
        if on and not b.get("grab_on"):
            self.log(f"{tag}: in-page loop armed (every {B.GRAB_LOOP_MS} ms: "
                     + ("weapon grab, " if weapon else "")
                     + ("walking + fighting, NO helper)" if fight else "statue)"))
        b["grab_on"] = bool(on)
        for e in r.get("events") or []:
            self.log(f"{tag} grab: {e}")
        if r.get("err") and r["err"] != b.get("grab_err"):
            b["grab_err"] = r["err"]
            self.log(f"{tag} grab error: {r['err']}")
        return r

    def fight_to(self, b, x, y, radius, mode, at_goal=None, weapon=True, statue=False):
        """No-helper movement inside the castle: the in-page loop walks to (x, y) and fights what is
        in the way (mode); once within radius it switches to at_goal (e.g. guard / hold).
        Returns (arrived, loop status)."""
        s = self.c.s
        if s.get("helper"):
            self.helper_async(False)              # left on from leveling: it would fight on its own
        arrived = dist(s, x, y) <= radius
        r = self.grab(b, True, goal=(x, y), goal_r=radius,
                      mode=(at_goal or mode) if arrived else mode, weapon=weapon, statue=statue)
        self.fight_log(b, r)
        return arrived, r

    def fight_log(self, b, r, tag="BC fight"):
        """Status line every FIGHT_LOG_SEC: where we are, what we hit, casts sent vs seen, potions."""
        now = time.time()
        if not r or now - b.get("fight_log_at", 0) < B.FIGHT_LOG_SEC:
            return
        b["fight_log_at"] = now
        f, s = r.get("fight") or {}, self.c.s
        d = f.get("dGoal")
        self.log(f"{tag}: ({s.get('x')},{s.get('y')}) {f.get('mode')} | "
                 f"{'%.0f' % d if d is not None else '?'} tiles to go | "
                 f"{f.get('target') or '-'} ({f.get('why')}) | {f.get('inRange')} in range "
                 f"({f.get('kind')}, {f.get('range')}t, skill {f.get('skill')}) | casts {f.get('casts')} sent / "
                 f"{f.get('seen')} seen | pots {f.get('pots')} | hp {f.get('hp')}/{f.get('maxHp')}"
                 + (" | STUCK" if f.get("stuck") else ""))

    def walk_in_castle(self, b, x, y, radius=2):
        c, s = self.c, self.c.s
        now = time.time()
        d = dist(s, x, y)
        # helper toggles never block here (helper_async): the old set_helper waited ~5 s for the
        # server on every toggle, and the whole tick (weapon pickup included) waited with it
        if d <= radius:
            if s.get("helper") and now >= b["burst_until"]:
                self.helper_async(False)
            c.walk_target = None
            return True
        if now < b["burst_until"]:
            if not s.get("helper") and now - b.get("burst_at", now) < 8:
                b["burst_until"] = now + B.HELPER_BURST     # count the burst from when the helper is really on
            self.helper_async(True)
            return False
        if s.get("helper"):
            self.helper_async(False)                        # walk once it has stopped (next ticks)
            c.walk_target = None
            return False
        if c.walk_target != (x, y):
            c.walk_target, c.walk_best, c.walk_progress_at = (x, y), d, now
        if d < c.walk_best - 0.5:
            c.walk_best, c.walk_progress_at = d, now
        elif now - c.walk_progress_at > B.STUCK_SEC:
            b["stuck_n"] = b.get("stuck_n", 0) + 1
            if b["stuck_n"] % 5 == 1:
                self.log(f"BC: stuck at ({s.get('x')},{s.get('y')}), {d:.0f} tiles from ({x},{y}) -> helper burst")
            b["burst_until"], b["burst_at"] = now + B.HELPER_BURST, now
            c.walk_target = None
            self.helper_async(True)
            return False
        if not self.ev(B.WALK_JS, [int(x), int(y)]):
            for ox, oy in ((1, 0), (-1, 0), (0, -1), (1, -1), (-1, -1), (2, 0), (0, -2), (2, -2),
                           (0, 1), (1, 1), (-1, 1)):
                if self.ev(B.WALK_JS, [int(x) + ox, int(y) + oy]):
                    break
        return False

    # new system / error chat lines and game notifications since the last call (marks what it
    # returns, so it never touches mu_chat's own cursor); prime=True only marks, returns nothing
    SYS_MSGS_JS = """(prime) => {
      const out = [], soc = window.__social, st = window.__store;
      for (const l of (soc && soc.chatLines) || []) {
        if (typeof l !== 'object' || l.__bcSeen) continue;
        l.__bcSeen = true;
        if (!prime && (l.type === 3 || l.type === 4 || !l.sender)) out.push(l.text || '');
      }
      for (const n of (st && st.notifications) || []) {
        if (typeof n !== 'object' || n.__bcSeen) continue;
        n.__bcSeen = true;
        if (!prime) out.push(n.text || n.message || JSON.stringify(n));
      }
      return out;
    }"""

    # diagnostic, once per run: what the client knows about another player (looking for a party flag)
    PLAYER_DUMP_JS = """(ours) => {
      const w = window.__world;
      const e = w.netObjsQuery.entities.find(e => !e.localPlayer && e.charAppearance &&
                  e.objectNameInWorld && !ours.includes(e.objectNameInWorld));
      if (!e) return null;
      const hits = {};
      for (const k of Object.keys(e)) {
        if (/party|guild|team|group|leader/i.test(k)) {
          try { hits[k] = JSON.stringify(e[k]).slice(0, 120); } catch (_) { hits[k] = '?'; }
        }
      }
      return {name: e.objectNameInWorld, hits, keys: Object.keys(e).join(',')};
    }"""
    player_dumped = False

    IN_PARTY_RE = re.compile(r"(already|ya\s+(est[aá]|tiene|pertenece)|otro|another|in a party|en un (grupo|party))", re.I)

    def bc_party_replies(self, b):
        """Read the game's answers to our invites: a player who is already in a party is skipped."""
        msgs = self.ev(self.SYS_MSGS_JS, False) or []
        for m in msgs:
            if not m:
                continue
            if b.get("inviting"):
                self.log(f"BC: game says: {m[:120]}")
            if not re.search(r"party|grupo", m, re.I) or not self.IN_PARTY_RE.search(m):
                continue
            for n in b["invited"]:
                if n in m and n not in b["in_party"]:
                    b["in_party"].add(n)
                    self.log(f"BC: {n} is already in a party -> not inviting him again")

    # who holds the Archangel's weapon (the castle state's holder = a netId): me / a name / a party member
    HOLDER_JS = """(h) => { const w = window.__world, me = w && w.playerEntity;
      if (me && me.netId === h) return {me: true, name: (window.__store.playerData || {}).name || null};
      const e = w ? w.netObjsQuery.entities.find(x => x.netId === h && x.playerAnimation) : null;
      let name = e ? (e.objectNameInWorld || '').trim() : null;
      if (!name) { const pm = ((window.__social || {}).partyMembers || []).find(m => m.index === h || m.netId === h);
                   if (pm) name = (pm.name || '').trim(); }
      return {me: false, name}; }"""

    def bc_weapon_shout(self, b, holder):
        """Once per castle: the weapon was just picked up by us or one of our party -> say BC_WEAPON_TEXT."""
        if not BC_WEAPON_TEXT or holder in (None, 255) or b.get("weapon_shout"):
            return
        who = self.ev(self.HOLDER_JS, holder) or {}
        name = (who.get("name") or "").strip()
        first = b.setdefault("weapon_seen_at", time.time())
        if not who.get("me") and not name and time.time() - first < 5:
            return                                   # holder not identified yet (out of view?): retry a few s
        b["weapon_shout"] = True                     # decided once per castle, whoever it was
        party = [n.strip().lower() for n in (self.c.s.get("party") or [])]
        ours = who.get("me") or (name and name.lower() in party)
        if not ours:
            self.log(f"BC: weapon picked up by {name or f'holder {holder}'} (not our party) -> no shout")
            return
        key = (b.get("tier"), b.get("open_at"))
        with self.hub.lock:                          # two of ours in the same castle: say it only once
            said = getattr(self.hub, "bc_weapon_shouts", set())
            if key in said:
                return
            said.add(key)
            self.hub.bc_weapon_shouts = said
        self.log(f"BC: weapon picked up by {'me' if who.get('me') else name} (our party) -> '{BC_WEAPON_TEXT}'")
        self.c.chat(BC_WEAPON_TEXT)

    def bc_party(self, b):
        """Once another player shows up: say "party?" and wait BC_WAIT_INVITE_SEC for an invite;
        then invite the players in view who aren't already in a party (and keep inviting new arrivals)
        while we lead and have room."""
        s = self.c.s
        now = time.time()
        party = s.get("party") or []
        if s.get("partyRequest"):
            if len(party) <= 1:
                self.log(f"BC: accepting party invite {s.get('partyRequestText', '')[:60]}")
                self.ev("() => window.__social.partyRespond(true)")
                self.queue_thanks(s.get("partyRequestText", ""))
            else:
                self.ev("() => window.__social.partyRespond(false)")
            return
        if not b.get("sys_primed"):
            b["sys_primed"] = True
            self.ev(self.SYS_MSGS_JS, True)
        self.bc_party_replies(b)
        players = (self.ev(B.SCAN_JS, [self.hub.friends, B.CLEAR_RADIUS, 0, 0, None]) or {}).get("players", [])
        if players and not Agent.player_dumped:
            dump = self.ev(self.PLAYER_DUMP_JS, self.hub.friends)
            if dump:
                Agent.player_dumped = True
                self.log(f"BC diag: player entity {dump.get('name')}: party-ish fields {dump.get('hits')} | keys {dump.get('keys')}")
        if players and b["first_stranger"] is None:
            b["first_stranger"] = now
            if len(party) <= 1 and not b["asked"]:
                b["asked"] = True
                if BC_ASK_PARTY:
                    self.log(f"BC: other players here ({', '.join(players[:4])}) -> asking '{BC_ASK_PARTY_TEXT}', "
                             f"waiting {BC_WAIT_INVITE_SEC}s for an invite")
                    self.c.chat(BC_ASK_PARTY_TEXT)
                else:
                    self.log(f"BC: other players here ({', '.join(players[:4])}) -> waiting {BC_WAIT_INVITE_SEC}s for an invite")
            else:
                self.log(f"BC: other players here ({', '.join(players[:4])})")
        if b["first_stranger"] is None or now - b["first_stranger"] < BC_WAIT_INVITE_SEC:
            return
        if len(party) > 1 and party[0] != self.name:
            return               # in someone else's party: they invite
        if len(party) >= 5:
            return
        if not b.get("inviting"):
            b["inviting"] = True
            self.log("BC: no invite came -> inviting the players here who aren't in a party")
        counts = b.setdefault("invite_n", {})
        for n in players:
            if n in party or n in b["in_party"]:
                continue
            if now - b["invited"].get(n, 0) < BC_INVITE_EVERY or counts.get(n, 0) >= BC_INVITE_MAX:
                continue
            b["invited"][n] = now
            counts[n] = counts.get(n, 0) + 1
            res = self.ev(B.INVITE_BY_NAME_JS, n)
            self.log(f"BC: party invite {n}: {res}")

    BC_PHASES = {0: "start", 1: "kill monsters", 4: "gate/statue", 2: "finished"}

    def bc_wait_party(self, b, st):
        """B.DELIVER_WAIT_PARTY: at the Archangel with the weapon -> True while a party member that is
        still in the castle is not in the spawn area yet (we hold the weapon and wait)."""
        now = time.time()
        t0 = b.setdefault("dwait_at", now)
        rem = (st or {}).get("rem")
        if b.get("dwait_done"):
            return False
        def done(msg):
            b["dwait_done"] = True
            if b.get("dwait_logged") or msg.startswith("waited"):
                self.log(f"BC: {msg} -> delivering")
            return False
        if now - t0 > B.DELIVER_WAIT_MAX:
            return done(f"waited {B.DELIVER_WAIT_MAX}s for the party")
        if rem is not None and rem < B.DELIVER_MIN_REM:
            return done(f"only {rem}s left on the castle clock")
        r = self.ev(B.PARTY_SPAWN_JS, [*B.ARCHANGEL, B.DELIVER_SPAWN_R, self.name]) or {}
        members = r.get("members") or []
        if members and not b.get("dwait_keys"):
            b["dwait_keys"] = True
            self.log(f"BC: party entry fields: {members[0].get('keys')}")
        my_map = self.c.s.get("map")
        snap = self.hub.snapshot()
        waiting = []
        for m in members:
            ours = snap.get(m["name"])
            if ours and ours.get("map") is not None:          # one of our characters: we know its map
                if ours["map"] != my_map:
                    continue                                  # left the castle -> doesn't count
                if ours.get("x") is not None and dist(ours, *B.ARCHANGEL) <= B.DELIVER_SPAWN_R:
                    continue
            elif m.get("here"):
                continue
            elif m.get("map") is not None and m["map"] != my_map:
                continue                                      # the party data says he's on another map
            if m.get("dead"):
                continue
            waiting.append(m)
        if not waiting:
            return done("whole party in the spawn area")
        if now - b.get("dwait_log_at", 0) > 5:
            b["dwait_log_at"], b["dwait_logged"] = now, True
            who = ", ".join(f"{m['name']} " + (f"({m['x']},{m['y']})" if m.get("visible") else "(not in view)")
                            for m in waiting)
            self.log(f"BC: holding the weapon at the Archangel, waiting for {who} "
                     f"({int(now - t0)}s, {rem}s left on the clock)")
        return True

    def bc_diag(self, b, st):
        """Diagnostics: log every castle state change, and what's on the ground near the statue."""
        s, now = self.c.s, time.time()
        if st:
            key = (st.get("st"), st.get("own"), st.get("wpn"))
            if key != b.get("st_key"):
                b["st_key"] = key
                self.log(f"BC state: phase {st.get('st')} ({self.BC_PHASES.get(st.get('st'), '?')}) | "
                         f"kills {st.get('cur')}/{st.get('max')} | {st.get('rem')}s left | "
                         f"weapon holder {st.get('own')} | weapon num {st.get('wpn')}")
        if dist(s, *B.STATUE) > B.GROUND_NEAR or now - b.get("ground_at", 0) < B.GROUND_LOG_SEC:
            return
        b["ground_at"] = now
        items = self.ev(B.GROUND_JS, B.GROUND_RADIUS)
        if items is None:
            return
        sig = sorted((i["name"], i["g"], i["n"], i["lvl"]) for i in items)
        if sig == b.get("ground_sig") and now - b.get("ground_logged", 0) < B.GROUND_LOG_FORCE_SEC:
            return
        b["ground_sig"], b["ground_logged"] = sig, now
        if not items:
            self.log(f"BC ground: nothing within {B.GROUND_RADIUS} tiles of ({s.get('x')},{s.get('y')})")
            return
        parts = [("zen" if i["money"] else f"{i['name']!r} [{i['g']}/{i['n']} lvl {i['lvl']}]")
                 + f" @({i['x']},{i['y']})" for i in items[:12]]
        more = f" +{len(items) - 12} more" if len(items) > 12 else ""
        self.log(f"BC ground ({len(items)} within {B.GROUND_RADIUS}): " + "; ".join(parts) + more)

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
        self.bc_diag(b, st)
        holder = st.get("own") if st else 255
        self.bc_weapon_shout(b, holder)
        # after a pickup attempt: the bag code may not be 13/19, so also trust "item gone from
        # the ground + the game says someone holds the weapon" (solo / right after our pickup)
        pk = b.get("pick_try")
        if pk and not b.get("we_have"):
            pid, t0, bag0 = pk
            ground = self.ev(B.WEAPON_SCAN_JS, [B.WEAPON[0], B.WEAPON[1], B.WEAPON_NAME_RE]) or []
            if not any(w["id"] == pid for w in ground):
                bag = list(self.ev(B.BAG_JS) or [])
                new = list(bag)
                for x in (bag0 or []):
                    if x in new:
                        new.remove(x)
                real = [x for x in new if any(x.startswith(f"{g}/{n}/") for g, n in B.WEAPON_CODES)]
                if real or (bag0 is None and holder not in (None, 255)):
                    b["we_have"] = True
                    self.log(f"BC: weapon picked up -> new in bag: {new or 'nothing?'}, "
                             f"holder index {holder}")
                    b["pick_try"] = None
                elif new:
                    self.log(f"BC: that was not the weapon (new in bag: {new}) -> rescanning")
                    b["pick_try"] = None
                elif time.time() - t0 > 4:
                    self.log("BC: weapon left the ground but nothing changed for us -> rescanning")
                    b["pick_try"] = None
        in_bag = (self.ev(B.WEAPON_INV_JS, [list(x) for x in B.WEAPON_CODES]) or 0) > 0
        has_weapon = in_bag or b.get("we_have")
        if has_weapon:
            if B.BC_USE_HELPER:
                self.grab(b, False)
                if not self.walk_in_castle(b, *B.ARCHANGEL, radius=2):
                    return True
            else:
                # run back hitting only what blocks the way; at the Archangel: stop fighting, talk
                arrived, _ = self.fight_to(b, *B.ARCHANGEL, 2, "rush", at_goal="hold", weapon=False)
                if not arrived:
                    return True
            if in_bag:
                b["deliver_at"] = None
            else:
                t0 = b.get("deliver_at")
                if t0 and time.time() - t0 > B.DELIVER_TIMEOUT_SEC:
                    self.log(f"BC: still 'delivering' after {B.DELIVER_TIMEOUT_SEC}s and no "
                             "Archangel weapon in the bag -> it was not the weapon, carrying on")
                    b["we_have"], b["deliver_at"] = False, None
                    return True
            if in_bag and B.DELIVER_WAIT_PARTY and self.bc_wait_party(b, st):
                return True                               # holding the weapon until the party is here
            if time.time() - b["talk_at"] > 3:
                if not in_bag and not b.get("deliver_at"):
                    b["deliver_at"] = time.time()
                b["talk_at"] = time.time()
                if s.get("helper"):
                    c.set_helper(False)
                self.log("BC: delivering the weapon to the Archangel")
                self.ev(B.TALK_NPC_JS, B.ARCHANGEL_NPC)
            return True
        if holder not in (None, 255) and not b["rush"] and not b.get("pick_try"):
            b["rush"] = True
            self.log("BC: another player has the weapon -> back to the Archangel")
        if b["rush"]:
            if B.BC_USE_HELPER:
                self.grab(b, False)
                self.walk_in_castle(b, *B.ARCHANGEL, radius=2)
            else:
                self.fight_to(b, *B.ARCHANGEL, 2, "rush", at_goal="hold", weapon=False)
            self.log_rush(b)
            return True
        if st:
            b["kill_phase"] = st.get("st") in (0, 1)
        if not st or st.get("st") not in (0, 1, 4):      # (v7) state 0 = the barrier opens: go right away
            # the in-page loop waits armed ("wait" mode) and starts walking by itself the instant the
            # castle state packet arrives, without waiting for this Python tick
            if not B.BC_USE_HELPER:
                self.grab(b, True, mode="wait", weapon=False, statue=False)
            else:
                self.grab(b, False)
            self.park(s.get("map"), "", *(self.ev(B.WAIT_SPOT_JS, [*B.BC_WAIT, *B.BC_START]) or B.BC_START),
                      radius=2)   # waiting for the castle to start (by the bridge), wiggling a little
            return True
        # castle running: the in-page loop watches the ground 20x/s and picks the weapon up the
        # instant it drops (even while this Python tick is busy). Without the helper it also walks
        # up the corridor to the stand spot by the statue, hitting what is in the way, and guards
        # the spot (statue first, then anything in range) once there.
        if not B.BC_USE_HELPER:
            _, gr = self.fight_to(b, *B.STATUE, B.STATUE_RADIUS, "advance", at_goal="guard",
                                  weapon=True, statue=True)
        else:
            gr = self.grab(b, True)
        if gr and gr.get("chasing"):
            self.helper_async(False)                  # the helper would keep attacking / walking away
            if not b.get("pick_try") or b["pick_try"][0] != gr["id"]:
                bag0 = self.ev(B.BAG_JS)
                b["pick_try"] = (gr["id"], time.time(), list(bag0) if bag0 is not None else None)
                self.log("BC: picking up the Archangel's weapon (in-page grabber)")
            return True
        if gr is None:
            # grabber not answering (page busy / reloading): the old 1-tick path
            weapon = self.ev(B.WEAPON_SCAN_JS, [B.WEAPON[0], B.WEAPON[1], B.WEAPON_NAME_RE]) or []
            if weapon:
                now = time.time()
                w = weapon[0]
                self.helper_async(False)
                if now - b.get("pick_at", 0) > 0.5:
                    b["pick_at"] = now
                    self.log(f"BC: picking up the Archangel's weapon: {w['name']!r} "
                             f"[{w['g']}/{w['n']} lvl {w['lvl']}] matched by {w['by']}, {w['d']:.0f} tiles away")
                if not b.get("pick_try") or b["pick_try"][0] != w["id"]:
                    bag0 = self.ev(B.BAG_JS)
                    b["pick_try"] = (w["id"], now, list(bag0) if bag0 is not None else None)
                self.ev(B.PICKUP_JS, w["id"])
                return True
        if not B.BC_USE_HELPER:
            return True                           # walking / fighting / statue: all in the page
        if gr and gr.get("statue"):
            # the grabber is hitting the Statue of Saint itself (we want the LAST hit: the weapon
            # then belongs to us/our party first) -> keep the helper out of it, for a while
            if gr.get("statueMs", 0) < B.STATUE_SOLO_SEC * 1000:
                self.helper_async(False)
                return True
            if not b.get("statue_solo_over"):
                b["statue_solo_over"] = True
                self.log(f"BC: statue still up after {B.STATUE_SOLO_SEC}s of our own attacks -> MU Helper")
                self.grab(b, True)
        tx, ty = B.STATUE
        if dist(s, tx, ty) <= B.STATUE_RADIUS:
            self.helper_async(True)
            return True
        self.walk_in_castle(b, tx, ty, radius=B.STATUE_RADIUS)
        return True

    def log_rush(self, b):
        """While running back to the Archangel: where are we, every 5 s (to see why we arrive late)."""
        s, now = self.c.s, time.time()
        if now - b.get("rush_log_at", 0) < 5:
            return
        b["rush_log_at"] = now
        b.setdefault("rush_t0", now)
        self.log(f"BC: running to the Archangel: at ({s.get('x')},{s.get('y')}), "
                 f"{dist(s, *B.ARCHANGEL):.0f} tiles left, {now - b['rush_t0']:.0f}s so far, "
                 f"helper {'on' if s.get('helper') else 'off'}")

    def check_msgwin(self):
        """Close the game's message window (OK dialog) whenever one is open, and log its text."""
        now = time.time()
        if now - self.msgwin_at < B.MSGWIN_CHECK_SEC:
            return
        self.msgwin_at = now
        r = self.ev(B.MSGWIN_JS)
        if r:
            self.log(f"message window closed ({r.get('how')}){' - STILL OPEN' if r.get('still') else ''}: {r.get('txt')}")
        open_t = self.ev("(ts) => { const t = window.__store && window.__store.pendingNpcTalk; "
                         "return !!t && ts.includes(t.npcType); }", B.NPC_TALK_CLOSE_TYPES)
        if not open_t:
            self.npc_talk_since = 0
        elif not self.npc_talk_since:
            self.npc_talk_since = now
        elif now - self.npc_talk_since >= B.NPC_TALK_CLOSE_AFTER:
            self.close_npc_talk("stuck on screen")

    def close_npc_talk(self, tag):
        r = self.ev(B.NPC_TALK_CLOSE_JS, B.NPC_TALK_CLOSE_TYPES)
        if r:
            self.log(f"{r.get('name')} dialog closed ({tag}: {r.get('how')})"
                     f"{' - STILL OPEN' if r.get('still') else ''}")
            if not r.get("still"):
                self.npc_talk_since = 0

    def close_dialogs(self, b, tag):
        """The Archangel leaves a dialog on screen after the delivery: log what is open, close the
        game windows, and if a dialog is still there click its OK / Close button."""
        c = self.c
        ui = self.ev(B.OPEN_UI_JS) or {}
        if ui.get("flags"):
            self.log(f"BC: open on screen ({tag}): {ui.get('flags')} | buttons {ui.get('dom')}")
        try:
            c.close_windows()
        except Exception as e:
            self.log(f"BC: close windows: {e!r}")
        after = self.ev(B.OPEN_UI_JS) or {}
        if after.get("flags"):
            hit = self.ev(B.CLICK_OK_JS)
            self.log(f"BC: still open after closing: {after.get('flags')} -> clicked {hit!r}")
            if tag != "1s after the end":      # give the delivery a moment before forcing it shut
                self.close_npc_talk(f"BC {tag}")

    def end_phase(self, b, since_end):
        c, s = self.c, self.c.s
        for t in (1, 5, 15):                  # the dialog can show up a moment after the delivery
            if since_end >= t and t not in b.setdefault("ui_closed", set()):
                b["ui_closed"].add(t)
                self.close_dialogs(b, f"{t}s after the end")
        if in_bc(s.get("map")) and not b["box"] and since_end < B.END_WAIT_SEC:
            if not self.go(s.get("map"), "", *B.ARCHANGEL, radius=4):
                # still on the way back (far up the corridor when someone else delivered):
                # keep walking - boxes are only in range near the Archangel
                self.log_rush(b)
                boxes = [it for it in (self.ev(B.SCAN_JS, [self.hub.friends, 30] + list(B.BOX)) or {}).get("items", [])
                         if it["id"] not in b["tried"]]
                if not boxes:
                    return True
            elif not b.get("arrived_at"):
                b["arrived_at"] = time.time()
                self.log(f"BC: at the Archangel {since_end:.0f}s after the end")
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
            boxes = [it for it in (self.ev(B.SCAN_JS, [self.hub.friends, 30] + list(B.BOX)) or {}).get("items", [])
                     if it["id"] not in b["tried"]]
            if boxes:
                b["box_try"] = (boxes[0]["id"], now, self.inv(B.BOX))
                self.ev(B.PICKUP_JS, boxes[0]["id"])
                return True
            if time.time() - b.get("arrived_at", time.time()) < 25:
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
             if i.get("online") and not i.get("paused") and i.get("level") and n not in skip
             and not i.get("assisted")}          # being carried after a grand reset: no Blood Castle
    # carrying someone, or hunting the Golden Invasion at :50 (no time to climb): may only hold
    pinned = {n for n, i in avail.items() if i.get("pair_lead") or i.get("gi_hunter")}
    rl = hub.reset_level or G.ROUTE[-1][1]
    left_min = (open_at - BC_CLIMB_DEADLINE - now) / 60
    taken, assign, why = dict(taken or {}), {}, {}
    for t, lo, hi in BC_TIERS:                       # holders: already in a castle's range
        inside = sorted((n for n, i in avail.items() if lo <= i["level"] <= hi
                         and not (n in pinned and i["level"] < DEVIAS_MIN_LEVEL)),
                        key=lambda n: (avail[n]["level"], n))
        if inside and t not in taken:
            taken[t] = inside[0]
            assign[inside[0]] = (t, "hold" if avail[inside[0]]["level"] >= DEVIAS_MIN_LEVEL else "climb")
            why[inside[0]] = f"lvl {avail[inside[0]]['level']}"
    # climbers: nearest free castle above them they can reach in time (lowest levels pick first)
    for n in sorted((n for n in avail if n not in assign and n not in pinned), key=lambda n: (avail[n]["level"], n)):
        lvl = avail[n]["level"]
        rate = max(avail[n].get("rate") or BC_DEFAULT_RATE, 1.0)
        for t, lo, hi in BC_TIERS:
            if t in taken or lo <= lvl:
                continue
            need = bc_entry_level(t) - lvl
            if need / rate <= left_min:
                taken[t] = n
                assign[n] = (t, "climb")
                why[n] = f"lvl {lvl} -> {bc_entry_level(t)}, ~{need / rate:.0f} min"
            break   # only the NEAREST free castle above (the next ones are even further)
    # nobody in BC1: the leftover closest to the reset level resets and climbs to it
    if 1 not in taken:
        lo1 = BC_TIERS[0][1]
        best = None
        for n in (n for n in avail if n not in assign and n not in pinned):
            lvl = avail[n]["level"]
            rate = max(avail[n].get("rate") or BC_DEFAULT_RATE, 1.0)
            mins = max(rl - lvl, 0) / rate + BC_RESET_OVERHEAD + bc_entry_level(1) / BC_LOW_RATE
            if mins <= left_min and (best is None or mins < best[0]):
                best = (mins, n, lvl)
        if best:
            mins, n, lvl = best
            taken[1] = n
            assign[n] = (1, "reset")
            why[n] = f"lvl {lvl} -> reset -> {bc_entry_level(1)}, ~{mins:.0f} min"
    return assign, why


# ── castles shared with the other machines (mu_hub claims "bc:<tier>") ──
BC_CLAIM_AFTER_DOOR = 25 * 60   # a castle claim lasts until the door closes + this (castle + delivery)


def remote_bc_taken(hub):
    """Castles another machine already claimed for this event: {tier: "name@machine"}."""
    link = hub.link
    if not link or not link.connected():
        return {}
    out = {}
    for k, c in link.claims().items():
        if k.startswith("bc:") and c.get("machine") != link.machine:
            try:
                out[int(k[3:])] = f"{c.get('owner')}@{c.get('machine')}"
            except ValueError:
                pass
    return out


def plan_with_claims(hub, open_at, taken=None, skip=(), announce=False):
    """build_plan(), then claim every castle at the hub. A castle another machine took first is
    marked taken and the plan is redone for whoever lost it. Hub unreachable -> the plan stays as
    it is (this machine plans among its own characters, Juan's call)."""
    taken, skip = dict(taken or {}), set(skip)
    assign, why = {}, {}
    ttl = max(60, open_at - time.time() + B.BC_DOOR_CLOSE_SEC + BC_CLAIM_AFTER_DOOR)
    for _ in range(4):
        remote = remote_bc_taken(hub)
        a, w = build_plan(hub, open_at, {**remote, **taken}, skip)
        lost = False
        for n, (t, r) in sorted(a.items(), key=lambda kv: kv[1][0]):
            res = hub.link.claim(f"bc:{t}", n, ttl) if hub.link else None
            if res is None or res.get("ok"):
                if res is None and hub.link and not getattr(hub, "bc_claim_warned", False):
                    hub.bc_claim_warned = True
                    log("hub: can't claim castles (hub offline) -> planning only among this machine's characters")
                elif res is not None:
                    hub.bc_claim_warned = False
                assign[n], why[n] = (t, r), w[n] + (" [claimed]" if res else "")
                taken[t] = n
                skip.add(n)
            else:
                log(f"hub: BC{t} already taken by {res.get('owner')}@{res.get('machine')} -> re-planning {n}")
                taken[t] = f"{res.get('owner')}@{res.get('machine')}"
                lost = True
        if not lost:
            break
    remote = remote_bc_taken(hub) if announce else None
    if remote:
        log("hub: castles taken by the other machine: " + ", ".join(f"BC{t} {o}" for t, o in sorted(remote.items())))
    return assign, why


def release_bc_claim(hub, tier, name):
    if hub.link and tier:
        threading.Thread(target=hub.link.release, args=(f"bc:{tier}", name), daemon=True).start()


BC_REPLAN_EVERY = 20   # seconds: re-check characters that had no castle (they may have levelled into one)


def coordinator(hub, agents):
    now = time.time()
    if not hub.bc_on:
        hub.plan = None
        return
    if hub.open_at is None:
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
        assign, why = plan_with_claims(hub, hub.open_at, taken, skip)
        if not assign:
            return
        with hub.lock:
            plan["assign"].update(assign)
        log("BLOOD CASTLE PLAN update: " + " | ".join(
            f"{n}: BC{t} {r} ({why[n]})" for n, (t, r) in sorted(assign.items(), key=lambda kv: kv[1][0])))
        return
    if any(a.bcs for a in agents):
        return       # still busy with the previous castle
    assign, why = plan_with_claims(hub, hub.open_at, announce=True)
    hub.plan = {"key": key, "open_at": hub.open_at, "assign": assign,
                "started": set(), "out": set(), "checked": now}
    opens = datetime.fromtimestamp(hub.open_at).strftime("%H:%M")
    lines = [f"{n}: BC{t} {r} ({why[n]})" for n, (t, r) in sorted(assign.items(), key=lambda kv: kv[1][0])]
    rest = sorted(n for n, i in hub.snapshot().items() if n not in assign)
    log(f"BLOOD CASTLE PLAN (opens {opens}): " + " | ".join(lines or ["nobody"])
        + (f" | keep leveling (re-checked every {BC_REPLAN_EVERY}s): {', '.join(rest)}" if rest else ""))


def read_keys(hub, agents):
    """P = pause/resume everyone, 1..5 = one character. A PAUSE file pauses everyone."""
    try:
        from console_keys import read_keys as get_console_keys
        for k in get_console_keys():
            if k == RESTART_KEY:
                if SUPERVISED:
                    try:
                        RESTART_FILE.write_text("now", encoding="utf-8")
                        log("=== R pressed: restarting the bot now (the supervisor closes and starts it) ===")
                    except OSError as e:
                        log(f"R pressed but could not write the RESTART file: {e!r}")
                else:
                    log("R pressed, but this bot was not started by the supervisor -> "
                        "close it and start run_auto.sh to get restarts")
            elif k == BC_TOGGLE_KEY:
                hub.bc_on = not hub.bc_on
                log("=== BLOOD CASTLE " + ("ON" if hub.bc_on else
                    "OFF (anyone already inside finishes that castle; nobody else goes)") + " ===")
            elif k == BB_TOGGLE_KEY:
                hub.bb_on = not hub.bb_on
                log("=== BIG BOSS " + ("ON" if hub.bb_on else
                    "OFF (anyone at the boss stops and goes back to normal; no reset hold)") + " ===")
            elif k == G.PAUSE_KEY:
                if all(a.name in hub.paused for a in agents):
                    hub.paused.clear()
                    hub.pause_all = False
                    log("=== RESUMING EVERYONE ===")
                else:
                    hub.pause_all = True
                    hub.paused.update(a.name for a in agents)
                    log("=== PAUSING EVERYONE (P again to resume; windows stay minimized, "
                        "use 1-5 to open one) ===")
            elif k.isdigit() and 1 <= int(k) <= len(agents):
                a = agents[int(k) - 1]
                if a.name in hub.paused:
                    hub.paused.discard(a.name)
                else:
                    hub.pause_all = False
                    hub.paused.add(a.name)
    except Exception:
        pass
    flag = G.BASE_DIR / G.PAUSE_FILE
    if flag.exists():
        if not getattr(read_keys, "file_paused", False):
            read_keys.file_paused = True
            hub.pause_all = True
            hub.paused.update(a.name for a in agents)
            log("=== PAUSE file found: everyone paused (delete it to resume) ===")
    elif getattr(read_keys, "file_paused", False):
        read_keys.file_paused = False
        hub.pause_all = False
        hub.paused.clear()
        log("=== PAUSE file removed: resuming everyone ===")


def main():
    G.disable_console_quickedit()
    lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        lock.bind(("127.0.0.1", 47291))   # same lock as mu_group_bot.py: never both at once
    except OSError:
        print("A MU bot (group or auto) is already running in another window. Close that one first.")
        if not SUPERVISED:              # the supervisor retries by itself: no prompt to block it
            input("Press Enter to exit...")
        return EXIT_OTHER_BOT
    try:
        STOP_BOT_FILE.unlink()          # a leftover from an earlier stop must not stop this run
    except OSError:
        pass
    log("=== MU auto bot starting ===" + (" (under the supervisor)" if SUPERVISED else ""))
    names = [a["character"] for a in G.ACCOUNTS]
    hub = Hub(names)
    log(R.startup_line(names))
    hub.link = HUB.make_link(hub.snapshot, log)
    if not hub.link:
        log("hub: no hub.json -> this machine runs alone")
    if CH.CHAT_ENABLED:
        hub.chat = CH.ChatHub(names)
        log(hub.chat.startup_line())
    agents = [Agent(acc, i, hub) for i, acc in enumerate(G.ACCOUNTS)]
    threads = []
    for a in agents:
        t = threading.Thread(target=a.run, name=a.name, daemon=True)
        t.start()
        threads.append(t)
        time.sleep(3)    # stagger the browser launches
    log(f"Blood Castle is {'ON' if hub.bc_on else 'OFF'} (B in this window switches it)")
    log(f"Big Boss is {'ON' if hub.bb_on else 'OFF'} (O in this window switches it)")
    log("Keys in this window: P = pause/resume everyone, B = Blood Castle on/off, O = Big Boss on/off, R = restart now, "
        + ", ".join(f"{i + 1} = {a.name}" for i, a in enumerate(agents)) + " (pause/resume one)")
    last_summary = 0
    rc = EXIT_ALL_DEAD             # the loop ends by itself only when every character thread stopped
    try:
        while any(t.is_alive() for t in threads):
            if STOP_BOT_FILE.exists():
                log("=== STOP_BOT file -> closing the browsers and exiting (restart / stop requested) ===")
                rc = EXIT_STOPPED
                break
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
                if hub.link:
                    log(hub.link.summary())
            time.sleep(0.3)
        else:
            log("every character stopped -> exiting")
    except KeyboardInterrupt:
        log("stopped by user")
        rc = EXIT_STOPPED
    finally:
        hub.stop = True
        if hub.link:
            hub.link.stop = True
        for t in threads:
            t.join(timeout=10)
    try:
        STOP_BOT_FILE.unlink()
    except OSError:
        pass
    return rc


if __name__ == "__main__":
    try:
        sys.exit(main() or 0)
    except KeyboardInterrupt:
        sys.exit(0)

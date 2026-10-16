"""
Golden Invasion hunter (used by mu_auto_bot.py) — every hour at :50 for GI_CHARS (wizzardo).

Timeline (start = the event start read from the event panel, normally :50:00):
  start-60s  PREP   warp to Noria, buy Large Healing Potions at Amy until the Q slot holds 255
  start-30s  WAIT   stand ~GI_WAIT_DIST tiles outside the Noria safe zone
  start      HUNT   read the per-map counts (InvasionMonsters packet): stay in Noria, or warp to Devias
                    when Devias has more. Hunt until the event is over (GI_HUNT_SEC = 15 min, or every
                    golden dead) or until no golden mob was in view for GI_IDLE_SEC (90 s):
                      * targets are ONLY golden mobs ("Golden ...") and other players; normal mobs ignored
                      * GI_PVP_MODE "defense" (default): players are fought ONLY when they hit us; the
                        dragon / boxes / goldens are taken even with players around. The three PvP rules
                        below are GI_PVP_MODE "attack" only (except fighting back, which both do)
                      * Golden Dragon in view + players within GI_PVP_RADIUS of us or of the dragon ->
                        kill the players first (one at a time), then the dragon, then grab the +5
                      * no PvP over small golden mobs (a small golden with a player next to it is skipped)
                      * a player that hits us is fought back
                      * Ice Storm (skill GI_SKILL) on every target
                      * mages: Soul Barrier (GI_BARRIER_SKILL) on themselves every time they step out of
                        a safe zone (waiting spot, after a warp, after a respawn), from PREP to the end
                      * potion (hotkey Q) every 0.2 s while a player has hit us in the last 2 s
                      * Box of Kundun on the ground -> pick it up; a locked box (someone else's) ->
                        keep trying for GI_BOX_WAIT_SEC while fighting players who come close
                      * nothing in view -> walk to the nearest part of the map not searched yet
                        (areas with leftover loot count as already searched)
                      * never runs back to town; after a death it respawns and carries on
                      * /pkclear right after every kill (GI_PKCLEAR_ON_KILL: as soon as we turn outlaw)
  door-5min  a hunter with a Blood Castle in the plan ends the hunt GI_BC_LEAVE_SEC (5 min) before the door
                    opens (on a Golden Dragon: when the door opens) -> AFTER, then cloak + bar, Devias at :59:30
  end        AFTER  /pkclear if still outlaw (sells ONE Zen Chest at the Lorencia bar first if zen is short),
                    then back to normal (leveling / Blood Castle if still in a castle's range)

The fight logic runs INSIDE the game page every GI_LOOP_MS (50 ms = 20x/s); Python only handles the
timeline, warps, shopping, searching and logging. While the hunt runs, mu_recorder also records
everything to mu_record.log (GI_RECORD).
"""
import math
import re
import time

import mu_bc as B
import mu_group_bot as G
import roster as R

# ───────────────────────────── CONFIG ─────────────────────────────
GI_ENABLED       = True
GI_CHARS         = {"wizzardo", "bruto"}   # always hunt (lower case; bruto: Juan 2026-10-11) ...
GI_MAX_STATS     = True             # ... plus every character with ALL 4 stats >= GI_MIN_STAT (Juan 2026-10-11)
GI_MIN_STAT      = 65000            # "max stats": STR, AGI, VIT and ENE all at least this
GI_STATS_EVERY   = 300              # seconds between stat checks
GI_SKILLS        = {}               # fixed skill per character (lower-case name -> skill number); empty = by class:
                                    # Ice Storm if owned (Soul Masters), else the Dark Knight combo, else the selected skill
GI_START_MINUTE  = 50               # fallback when the event panel can't be read (PC clock)
GI_PREP_SEC      = 45               # start preparing this long before the start (:48:30: potions in Noria, then the waiting map)
GI_WAIT_SEC      = 15               # be at the waiting spot this long before the start (:49:30)
GI_HUNT_SEC      = 900              # the event lasts 15 min (:50 -> :05): hunt until the end ... (Juan 2026-10-11:
                                    # dragons are much tougher now, 3 min was too short)
GI_IDLE_SEC      = 90               # ... or until no golden mob was in view this long (dead / warping don't count)
GI_BC_LEAVE_SEC  = 300              # a hunter that has a Blood Castle in the plan ends the hunt this long before the
                                   # door opens (:55) -> cloak at the bar, Devias at BC_DEVIAS_SEC (Juan 2026-10-14).
                                   # Fighting over a Golden Dragon (dragon lock): stays until the door opens, then goes
GI_SEARCH_AGE    = 180              # a searched area counts as unsearched again after this long (long hunts)
GI_MIN_LEFT      = 10               # (Juan 2026-10-14) fewer golden mobs than this left in total -> stop searching and
                                    # end the hunt (a fight / dragon lock in progress is finished first; 0 = off)
GI_SPACING       = 8                # (Juan 2026-10-14) while searching, our hunters keep at least this many tiles
                                    # apart: too close -> step away, and search goals near a teammate are skipped.
                                    # Not applied in fights / dragon calls / loot (they need to be on the target). 0 = off
GI_EXPLORED_SWITCH = True           # (Juan 2026-10-14) whole map searched -> warp to the other GI map with the most
                                    # golden mobs left right now (none left elsewhere -> keep patrolling this one)
GI_SWITCH_MIN_STAY = 60             # ... but not sooner than this many seconds after landing on this map (no ping-pong)
GI_WAIT_DIST     = 20               # waiting spot: this many tiles outside the Noria safe zone
GI_SKILL         = 39               # default for GI_SKILLS (Ice Storm)
GI_BARRIER       = True             # mages: Soul Barrier on yourself every time you leave a safe zone (Juan 2026-10-11)
GI_BARRIER_SKILL = 16               # Soul Barrier (only characters that have it cast it)
GI_FORTITUDE_SKILL = 48             # Dark Knight: Greater Fortitude (Swell Life), cast like the barrier ...
GI_FORTITUDE_REFRESH = 60           # ... and again before a fight with a player if it's older than this (s)
GI_MAGE_SKILL    = 39               # anyone who has Ice Storm uses it (Soul Masters, like wizzardo)
GI_KNIGHT_COMBO  = [[22, 20, 19, 21, 23], 43, 41]   # Dark Knight combo (Juan 2026-10-11):
                                    # weapon skill (Cyclone 22 / Lunge 20 / ... first one owned) ->
                                    # Death Stab 43 -> Twisting Slash 41, repeated on every target
GI_COMBO_STEP_MS = 700              # next combo skill when the server showed the last one, or after this
GI_BARRIER_TRIES = 4                # casts per safe-zone exit before giving up (each waits ~1.2 s for the server)
GI_POTION_KEY    = 0                # item hotkey index: 0 = Q
GI_POTION_ITEM   = (14, 3)          # Large Healing Potion (used if the Q slot can't be read)
GI_POTION_STACK  = 255              # one full stack ...
GI_POTION_TOTAL  = 510              # ... keep TWO full stacks in the bag (Juan 2026-10-11: 211 used in 1.5 min)
GI_POTION_NPC    = 253              # Amy, Potion Girl (two of them in Noria)
GI_CLEAR         = False             # (Juan 2026-10-12) dragon about to die + 1..3 other players hitting it -> kill them
GI_CLEAR_ETA     = 24               # ... when the dragon has this many seconds left or fewer (estimated from its HP bar)
GI_CLEAR_MIN_ETA = 6                # ... but not under this: then everybody stays on the dragon
GI_CLEAR_MAX     = 3                # more players than this hitting the dragon -> don't bother, stay on the dragon
GI_CLEAR_KILL_SEC = 8               # a player not dead after this long -> back to the dragon, don't try him again
GI_CLEAR_LEASH   = 8                # only players within this many tiles of the dragon (no chasing)
GI_POT_HP        = 0.8              # (Juan 2026-10-12) also drink whenever HP < 80 % of max, players near or not
GI_CALL          = True             # (Juan 2026-10-12) a hunter that sees a Golden Dragon calls the others: any of our
GI_CALL_DIST     = 50               # hunters on the same map within this many tiles of it (and no dragon of its own)
GI_CALL_FRESH    = 5                # walks over to help (more chances at the last hit + backup vs players);
                                    # a sighting older than this (s) no longer counts
GI_POT_EVERY_MS  = 400              # potion every 0.4 s (was 0.2: the server refused every 2nd one, "That item
                                    # cannot be used like that" - same healing, half the requests) ...
GI_HIT_WINDOW_MS = 2000             # ... while a player has hit us in the last 2 s
GI_PVP_MODE      = "defense"        # how we treat other players (Juan, 2026-10-12):
                                    #   "defense" = only fight back players who hit us; never attack anyone on sight
                                    #               (dragons, boxes and small goldens are taken even with players around)
                                    #   "attack"  = old behaviour: kill players at a dragon / guarding a locked box first
GI_PVP_RADIUS    = 12               # players this close to us or to the dragon -> fight them first (attack mode)
GI_VIEW          = 30               # tiles: ignore anything further away
GI_BOX_WAIT_SEC  = 8               # a locked Box of Kundun: keep trying this long
GI_DRAGON_LOCK   = True             # (Juan, 2026-10-12) fighting over a Golden Dragon (+5): never leave it - no event end,
                                   # idle end, map switch or Blood Castle - until we have the +5 box, it's gone, or we die
GI_DRAGON_LOCK_DIST = 16            # "fighting over it" = targeting the dragon, or a player while the dragon is this close
GI_DRAGON_BOX_GRACE = 8             # dragon gone from view: wait this long for its box to show up before letting go
GI_DRAGON_LOCK_MAX = 900            # safety cap: the lock never keeps a hunt alive longer than this past the normal end
GI_CLOSE          = True            # (Juan 2026-10-14) close in for the kill: when the golden we're hitting (dragon
GI_CLOSE_DIST     = 1               # or small one) is about to die, walk to this many tiles from it and keep casting
GI_CLOSE_ETA      = 5               # from there, so we stand on the box when it drops. "About to die" = this many
GI_CLOSE_HP       = 0.15            # seconds left (from its HP bar) or HP under this fraction. Ice Storm hits the
GI_CLOSE_STUCK_MS = 2000            # same from 1 tile; no progress walking for this long -> cast from where we are
GI_SPAM_PICK_MS  = 300              # loot spam after a dragon dies: pickup request this often (was 50 ms: 12 refused
                                    # pickups/s on a locked box -> server snapped bruto back 7x, 2026-10-15 18:54)
GI_WALK_REPEAT_SEC = 1.5            # Python re-sends the same walk target at most this often (no re-path every tick)
GI_CLOSE_REPATH_MS = 1200           # close-in: re-send the walk to the same tile at most this often (was 400 ms)
GI_BIG_BOX_RADIUS = 10               # a free Box +5 this close is picked up BEFORE fighting players (Juan, 2026-10-12)
GI_IGNORE_AFTER  = 20               # a target that got this many casts with <= 1 seen by the server ...
GI_IGNORE_SEC    = 20               # ... is skipped this long (2026-10-11 23:50: 218 casts at the Noria gate, 1 seen)
GI_CAST_GAP_MS   = 0                # min. ms between two cast requests (was 100); 0 = refill as soon as the client takes one
GI_AREA_HIT_RADIUS = 6             # attacker detection (v10): a player's area skill aimed within this many tiles
GI_AREA_HIT_MS   = 1200             # of me (or cast from that close) followed by damage on me within this = hostile
GI_LOOP_MS       = 20               # in-page fight loop period (50x/s; was 50 ms - Juan 2026-10-12: cast as soon as the
                                   # client is free). The client itself holds a cast until the previous animation ends.
GI_SHOW_WINDOW   = True             # True = restore the game windows during the invasion. Back to True
                                   # (Juan, 2026-10-12): with them minimized (00:50-08:50 runs) the characters
                                   # landed ~1 hit per 3 s instead of ~0.4 s (see claude/gi-loot-drop-2026-10-12.md).
                                   # False = they stay minimized, and a window a relog restores is minimized again
GI_SEARCH_CELL   = 24               # search grid cell size (tiles)
GI_SIGHT         = 18               # tiles around us that count as "searched"
GI_LOOT_RADIUS   = 10               # leftover loot marks this area as searched
GI_WALK_STUCK    = 4                # seconds without getting closer to a search point -> pick another
GI_RECORD        = True             # record the hunt with mu_recorder
GI_SELL_CHEST_FOR_PK = True         # /pkclear short of zen -> sell ONE Zen Chest at the Lorencia bar
GI_PKCLEAR_ON_KILL = True           # /pkclear right after every kill during the hunt, not just at the end
                                    # (Juan 2026-10-13: new server rule, outlaws can drop gear)
GI_PKCLEAR_WAIT_SEC = 61            # server rule (14/10): /pkclear only works 60 s after your last kill -> wait this
                                    # long after the last kill (or what the server's "podés limpiar el PK en N
                                    # segundos" says) before sending it (2026-10-16: every try came 0-6 s after a kill)
GI_PKCLEAR_RETRY_SEC = 8            # a /pkclear that didn't work: next one no sooner than this
GI_PKCLEAR_MAX_TRIES = 3            # failed tries (after the wait) per kill before leaving it to the end of the hunt
GI_SELF_DEF_ONLY = True             # (2026-10-16) a player only counts as "hit me" when the SERVER says so ("Self
                                    # defense is initiated by X's attack to <me>"). Our own guesses (an area spell
                                    # near us + damage) flagged Axwjere / ayudanteBK / DkPegasus, who never hit
                                    # wizzardo -> we attacked first -> 3 kills = 2nd Stage Outlaw. Killing a player
                                    # we have self-defense against gives NO PK (CocoLoco, same fight).
GI_SELF_DEF_SEC  = 60               # our self-defense right lasts about this long (server ends it with "Self defense
                                    # of <me> against X")
GI_OUTLAW_CAUTION = True            # (Juan 2026-10-16) while we're outlaw and /pkclear hasn't gone through: never
                                    # start a fight with anyone (no dragon clearing, no attack mode, no box guarding);
                                    # only fight back players the server gave us self-defense against
MAP_LORENCIA, MAP_DEVIAS, MAP_NORIA = 0, 2, 3
DEVIAS_WARPS     = ["Devias3", "Devias"]   # /move aliases to try (Devias 3 landed at (225,229) for 2500 zen)
DEVIAS_OUT_WARPS = ["Devias4", "Devias2", "Devias3"]  # stuck in the Devias safe zone (respawn): warp out, next one if it fails
GI_TOWN_MAX_SEC  = 3                # in the Devias safe zone this long during the hunt -> warp out (Juan 2026-10-11)
NORIA_WARP       = "Noria"
NORIA_TOWN       = (175, 100)       # roughly the middle of Noria town (search for the safe-zone edge from here)
# Before the start our hunters wait scattered over these maps (Juan 2026-10-11), in this order:
# 1st hunter Devias, 2nd Noria, 3rd Lorencia, 4th Devias, ... (Devias needs level 20 -> Noria instead)
GI_PRE_MAPS      = [MAP_DEVIAS, MAP_NORIA, MAP_LORENCIA]
GI_PRE_WARP      = {MAP_DEVIAS: "Devias3", MAP_NORIA: "Noria", MAP_LORENCIA: "Lorencia"}
GI_PRE_CENTER    = {MAP_NORIA: NORIA_TOWN}   # town middle to search the safe-zone edge from (others: where we land)
# At the start, GI_FOCUS_COUNT of them go to the FOCUS map: the map named in the "[Map] Golden invasion!" banner
# (every Golden Dragon we ever saw was in the banner's map), or the one with the most golden mobs when there is
# no banner for one of our maps. The others hunt where they waited.
GI_FOCUS_COUNT   = 3
GI_BANNER_WAIT   = 6                # seconds to wait for the banner / the first real counts at the start
MAP_NAMES        = {MAP_LORENCIA: "Lorencia", MAP_DEVIAS: "Devias", MAP_NORIA: "Noria", 7: "Atlans", 8: "Tarkan"}
DEVIAS_MIN_LEVEL = 20
NORIA_MIN_LEVEL  = 10
# ──────────────────────────────────────────────────────────────────

# Event panel row: countdown "mm:ss" before the start, "Active" / "N left" while it runs
GI_TIMER_JS = r"""
() => {
  for (const e of document.querySelectorAll('.event-row-label')) {
    const t = ((e.parentElement || e).textContent || '').replace(/\s+/g, ' ').trim();
    if (!/Golden Invasion/i.test(t)) continue;
    const m = t.match(/(\d+):(\d+)(?::(\d+))?/);
    if (m) { const n = m.slice(1).filter(v => v !== undefined).map(Number);
             return {secs: n.length === 3 ? n[0] * 3600 + n[1] * 60 + n[2] : n[0] * 60 + n[1], text: t}; }
    const l = t.match(/(\d+)\s*left/i);
    return {active: true, left: l ? Number(l[1]) : null, text: t};
  }
  return null;
}
"""

# The fight loop. Installed once per page (a reload installs it again); switched on/off with .on
GI_INSTALL_JS = r"""
(cfg) => {
  const W = window;
  if (W.__gi && W.__gi.version === cfg.version) { W.__gi.cfg = cfg; return 'kept'; }
  if (W.__gi && W.__gi.timer) clearInterval(W.__gi.timer);
  if (W.__gi && W.__gi.obs) { try { W.__gi.obs.disconnect(); } catch (e) {} }
  const G = W.__gi = {version: cfg.version, cfg, on: false, counts: null, countsAt: 0, myId: null,
    potUntil: 0, lastPot: 0, pots: 0, casts: 0, myCasts: 0, lastCast: 0, lastHp: null,
    hostile: new Map(), target: null, targetIsPlayer: false, lastTargetName: null,
    box: null, boxFirstFail: new Map(), boxGaveUp: new Set(), lastPick: 0, pickFails: 0,
    lootSpamUntil: 0, lootSpamPos: null, lastSeenDragon: null,
    mHp: new Map(), closing: null, closeBest: 0, closeProg: 0, closeWalkAt: 0, closeTileXY: null, closeFail: new Set(),
    hitsOnMe: 0, events: [], status: {}, recentTargets: new Map(), chatSeen: new WeakSet(),
    barrier: false, wasSafe: null, bQueue: [], bTries: 0, bAt: 0, bDone: 0, bEvents: [], buffAt: {},
    comboIdx: 0, comboAt: 0, attackSkill: null, ignored: new Map(), tStats: new Map(), tPrev: null, safeSkipped: 0,
    areaCasts: new Map(), hitMatch: new Map(),
    selfDef: new Map(), aggro: new Map(), pk: {kills: 0, stage: null, clearAt: 0, killAt: 0, cleanedAt: 0, failAt: 0, failMsg: ''},
    dHp: [], dHpId: null, dragonId: null, dragonPos: null, dHitters: new Map(), clr: null, clrFailed: new Set(), clrDragon: null, clrNote: ''};
  const SKILL_NAMES = {16: 'Soul Barrier', 48: 'Greater Fortitude'};
  const sname = n => SKILL_NAMES[n] || ('skill ' + n);
  const bev = s => { if (G.bEvents.length < 50) G.bEvents.push(s); };
  const ev = s => { if (G.events.length < 300) G.events.push(s); };
  const bus = W.__eventBus;
  const u8 = d => new Uint8Array(d.buffer || d, d.byteOffset || 0, d.byteLength);
  if (bus) {
    bus.on('InvasionMonsters', d => { try {
      const u = u8(d), m = {}, n = u[6];
      for (let i = 0; i < n; i++) { const o = 7 + i * 3; m[u[o]] = (u[o + 1] << 8) | u[o + 2]; }
      G.counts = {total: (u[4] << 8) | u[5], maps: m}; G.countsAt = Date.now();
    } catch (e) {} });
    // c3 09 19 00 <skill> <caster u16> <target u16>: a player's skill on me = a hit
    bus.on('SkillAnimation', d => { try {
      const u = u8(d), caster = (u[5] << 8) | u[6], target = ((u[7] & 0x7f) << 8) | u[8];
      const skill = (u[3] << 8) | u[4];
      // my own buff (Soul Barrier / Greater Fortitude) went through
      if (G.bQueue.length && G.myId !== null && caster === G.myId && skill === G.bQueue[0]) {
        G.buffAt[skill] = Date.now(); G.bQueue.shift(); G.bDone++; bev(sname(skill) + ' cast (try ' + G.bTries + ')');
        G.bTries = 0; G.bAt = 0;
      }
      if (!G.on || G.myId === null) return;
      if (caster === G.myId) { G.myCasts++; G.lastMySkill = skill; G.lastMySkillAt = Date.now(); return; }
      if (target !== G.myId) return;
      const e = (W.__world.byNetId && W.__world.byNetId.get ? W.__world.byNetId.get(caster) : null)
        || W.__world.netObjsQuery.entities.find(x => x.netId === caster);
      if (e && e.npcType === undefined) { G.markHostile(e, 'skill ' + skill); G.hitsOnMe++; }
    } catch (e) {} });
    // dragon HP bar (c1 06 fc <netId u16> <hp 0-255>) -> time-to-death estimate (G.dragonEta)
    bus.on('MonsterHealthStatus', d => { try { const u = u8(d);
      const id = ((u[3] & 0x7f) << 8) | u[4];
      if (G.dragonId === null || id !== G.dragonId) return;
      if (G.dHpId !== id) { G.dHpId = id; G.dHp = []; }
      const now = Date.now(); G.dHp.push([now, u[5]]);
      while (G.dHp.length && now - G.dHp[0][0] > 15000) G.dHp.shift();
    } catch (e) {} });
    // HP bar of ANY monster we hit -> G.mHp (close in for the kill, GI_CLOSE)
    bus.on('MonsterHealthStatus', d => { try { const u = u8(d);
      const id = ((u[3] & 0x7f) << 8) | u[4], now = Date.now();
      let h = G.mHp.get(id); if (!h) { h = []; G.mHp.set(id, h); }
      h.push([now, u[5]]);
      while (h.length && now - h[0][0] > 15000) h.shift();
      if (G.mHp.size > 300) for (const [k, v] of G.mHp) if (!v.length || now - v[v.length - 1][0] > 20000) G.mHp.delete(k);
    } catch (e) {} });
    // who is hitting the dragon: a player's skill on it, an area skill on/next to it, a plain attack on it
    G.dragonHit = (caster, how) => {
      if (caster === G.myId) return;
      const e = G.ent(caster);
      if (e && e.npcType === undefined && e.transform) G.dHitters.set(caster, Date.now());
    };
    bus.on('SkillAnimation', d => { try { const u = u8(d);
      if (G.dragonId !== null && (((u[7] & 0x7f) << 8) | u[8]) === G.dragonId) G.dragonHit((u[5] << 8) | u[6], 'skill');
    } catch (e) {} });
    bus.on('ObjectAnimation', d => { try { const u = u8(d);
      if (G.dragonId !== null && u.length >= 9 && (((u[7] & 0x7f) << 8) | u[8]) === G.dragonId) G.dragonHit(((u[3] & 0x7f) << 8) | u[4], 'attack');
    } catch (e) {} });
    bus.on('AreaSkillAnimation', d => { try { const u = u8(d);
      if (G.dragonId === null || !G.dragonPos) return;
      const caster = ((u[5] & 0x7f) << 8) | u[6], e = G.ent(caster);
      if (!e || !e.transform) return;
      if (dist({x: u[7], y: u[8]}, G.dragonPos) <= 4 || dist(P(e), G.dragonPos) <= 4) G.dragonHit(caster, 'area');
    } catch (e) {} });
    // c3 0a 1e <skill u16> <caster u16> <x> <y> <rot>: area skills name no target -> remember where
    // players cast them; an ObjectHit on me right after ties the hit to them (see ObjectHit)
    bus.on('AreaSkillAnimation', d => { try { const u = u8(d);
      const caster = ((u[5] & 0x7f) << 8) | u[6];
      if (G.on && G.myId !== null && caster === G.myId) {
        G.myCasts++; G.lastMySkill = (u[3] << 8) | u[4]; G.lastMySkillAt = Date.now(); return; }
      if (!G.on) return;
      const e = G.ent(caster);
      if (e && e.npcType === undefined && e.transform)
        G.areaCasts.set(caster, {at: Date.now(), x: u[7], y: u[8], skill: (u[3] << 8) | u[4]});
    } catch (e) {} });
    // c1 09 18 <attacker u16> <dir> <anim> <target u16>: a normal (no skill) attack; on me = hostile
    bus.on('ObjectAnimation', d => { try { const u = u8(d);
      if (!G.on || G.myId === null || u.length < 9) return;
      const att = ((u[3] & 0x7f) << 8) | u[4], tgt = ((u[7] & 0x7f) << 8) | u[8];
      if (tgt !== G.myId || att === G.myId) return;
      const e = G.ent(att);
      if (e && e.npcType === undefined) { G.markHostile(e, 'normal attack'); G.hitsOnMe++; }
    } catch (e) {} });
    // c1 0a 11 <obj u16> <hp dmg u16> <kind> <shield dmg u16>: damage on me -> potions; players that
    // cast an area skill on/next to me in the last cfg.areaHitMs are the attackers (with a monster
    // right next to me the hit could be the monster's: then it takes 2 such hits within 3 s)
    bus.on('ObjectHit', d => { try { const u = u8(d);
      if (!G.on || G.myId === null || u.length < 10) return;
      if ((((u[3] & 0x7f) << 8) | u[4]) !== G.myId) return;
      const dmg = ((u[5] << 8) | u[6]) + ((u[8] << 8) | u[9]);
      if (dmg <= 0) return;
      const now = Date.now(), w = W.__world, h = w && w.playerEntity;
      if (!h || !h.transform) return;
      const me = P(h), r = G.cfg.areaHitRadius || 4, win = G.cfg.areaHitMs || 1200;
      let monsterNext = false;
      for (const e of w.netObjsQuery.entities)
        if (e.monsterAnimation && !e.dying && e.transform && dist(P(e), me) <= 3) { monsterNext = true; break; }
      for (const [id, c] of G.areaCasts) {
        if (now - c.at > win) { if (now - c.at > 5000) G.areaCasts.delete(id); continue; }
        const e = G.ent(id);
        if (!e || !e.transform || e.dying) continue;
        if (dist({x: c.x, y: c.y}, me) > r && dist(P(e), me) > r) continue;
        if (monsterNext) {
          const m = G.hitMatch.get(id);
          if (!m || now - m > 3000) { G.hitMatch.set(id, now); continue; }
        }
        G.hitMatch.delete(id);
        G.markHostile(e, 'area skill ' + c.skill);
      }
    } catch (e) {} });
    bus.on('ItemPickUpRequestFailed', () => { G.pickFails++;
      if (G.box !== null && !G.boxFirstFail.has(G.box)) { G.boxFirstFail.set(G.box, Date.now()); ev('box locked (someone else\'s) -> trying for ' + G.cfg.boxWait / 1000 + ' s'); } });
  }
  const dist = (a, b) => Math.hypot(a.x - b.x, a.y - b.y);
  const P = e => ({x: e.transform.pos.x, y: e.transform.pos.z});
  G.ent = id => { const w = W.__world; if (!w) return null;
    return (w.byNetId && w.byNetId.get ? w.byNetId.get(id) : null) || w.netObjsQuery.entities.find(x => x.netId === id) || null; };
  // seconds until death from an HP history [[t, hp0-255], ...] (same estimate as the dragon's), null = unknown
  G.etaOf = (h, now) => {
    if (!h || h.length < 2) return null;
    const last = h[h.length - 1], first = h.find(x => last[0] - x[0] <= 10000) || h[0];
    const dt = (last[0] - first[0]) / 1000, dh = first[1] - last[1];
    return (dt >= 2 && dh > 0) ? Math.max(0, last[1] / (dh / dt) - (now - last[0]) / 1000) : null;
  };
  // walkable tile GI_CLOSE_DIST from the target, the one nearest to me
  G.closeTile = (w, tp, me, r) => {
    let best = null; const tx = Math.round(tp.x), ty = Math.round(tp.y);
    for (let dx = -r; dx <= r; dx++) for (let dy = -r; dy <= r; dy++) {
      if (Math.max(Math.abs(dx), Math.abs(dy)) !== r) continue;
      const x = tx + dx, y = ty + dy;
      if (x < 3 || y < 3 || x > 252 || y > 252 || !w.isWalkable(x, y)) continue;
      const d = Math.hypot(x - me.x, y - me.y);
      if (!best || d < best.d) best = {x, y, d};
    }
    return best;
  };
  // a player hit me: target them right away (also potions); logged once per attack spell
  G.markHostile = (e, how) => {
    const now = Date.now(), name = (e.objectNameInWorld || '').trim();
    if (G.cfg.ours && G.cfg.ours.some(n => n.toLowerCase() === name.toLowerCase())) return;
    const prev = G.hostile.get(e.netId);
    G.hostile.set(e.netId, now); G.potUntil = now + G.cfg.hitWindow;
    if (!prev || now - prev > 8000) ev(`attacked by ${name} (${how})`);
  };
  // Buffs on myself (Soul Barrier for mages, Greater Fortitude for knights) every time I step out of a safe
  // zone (also when switched on outside, and after a respawn: the respawn is in town). G.wantBuff() queues
  // a refresh before a fight. True = casting, skip the fight this tick.
  const owns = (s, n) => !!(n && s.skills && s.skills.some(k => k.number === n));
  G.wantBuff = (s, n, why) => {
    if (!owns(s, n) || G.bQueue.includes(n)) return;
    G.bQueue.push(n); bev(why + ' -> ' + sname(n));
  };
  G.barrierTick = (now, w, s) => {
    const cfg = G.cfg, h = w.playerEntity, pd = s.playerData || {};
    const buffs = cfg.buffs || [];
    if (!G.barrier || !buffs.length || !h || !w.getTerrainFlag) { G.wasSafe = null; G.bQueue = []; return false; }
    if (h.dying || (pd.currentHP || 0) <= 0) { G.wasSafe = true; G.bQueue = []; return false; }
    G.myId = h.netId;
    const me = P(h), safe = (w.getTerrainFlag(Math.round(me.x), Math.round(me.y)) & 1) === 1;
    // a warp: other map, or a jump of more than 15 tiles on the same map (e.g. Devias 2/3/4)
    const warped = G.lastMap !== undefined && (w.mapIndex !== G.lastMap ||
      (G.lastPos && Math.hypot(me.x - G.lastPos.x, me.y - G.lastPos.y) > 15));
    G.lastMap = w.mapIndex; G.lastPos = me;
    if (!safe && G.wasSafe !== false)
      for (const n of buffs) G.wantBuff(s, n, `left the safe zone at (${Math.round(me.x)},${Math.round(me.y)})`);
    else if (!safe && warped)
      for (const n of buffs) G.wantBuff(s, n, `warped to (${Math.round(me.x)},${Math.round(me.y)}) on map ${w.mapIndex}`);
    G.wasSafe = safe;
    if (safe) { G.bQueue = []; return false; }
    if (!G.bQueue.length) return false;
    const n = G.bQueue[0];
    if (now - G.bAt < 1200) return true;                  // waiting for the server to show the cast
    if (G.bTries >= cfg.barrierTries) { G.bQueue.shift(); bev(sname(n) + ': no cast seen after ' + G.bTries + ' tries -> giving up'); G.bTries = 0; return false; }
    G.bTries++; G.bAt = now;
    try {
      if (s.currentSkill !== n) s.selectSkill(n);
      w.attackTarget = null;
      w.castRequest = {target: h, point: {x: me.x, y: me.y}, forced: false, pvp: false};
    } catch (e) { bev(sname(n) + ' error: ' + String(e).slice(0, 120)); }
    return true;
  };
  // attack skill: GI_SKILLS for this character, else Ice Storm if owned, else the knight combo
  // (weapon skill -> Death Stab -> Twisting Slash, each owned one in turn), else the selected skill
  G.pickSkill = (now, s) => {
    const cfg = G.cfg;
    if (cfg.skill && owns(s, cfg.skill)) return cfg.skill;
    if (owns(s, cfg.mageSkill)) return cfg.mageSkill;
    const combo = [];
    for (const step of cfg.combo || []) {
      const opts = Array.isArray(step) ? step : [step];
      const n = opts.find(o => owns(s, o));
      if (n) combo.push(n);
    }
    if (!combo.length) return null;
    if (G.comboIdx >= combo.length) G.comboIdx = 0;
    const cur = combo[G.comboIdx];
    // the server showed the current one (after we selected it) or it took too long -> next one
    if ((G.lastMySkill === cur && G.lastMySkillAt > G.comboAt) || now - G.comboAt > cfg.comboStep) {
      if (G.comboAt) G.comboIdx = (G.comboIdx + 1) % combo.length;
      G.comboAt = now;
    }
    return combo[G.comboIdx];
  };
  // Server chat (system lines), read every tick even when the hunt is off. The client WRAPS long lines into
  // separate chatLines entries ("Self defense is initiated by CocoLoco's attack to" + "wizzardo!"), so each
  // new line is also tried joined with the next one. Feeds:
  //   G.selfDef  name -> until : players the server gave ME self-defense against (legal to fight back)
  //   G.pk       my PK state   : kills, stage, when /pkclear becomes possible, cleaned
  const lc = x => String(x || '').trim().toLowerCase();
  const lineTxt = l => (typeof l === 'string' ? l : [l.text, l.message, l.msg, l.content].filter(Boolean).join(' ')).trim();
  G.scanChat = (now, soc, myName) => {
    const lines = ((soc && soc.chatLines) || []).slice(-14), me = lc(myName);
    if (!me) return;
    for (let i = 0; i < lines.length; i++) {
      const l = lines[i];
      if (!l || typeof l !== 'object' || G.chatSeen.has(l)) continue;
      const t1 = lineTxt(l), nx = lines[i + 1];
      const last = i === lines.length - 1;
      // the last line may be the first half of a wrapped message: wait for its second half (max 2 s)
      if (last && !/[.!]$/.test(t1) && /(attack to|against|limpiar el PK en \d+|have been|kills,?)$/i.test(t1)) {
        if (!l.__giWait) l.__giWait = now;
        if (now - l.__giWait < 2000) continue;
      }
      G.chatSeen.add(l);
      const t2 = nx && typeof nx === 'object' ? t1 + ' ' + lineTxt(nx) : t1;
      let m;
      if ((m = t2.match(/Self defense is initiated by ([^\s']+)'s attack to ([^\s!.]+)/i)) ||
          (m = t1.match(/Self defense is initiated by ([^\s']+)'s attack to ([^\s!.]+)/i))) {
        const by = lc(m[1]), to = lc(m[2]);
        if (to === me && !G.selfDef.has(by)) {
          G.selfDef.set(by, now + G.cfg.selfDefMs); G.potUntil = now + G.cfg.hitWindow;
          const e = w0().netObjsQuery.entities.find(x => lc(x.objectNameInWorld) === by);
          if (e) G.hostile.set(e.netId, now);
          ev(`attacked by ${m[1]} (server: self-defense, ${G.cfg.selfDefMs / 1000} s)`);
        } else if (to === me) G.selfDef.set(by, now + G.cfg.selfDefMs);
        else if (by === me && !G.aggro.has(to)) {
          G.aggro.set(to, now);
          ev(`WARNING: I hit ${m[2]} first (server gave HIM self-defense) -> killing him = PK`);
        }
        continue;
      }
      if ((m = t2.match(/Self defense of ([^\s]+) against ([^\s.!]+)/i)) || (m = t1.match(/Self defense of ([^\s]+) against ([^\s.!]+)/i))) {
        if (lc(m[1]) === me) G.selfDef.delete(lc(m[2]));
        else if (lc(m[2]) === me) G.aggro.delete(lc(m[1]));
        continue;
      }
      if ((m = t1.match(/^(\S+)\s*:\s*(Outlaw Warning|\S+ Stage Outlaw|Commoner|Murderer|Hero.*)$/i)) && lc(m[1]) === me) {
        G.pk.stage = m[2];
        if (/commoner|hero/i.test(m[2])) { G.pk.kills = 0; G.pk.clearAt = 0; }
        continue;
      }
      if ((m = t1.match(/^PK:\s*(\d+)\s*kills/i))) {
        const k = Number(m[1]);
        if (k > G.pk.kills) { G.pk.killAt = now; G.pk.clearAt = Math.max(G.pk.clearAt, now + 60000); }
        G.pk.kills = k; continue;
      }
      if ((m = t2.match(/limpiar el PK en (\d+)\s*segundos/i))) {
        G.pk.clearAt = now + Number(m[1]) * 1000; G.pk.failAt = now; G.pk.failMsg = 'wait ' + m[1] + ' s'; continue;
      }
      if ((m = t2.match(/\[\/pkclear\]\s*(\S+) kills have been cleaned/i)) && lc(m[1]) === me) {
        G.pk.kills = 0; G.pk.clearAt = 0; G.pk.cleanedAt = now; G.pk.stage = 'Commoner'; continue;
      }
      if (/\/pkclear|pkclear/i.test(t1) && !/^\S+\s*:\s*\/pkclear/i.test(t1)) { G.pk.failAt = now; G.pk.failMsg = t2.slice(0, 120); }
    }
    for (const [n, t] of G.selfDef) if (now > t) G.selfDef.delete(n);
    for (const [n, t] of G.aggro) if (now - t > 120000) G.aggro.delete(n);
  };
  const w0 = () => W.__world;
  // legal to fight back: the server gave me self-defense against him
  G.legal = (name, now) => (G.selfDef.get(lc(name)) || 0) > now;
  // outlaw = hero state above 3 (Commoner) or kills the server told us about and not cleaned yet
  G.outlaw = h => !!((h && h.heroState > 3) || G.pk.kills > 0);
  G.tick = () => {
    const now = Date.now(), cfg = G.cfg, w = W.__world, s = W.__store, soc = W.__social;
    if (!w || !s) return;
    try { G.scanChat(now, soc, (s.playerData || {}).name); } catch (e) { G.err = 'chat: ' + String(e).slice(0, 150); }
    if (G.barrierTick(now, w, s)) return;
    if (!G.on) return;
    const h = w.playerEntity, pd = s.playerData || {};
    if (!h) { G.status = {dead: true}; return; }
    G.myId = h.netId;
    const hp = pd.currentHP || 0;
    if (h.dying || hp <= 0) { G.status = {dead: true}; G.target = null; G.lastHp = hp; return; }
    const me = P(h);
    const ours = new Set(cfg.ours.map(n => n.toLowerCase()));
    const party = new Set(((soc && soc.partyMembers) || []).map(m => (m.name || '').trim().toLowerCase()));
    const players = [], goldens = [], boxes = [], loot = [];
    const isSafe = p => !!(w.getTerrainFlag && (w.getTerrainFlag(Math.round(p.x), Math.round(p.y)) & 1) === 1);
    const meSafe = isSafe(me);
    for (const [id, t] of G.ignored) if (now > t) G.ignored.delete(id);
    let safeN = 0;
    for (const e of w.netObjsQuery.entities) {
      if (e.localPlayer || !e.transform) continue;
      const p = P(e), d = dist(p, me);
      if (d > cfg.view) continue;
      if (e.droppedItem) {
        const it = e.droppedItem;
        if (it.isMoney) continue;
        const lvl = it.item ? it.item.lvl : null;
        if (it.group === 14 && it.num === 11 && lvl >= 8) boxes.push({e, p, d, lvl});
        else loot.push([Math.round(p.x), Math.round(p.y)]);
        continue;
      }
      if (e.dying) continue;
      const name = (e.objectNameInWorld || '').trim();
      if (e.npcType === undefined) {
        if (e.netId === undefined || !(e.playerAnimation || e.skin !== undefined)) continue;
        if (ours.has(name.toLowerCase()) || party.has(name.toLowerCase())) continue;
        if (isSafe(p)) { safeN++; continue; }               // in a safe zone: can't be hit -> never a target
        if (G.ignored.has(e.netId)) continue;               // the server kept ignoring our casts on it
        players.push({e, p, d, name});
      } else if (e.monsterAnimation && /^Golden/i.test(name)) {
        if (G.ignored.has(e.netId)) continue;
        goldens.push({e, p, d, name, dragon: e.npcType === 79});
      }
    }
    // HP going down with a player close -> potions (a monster hit alone does not start them)
    if (G.lastHp !== null && hp < G.lastHp && players.some(x => x.d <= cfg.pvpRadius)) G.potUntil = now + cfg.hitWindow;
    G.lastHp = hp;
    const lowHp = !!(cfg.potHp && pd.maxHP && hp < cfg.potHp * pd.maxHP);
    if ((now < G.potUntil || lowHp) && now - G.lastPot >= cfg.potEvery) {
      G.lastPot = now;
      try { s.useItemHotkey(cfg.potKey); G.pots++; } catch (e) {}
    }
    for (const [id, t] of G.hostile) if (now - t > 8000) G.hostile.delete(id);
    const byD = (a, b) => a.d - b.d;
    players.sort(byD); goldens.sort(byD); boxes.sort((a, b) => (b.lvl - a.lvl) || (a.d - b.d));
    const dragon = goldens.find(g => g.dragon) || null;
    
    // Box +5 Rush Logic
    if (dragon) {
      G.lastSeenDragon = { p: dragon.p };
    } else if (G.lastSeenDragon) {
      G.lootSpamUntil = now + 3000;
      G.lootSpamPos = [Math.round(G.lastSeenDragon.p.x), Math.round(G.lastSeenDragon.p.y)];
      G.lastSeenDragon = null;
      ev('dragon dead -> rushing coordinates to spam Box +5 loot');
    }
    const isSpammingLoot = now < G.lootSpamUntil;

    // another of our hunters called us to its dragon (GI_CALL_JS): walk there (Python), skip the small stuff
    const helping = !!(G.call && now < G.call.until && !dragon);
    // ── clearing a dying dragon (Juan 2026-10-12) ──
    if (dragon) { if (G.dragonId !== dragon.e.netId) { G.dragonId = dragon.e.netId; G.dHitters.clear(); G.clr = null;
                    G.clrFailed = new Set(); G.clrNote = ''; } G.dragonPos = dragon.p; }
    else { G.dragonId = null; G.dragonPos = null; }
    let eta = null;
    if (dragon && G.dHpId === dragon.e.netId && G.dHp.length >= 2) {
      const last = G.dHp[G.dHp.length - 1], first = G.dHp.find(x => last[0] - x[0] <= 10000) || G.dHp[0];
      const dt = (last[0] - first[0]) / 1000, dh = first[1] - last[1];
      if (dt >= 3 && dh > 0) eta = Math.max(0, last[1] / (dh / dt) - (now - last[0]) / 1000);
    }
    G.dragonEta = eta;
    let clearT = null;
    const outlaw = G.outlaw(h), caution = cfg.outlawCaution && outlaw;
    if (cfg.clear && !caution && dragon && eta !== null && eta <= cfg.clearEta && eta >= cfg.clearMinEta) {
      for (const [id, t] of G.dHitters) if (now - t > 3000) G.dHitters.delete(id);
      const enemies = players.filter(x => G.dHitters.has(x.e.netId) && dist(x.p, dragon.p) <= cfg.clearLeash);
      if (enemies.length > cfg.clearMax) {
        if (G.clrNote !== 'many') { G.clrNote = 'many'; ev(`dragon ~${Math.round(eta)} s from dying, ${enemies.length} players hitting it -> too many, staying on the dragon`); }
      } else {
        if (G.clr && now - G.clr.since > cfg.clearKillMs && enemies.some(x => x.e.netId === G.clr.id)) {
          G.clrFailed.add(G.clr.id); ev(`could not kill ${G.clr.name} in ${cfg.clearKillMs / 1000} s -> back to the dragon`); G.clr = null;
        }
        // same order on all our characters -> they gang up on the same player
        const cand = enemies.filter(x => !G.clrFailed.has(x.e.netId))
                            .sort((a, b) => (dist(a.p, dragon.p) - dist(b.p, dragon.p)) || (a.e.netId - b.e.netId));
        if (cand.length) {
          const cur = G.clr && cand.find(x => x.e.netId === G.clr.id);
          clearT = cur || cand[0];
          if (!cur) { G.clr = {id: clearT.e.netId, name: clearT.name, since: now};
            ev(`dragon ~${Math.round(eta)} s from dying, ${enemies.length} player(s) hitting it -> clearing ${clearT.name}`); }
        } else G.clr = null;
      }
    } else if (G.clr) G.clr = null;
    // hostiles: players active against me; with selfDefOnly only those the server gave me self-defense against
    // (also counts one the server just named even if our hit detection missed him)
    const hostiles = players.filter(x => cfg.selfDefOnly
      ? G.legal(x.name, now) && (G.hostile.has(x.e.netId) || (G.selfDef.get(lc(x.name)) || 0) - cfg.selfDefMs > now - 8000)
      : G.hostile.has(x.e.netId));
    const defense = cfg.pvpMode !== 'attack' || caution;
    let target = null, why = '', pick = null;
    // 0. a free Box +5 close by: grab it before any fight (2026-10-11: two +5s lost while fighting players)
    // spam mode: any distance, and a box refused once is still retried until cfg.boxWait runs out (then given up)
    const ffOk = b => { const ff = G.boxFirstFail.get(b.e.netId); return !ff || now - ff <= cfg.boxWait; };
    const bigBox = boxes.find(b => b.lvl >= 12 && !G.boxGaveUp.has(b.e.netId) &&
                     (isSpammingLoot ? ffOk(b) : (b.d <= cfg.bigBoxRadius && !G.boxFirstFail.has(b.e.netId))));
    
    if (isSpammingLoot && !bigBox) { 
      target = null; pick = null; why = 'rushing to dragon death location'; 
    } else if (bigBox) { pick = bigBox; }
    else if (defense && hostiles.length && !(dragon && cfg.dragonLock)) {
      // defense mode: only players who hit me are ever targets, and they come first
      target = hostiles[0]; why = 'hit me';
    } else if (defense && dragon && cfg.dragonLock) {
      // dragon lock (Juan 2026-10-12): never leave a +5 dragon - fight back only against players at it
      const hot = hostiles.filter(x => x.d <= cfg.pvpRadius || dist(x.p, dragon.p) <= cfg.pvpRadius);
      if (clearT) { target = clearT; why = `clearing the dragon (~${Math.round(eta)} s left)`; }
      else if (hot.length) { target = hot[0]; why = 'hit me (at a dragon)'; }
      else { target = dragon; why = 'dragon'; }
    } else if (dragon) {
      // 1. at a dragon: players first (whoever hit me first, else the closest), then the dragon
      //    (defense mode: nobody hit me -> no threats, straight to the dragon)
      const threats = defense ? [] : players.filter(x => x.d <= cfg.pvpRadius || dist(x.p, dragon.p) <= cfg.pvpRadius);
      const hot = threats.filter(x => G.hostile.has(x.e.netId));
      if (clearT) { target = clearT; why = `clearing the dragon (~${Math.round(eta)} s left)`; }
      else if (hot.length) { target = hot[0]; why = 'hit me (at a dragon)'; }
      else if (threats.length) { target = threats[0]; why = 'player at the dragon'; }
      else { target = dragon; why = 'dragon'; }
    } else if (hostiles.length) {
      // 2. anyone who hit me
      target = hostiles[0]; why = 'hit me';
    } else {
      // 3. Boxes of Kundun: free ones are picked up; a locked one is retried for cfg.boxWait while
      //    players coming close to it get fought
      for (const b of boxes) {
        if (G.boxGaveUp.has(b.e.netId)) continue;
        if (helping && b.lvl < 12) continue;               // on the way to a called dragon: only +5 boxes
        const ff = G.boxFirstFail.get(b.e.netId);
        if (ff && now - ff > cfg.boxWait) { G.boxGaveUp.add(b.e.netId); ev('gave up a locked Box +' + (b.lvl - 7)); continue; }
        pick = b; break;
      }
      if (pick && !defense && !caution && G.boxFirstFail.has(pick.e.netId)) {
        const guard = players.filter(x => dist(x.p, pick.p) <= cfg.pvpRadius || x.d <= cfg.pvpRadius);
        if (guard.length) { target = guard[0]; why = 'guarding a locked box'; pick = null; }
      }
      // 4. small golden mobs with no player next to them (no PvP over small ones);
      //    defense mode: those first, then contested ones too (we don't fight the players there)
      if (!target && !pick && !helping) {
        const free = goldens.filter(g => !players.some(x => dist(x.p, g.p) <= cfg.pvpRadius));
        if (free.length) { target = free[0]; why = 'golden'; }
        else if (defense && goldens.length) { target = goldens[0]; why = 'golden (players near)'; }
      }
    }
    let isPlayer = !!(target && target.e.npcType === undefined);
    // last gate: never START a fight we'd get PK for (defense mode, or outlaw with caution on).
    // Dragon clearing (GI_CLEAR) is a deliberate attack and passes when we're not outlaw.
    if (isPlayer && !G.legal(target.name, now) && (caution || (defense && cfg.selfDefOnly && target !== clearT))) {
      const k = 'no-sd:' + target.name;
      if (G.lastGate !== k) { G.lastGate = k; ev(`not attacking ${target.name}: no self-defense against him` + (outlaw ? ' (I am outlaw)' : '')); }
      target = dragon || goldens[0] || null; why = target ? (dragon ? 'dragon' : 'golden') : ''; isPlayer = false;
    }
    if (cfg.refreshSkill && (isPlayer || players.some(x => x.d <= cfg.pvpRadius))
        && now - (G.buffAt[cfg.refreshSkill] || 0) > cfg.refreshMs) {
      G.buffAt[cfg.refreshSkill] = now;                    // one request per refresh period
      G.wantBuff(s, cfg.refreshSkill, 'player close');
    }
    // casts the server ignores: count sent vs seen per target (only outside a safe zone, where
    // casting can work at all); too many ignored -> skip that target for cfg.ignoreMs
    if (G.tPrev && !meSafe) {
      const st = G.tStats.get(G.tPrev.id) || {sent: 0, seen: 0, name: G.tPrev.name};
      st.sent += G.casts - G.tPrev.casts; st.seen += G.myCasts - G.tPrev.seen;
      G.tStats.set(G.tPrev.id, st);
      if (st.sent >= cfg.ignoreAfter && st.seen <= 1 && !(cfg.dragonLock && G.tPrev.dragon)) {
        G.ignored.set(G.tPrev.id, now + cfg.ignoreMs); G.tStats.delete(G.tPrev.id);
        ev(`skipping ${st.name} for ${cfg.ignoreMs / 1000} s: ${st.sent} casts, ${st.seen} seen by the server`);
        if (target && target.e.netId === G.tPrev.id) { target = null; why = ''; isPlayer = false; }
      } else if (st.seen > 1) G.tStats.delete(G.tPrev.id);   // it works on this one
    }
    G.tPrev = target ? {id: target.e.netId, name: target.name, casts: G.casts, seen: G.myCasts, dragon: target.e.npcType === 79} : null;
    if (G.tStats.size > 200) G.tStats.clear();
    const tName = target ? target.name : null;
    if (tName !== G.lastTargetName) {
      G.lastTargetName = tName;
      if (target) ev(`target -> ${tName}${isPlayer ? ' [player]' : ''} (${why}, ${Math.round(target.d)} tiles)`);
    }
    if (isPlayer) G.recentTargets.set(target.e.netId, {name: tName, at: now});
    for (const [id, r] of G.recentTargets) {
      const e = w.netObjsQuery.entities.find(x => x.netId === id);
      if (e && e.dying) { ev('player DIED: ' + r.name); G.recentTargets.delete(id); }
      else if (now - r.at > 10000) G.recentTargets.delete(id);
    }
    // ── close in for the kill (Juan 2026-10-14): golden about to die -> walk to GI_CLOSE_DIST tiles of it ──
    let closeTo = null, closeInfo = '';
    if (cfg.close && target && !isPlayer) {
      const id = target.e.netId, hist = G.mHp.get(id);
      const hpNow = hist && hist.length ? hist[hist.length - 1][1] : null;
      const tEta = G.etaOf(hist, now);
      const dying = (hpNow !== null && hpNow <= cfg.closeHp * 255) || (tEta !== null && tEta <= cfg.closeEta);
      if (dying && !G.closeFail.has(id)) {
        const need = target.d > cfg.closeDist + (G.closing === id ? 0.8 : 0.9);
        closeInfo = `hp ${hpNow === null ? '?' : Math.round(hpNow / 2.55)}%` + (tEta !== null ? `, ~${tEta.toFixed(1)} s left` : '');
        if (need) closeTo = target;
        else if (G.closing === id) { ev(`next to ${target.name} (${target.d.toFixed(1)} tiles) -> casting from here`); G.closing = null; }
      }
    }
    if (closeTo) {
      const id = closeTo.e.netId;
      if (G.closing !== id) {
        G.closing = id; G.closeBest = closeTo.d; G.closeProg = now; G.closeWalkAt = 0; G.closeTileXY = null;
        ev(`closing in on ${closeTo.name} (${closeInfo}, ${closeTo.d.toFixed(1)} tiles) -> ${cfg.closeDist} tile`);
      }
      if (closeTo.d < G.closeBest - 0.3) { G.closeBest = closeTo.d; G.closeProg = now; }
      else if (now - G.closeProg > cfg.closeStuckMs) {
        G.closeFail.add(id); G.closing = null; closeTo = null;
        ev(`could not get next to ${target.name} in ${cfg.closeStuckMs / 1000} s -> casting from here`);
        if (G.closeFail.size > 100) G.closeFail.clear();
      }
    } else if (G.closing !== null && (!target || target.e.netId !== G.closing || isPlayer)) G.closing = null;
    if (closeTo) {
      // walk instead of casting this tick (a cast still in its animation finishes; nothing new is queued)
      G.box = null;
      w.castRequest = null; w.attackTarget = null;
      const t = G.closeTile(w, closeTo.p, me, cfg.closeDist);
      if (!t) { G.closeFail.add(closeTo.e.netId); G.closing = null; ev(`no free tile next to ${closeTo.name} -> casting from here`); }
      else if (!G.closeTileXY || G.closeTileXY[0] !== t.x || G.closeTileXY[1] !== t.y || now - G.closeWalkAt > (cfg.closeRepathMs || 1200)) {
        G.closeTileXY = [t.x, t.y]; G.closeWalkAt = now;
        try {
          const mv = h.playerMoveTo, pf = h.pathfinding;
          mv.point.x = t.x; mv.point.y = t.y; mv.handled = false; mv.sendToServer = true;
          pf.from.x = h.transform.pos.x; pf.from.y = h.transform.pos.z; pf.to.x = t.x; pf.to.y = t.y; pf.calculated = false;
        } catch (e) { G.closeFail.add(closeTo.e.netId); G.closing = null; ev('close-in walk error: ' + String(e).slice(0, 100)); }
      }
    } else if (target) {
      G.box = null;
      try { const sk = G.pickSkill(now, s); G.attackSkill = sk; if (sk && s.currentSkill !== sk) s.selectSkill(sk); } catch (e) {}
      if (!isPlayer) w.attackTarget = target.e;
      if (!w.castRequest && now - G.lastCast >= (cfg.castGap || 0)) {
        w.castRequest = {target: target.e, point: {x: target.p.x, y: target.p.y}, forced: false, pvp: isPlayer};
        G.lastCast = now; G.casts++;
      }
    } else if (pick) {
      if (G.box !== pick.e.netId) { G.box = pick.e.netId; ev(`picking up Box +${pick.lvl - 7} (${Math.round(pick.d)} tiles)`); }
      const pickDelay = (isSpammingLoot && pick.lvl >= 12) ? (cfg.spamPickMs || 300) : 250;
      if (now - G.lastPick > pickDelay) { G.lastPick = now; w.pickupTarget = pick.e; }
    } else {
      G.box = null;
    }
    if (!target && G.target !== null) { w.attackTarget = null; w.castRequest = null; }
    G.target = target ? target.e.netId : null;
    G.status = {safePlayers: safeN, ignored: G.ignored.size, mode: (isSpammingLoot && !pick) ? 'spam_loot' : (target ? (isPlayer ? 'pvp' : (target.e.npcType === 79 ? 'dragon' : 'golden')) : (pick ? 'box' : 'search')),
      spamPos: G.lootSpamPos, closing: !!closeTo, target: tName, why, players: players.length, near: players.filter(x => x.d <= cfg.pvpRadius).length,
      goldens: goldens.length, dragon: dragon ? [Math.round(dragon.p.x), Math.round(dragon.p.y), Math.round(dragon.d)] : null,
      helping, eta: eta === null ? null : Math.round(eta), hitters: G.dHitters.size, boxes: boxes.length, loot: loot.slice(0, 20), hp, maxHp: pd.maxHP, heroState: h.heroState, outlaw, pkKills: G.pk.kills, pkStage: G.pk.stage,
      pkClearIn: G.pk.clearAt ? Math.max(0, Math.round((G.pk.clearAt - now) / 1000)) : 0, selfDef: [...G.selfDef.keys()],
      big5: (() => { const b = boxes.find(x => x.lvl >= 12 && !G.boxGaveUp.has(x.e.netId)); return b ? Math.round(b.d) : null; })(),
      attackSkill: G.attackSkill, pots: G.pots, casts: G.casts, myCasts: G.myCasts, hitsOnMe: G.hitsOnMe, pickFails: G.pickFails,
      skill: s.currentSkill, map: w.mapIndex, x: Math.round(me.x), y: Math.round(me.y),
      safe: w.getTerrainFlag ? (w.getTerrainFlag(Math.round(me.x), Math.round(me.y)) & 1) === 1 : null};
  };
  // "[Devias] Golden invasion!" banner -> G.banner = {name, at} (the map with the Golden Dragons)
  try {
    G.obs = new MutationObserver(muts => { for (const m of muts) for (const n of m.addedNodes) {
      const t = (n.textContent || ''); if (t.length > 300) continue;
      const b = t.match(/\[(Lorencia|Devias|Noria|Atlans|Tarkan)\] Golden invasion!/i);
      if (b) G.banner = {name: b[1], at: Date.now()};
    } });
    G.obs.observe(document.body, {childList: true, subtree: true});
  } catch (e) {}
  G.timer = setInterval(() => { try { G.tick(); } catch (e) { G.err = String(e).slice(0, 200); } }, cfg.loopMs);
  return 'installed';
}
"""

GI_STATUS_JS = """() => { const G = window.__gi; if (!G) return null;
  const ev = G.events.splice(0); return {status: G.status, events: ev, counts: G.counts, countsAt: G.countsAt,
  err: G.err || null, on: G.on, banner: G.banner || null}; }"""

# Soul Barrier watcher on/off; returns its events (null = not installed, e.g. the page reloaded)
GI_BARRIER_JS = """(on) => { const G = window.__gi; if (!G || !G.barrierTick) return null; G.barrier = on;
  if (!on) { G.bQueue = []; G.wasSafe = null; } return G.bEvents.splice(0); }"""

# a dragon call from another of our hunters: {x, y, ms} (null = none)
GI_CALL_JS = """(c) => { const G = window.__gi; if (!G) return false;
  G.call = c ? {x: c.x, y: c.y, until: Date.now() + c.ms} : null; return true; }"""

GI_SET_JS = """(on) => { const G = window.__gi; if (!G) return false; G.on = on;
  if (!on) { const w = window.__world; if (w) { w.castRequest = null; w.attackTarget = null; w.pickupTarget = null; }
             G.target = null; G.box = null; G.potUntil = 0; }
  return true; }"""

# potion slot behind the Q hotkey: {slot, count, group, num, lvl}
POTION_SLOT_JS = """([key, g, n]) => { const s = window.__store, items = s.playerData.items || [];
  let i = -1; try { i = s.hotkeyItemSlot(key); } catch (e) {}
  const hk = i;
  let it = i >= 0 ? items[i] : null;
  if (!it) { i = items.findIndex((x, j) => j >= 12 && x && x.group === g && x.num === n); it = i >= 0 ? items[i] : null; }
  const cnt = x => x.durability ?? x.count ?? 1;
  const G = it ? it.group : g, N = it ? it.num : n;
  const total = items.reduce((t, x, j) => t + (j >= 12 && x && x.group === G && x.num === N ? cnt(x) : 0), 0);
  if (!it) return {slot: -1, hk, count: 0, total, group: g, num: n, lvl: 0};
  return {slot: i, hk, count: cnt(it), total, group: it.group, num: it.num, lvl: it.lvl || 0}; }"""

SHOP_FIND_JS = """([g, n, l]) => { const shop = window.__store.npcShop;
  if (!shop || !shop.items.some(Boolean)) return null;
  let i = shop.items.findIndex(it => it && it.group === g && it.num === n && (l === null || it.lvl === l));
  if (i < 0) i = shop.items.findIndex(it => it && it.group === g && it.num === n);
  return i; }"""

# a point GI_WAIT_DIST tiles outside the safe zone, searched along 16 directions from the town
WAIT_SPOT_JS = r"""
([cx, cy, extra]) => {
  const w = window.__world; if (!w || !w.getTerrainFlag) return null;
  const SAFE = 1;
  const safe = (x, y) => (w.getTerrainFlag(x, y) & SAFE) === SAFE;
  const ok = (x, y) => x > 2 && y > 2 && x < 253 && y < 253 && w.isWalkable(x, y);
  const h = w.playerEntity;
  const here = h ? [Math.floor(h.transform.pos.x), Math.floor(h.transform.pos.z)] : [cx, cy];
  const out = [];
  for (let k = 0; k < 16; k++) {
    const a = k * Math.PI / 8, dx = Math.cos(a), dy = Math.sin(a);
    let edge = -1;
    for (let r = 1; r < 90; r++) {
      const x = Math.round(cx + dx * r), y = Math.round(cy + dy * r);
      if (!ok(x, y)) continue;
      if (!safe(x, y)) { edge = r; break; }
    }
    if (edge < 0) continue;
    for (const r of [edge + extra, edge + extra - 4, edge + extra + 4, edge + extra - 8]) {
      const x = Math.round(cx + dx * r), y = Math.round(cy + dy * r);
      if (ok(x, y) && !safe(x, y)) { out.push({x, y, edge, score: dx + dy}); break; }
    }
  }
  out.sort((a, b) => b.score - a.score);     // prefer south-east (your Noria runs went that way)
  return {spots: out.slice(0, 8), hereSafe: h ? safe(here[0], here[1]) : null, hereFlag: h ? w.getTerrainFlag(here[0], here[1]) : null};
}
"""

WALKABLE_JS = """(pts) => { const w = window.__world; if (!w) return null;
  return pts.map(([x, y]) => { for (const [dx, dy] of [[0,0],[3,0],[-3,0],[0,3],[0,-3],[5,5],[-5,-5],[5,-5],[-5,5]]) {
      const a = x + dx, b = y + dy; if (a > 2 && b > 2 && a < 253 && b < 253 && w.isWalkable(a, b)) return [a, b]; }
    return null; }); }"""

# STR / AGI / VIT / ENE (field names differ between clients: take the first one that exists)
STATS_JS = """() => { const p = (window.__store && window.__store.playerData) || {};
  const f = (...k) => { for (const x of k) if (typeof p[x] === 'number') return p[x]; return null; };
  return {str: f('str', 'strength'), agi: f('agi', 'agility', 'dex', 'dexterity'), vit: f('sta', 'vit', 'vitality'),
          ene: f('eng', 'ene', 'energy'), keys: Object.keys(p).filter(k => typeof p[k] === 'number').join(',')}; }"""

HERO_STATE_JS = "() => { const h = window.__world && window.__world.playerEntity; return h ? h.heroState : null; }"

# my PK state: hero state + what the server's chat told the fight loop (kills, stage, seconds until /pkclear
# is allowed). The hero state alone misread once (09:55:25 2026-10-16: "not outlaw" 100 s after 2nd Stage).
PK_STATE_JS = """() => { const h = window.__world && window.__world.playerEntity, G = window.__gi, now = Date.now();
  const hs = h ? h.heroState : null;
  if (!G || !G.pk) return {hs, kills: null, outlaw: hs !== null && hs !== undefined ? hs > 3 : null, clearIn: 0};
  return {hs, kills: G.pk.kills, stage: G.pk.stage, outlaw: !!((hs > 3) || G.pk.kills > 0),
          clearIn: G.pk.clearAt ? Math.max(0, Math.round((G.pk.clearAt - now) / 1000)) : 0,
          cleanedAgo: G.pk.cleanedAt ? Math.round((now - G.pk.cleanedAt) / 1000) : null}; }"""

BAG_BOXES_JS = """() => { const r = {}; (window.__store.playerData.items || []).forEach((it, i) => {
  if (i >= 12 && it && it.group === 14 && it.num === 11 && it.lvl >= 8) r[it.lvl] = (r[it.lvl] || 0) + 1; }); return r; }"""

ZEN_CHEST_SLOT_JS = """() => (window.__store.playerData.items || []).findIndex((it, i) =>
  i >= 12 && it && it.group === 14 && it.num === 11 && it.lvl === 4)"""


def box_name(lvl):
    return f"+{int(lvl) - 7}"


class GoldenInvasion:
    """One per hunting character. tick() returns True while the invasion owns the character."""

    def __init__(self, agent):
        self.a = agent
        self.max_stats = None        # True/False once read (GI_MAX_STATS)
        self.stats_at = 0
        self.stats_logged = None
        self.phase = "idle"
        self.start_at = None
        self.timer_at = 0
        self.last_text = None
        self.hour_done = None        # start_at of the invasion we already handled
        self.potions_ok = False
        self.wait_spot = None
        self.map_target = None
        self.goal = None
        self.goal_best = 1e9
        self.goal_progress_at = 0
        self.last_warp = 0
        self.warp_tries = 0
        self.status_at = 0
        self.stats = {}
        self.bag0 = {}
        self.deaths = 0
        self.was_dead = False
        self.pk_tries = 0
        self.pk_sent_at = 0          # GI_PKCLEAR_ON_KILL: last /pkclear sent during the hunt
        self.pk_live_tries = 0       # ... tries since we last turned outlaw
        self.pk_live_state = None    # ... hero state when we sent it
        self.pk_kill_at = 0          # when we last saw a new kill (hero state / kill count went up)
        self.pk_kills_seen = 0       # kill count (or hero state) at that time
        self.pk_wait_logged = 0
        self.after_step = None
        self.last_status_log = 0

    # ── helpers ──
    def log(self, msg):
        self.a.log("GI: " + msg)

    @property
    def c(self):
        return self.a.c

    def ev(self, js, arg=None):
        return self.a.ev(js, arg)

    def active(self):
        return self.phase not in ("idle",)

    def sleep(self):
        return 0.25 if self.phase in ("prep", "wait", "hunt", "after") else None

    def read_timer(self):
        now = time.time()
        if now - self.timer_at < 5 and self.start_at:
            return
        self.timer_at = now
        t = self.ev(GI_TIMER_JS)
        if t and t.get("secs") is not None:
            self.start_at = now + t["secs"]
        elif t and t.get("active"):
            if not self.start_at or self.start_at > now:
                lt = time.localtime(now)
                since = (lt.tm_min - GI_START_MINUTE) % 60 * 60 + lt.tm_sec
                self.start_at = now - since
        else:
            lt = time.localtime(now)                      # no panel: assume :50 on this PC's clock
            mins = (GI_START_MINUTE - lt.tm_min) % 60
            self.start_at = now + mins * 60 - lt.tm_sec
        if t and t.get("text") != self.last_text and t.get("active"):
            self.last_text = t.get("text")

    def check_stats(self):
        """Every GI_STATS_EVERY s: are all 4 stats >= GI_MIN_STAT? (cached in self.max_stats)"""
        if not GI_MAX_STATS or not self.c or not self.c.s.get("inGame"):
            return
        now = time.time()
        if now - self.stats_at < GI_STATS_EVERY:
            return
        self.stats_at = now
        st = self.ev(STATS_JS) or {}
        vals = [st.get(k) for k in ("str", "agi", "vit", "ene")]
        if any(v is None for v in vals):
            if self.stats_logged != "missing":
                self.stats_logged = "missing"
                self.log(f"can't read all 4 stats ({st}) -> max-stats check off for me")
            self.max_stats = False
            return
        ok = all(v >= GI_MIN_STAT for v in vals)
        if ok != self.max_stats:
            self.log(f"stats STR {vals[0]} AGI {vals[1]} VIT {vals[2]} ENE {vals[3]} -> "
                     + ("MAX: I hunt the Golden Invasion" if ok else f"not max ({GI_MIN_STAT}): no Golden Invasion"))
        self.max_stats = ok

    def switched_on(self):
        """hub.gi_on: G in the console / the hub page (shared by every machine through mu_hub)."""
        return GI_ENABLED and bool(getattr(self.a.hub, "gi_on", True))

    def is_hunter(self):
        name = self.a.name.lower()
        return self.switched_on() and (name in GI_CHARS or bool(self.max_stats))

    def eligible(self):
        a, s = self.a, self.c.s
        if not self.is_hunter():
            return False
        if a.bcs is not None and a.bcs.get("phase") in ("inside", "end"):
            return False                          # inside a castle (BC prep can wait: invasion first)
        if a.my_trade or (a.hub.trade and a.name in (a.hub.trade.get("alt"), a.hub.trade.get("lead"))):
            return False
        lvl = s.get("level") or 0
        if lvl < NORIA_MIN_LEVEL:
            if self.hour_done != round((self.start_at or 0) / 600):
                self.log(f"level {lvl} is below {NORIA_MIN_LEVEL} (Noria) -> skipping this invasion")
                self.hour_done = round((self.start_at or 0) / 600)
            return False
        return True

    # ── main ──
    def tick(self):
        """True while the Golden Invasion owns the character (the caller skips everything else)."""
        if not GI_ENABLED:
            return False
        if not self.switched_on() and self.phase not in ("idle", "after"):
            if self.phase == "hunt":
                self.end_hunt("Golden Invasion switched off")     # -> "after" (/pkclear etc.), then idle
                return True
            self.ev(GI_SET_JS, False)
            return self.finish("Golden Invasion switched off")
        if self.phase == "idle":
            self.check_stats()
            if not self.is_hunter():
                return False
        now = time.time()
        if self.phase != "idle" and now - self.cur_start > GI_HUNT_SEC + 300 + (GI_DRAGON_LOCK_MAX if self.locked() else 0):
            return self.finish("took too long after the hunt")      # safety net
        if self.phase != "idle":
            try:
                return self.run(now - self.cur_start)
            except Exception as e:
                self.log(f"error: {e!r}")
                return True
        self.read_timer()
        if self.start_at is None:
            return False
        rel = now - self.start_at                 # <0 before the start
        key = round(self.start_at / 600)          # one key per invasion (start time jitters by seconds)
        if True:
            if self.hour_done == key or rel < -GI_PREP_SEC or rel > GI_HUNT_SEC - 20:
                return False
            if not self.eligible():
                return False
            self.begin(key)
            return True

    def begin(self, key):
        a, c = self.a, self.c
        self.hour_done = key
        self.cur_start = self.start_at
        self.phase = "prep"
        self.potions_ok = False
        self.wait_spot = None
        self.map_target = None
        hub = self.a.hub
        with hub.lock:
            sh = getattr(hub, "gi_shared", None)
            if not sh or sh.get("key") != key:
                hub.gi_shared = {"key": key, "grids": {}, "assign": {}}   # shared by all our hunters
        self.goal = None
        self.explored_map = None             # map whose whole search grid was searched recently (search_step)
        self.map_since = time.time()         # when we got our current map_target (GI_SWITCH_MIN_STAY)
        self.pre_map = self.pick_pre_map()
        with hub.lock:
            hub.gi_shared.setdefault("pre", {})[self.a.name] = self.pre_map
        self.deaths = 0
        self.was_dead = False
        self.pk_tries = 0
        self.pk_sent_at = 0
        self.pk_live_tries = 0
        self.pk_live_state = None
        self.pk_kill_at = 0
        self.pk_kills_seen = 0
        self.pk_wait_logged = 0
        self.after_step = None
        self.warp_tries = 0
        self.bag0 = self.ev(BAG_BOXES_JS) or {}
        self.zen0 = c.s.get("money") or 0
        install = self.ev(GI_INSTALL_JS, self.cfg())
        self.log(f"=== GOLDEN INVASION at {time.strftime('%H:%M:%S', time.localtime(self.start_at))} "
                 f"-> preparing, waiting in {MAP_NAMES[self.pre_map]} (fight loop {install}, PvP {GI_PVP_MODE}) ===")
        a.leave_party("Golden Invasion")
        c.set_helper(False)
        self.showed = bool(GI_SHOW_WINDOW)
        if GI_SHOW_WINDOW:
            if c.minimized and not G_HEADLESS():
                c.set_window("normal")
            a.show_window = True

    def cfg(self):
        return {"version": 18, "selfDefOnly": GI_SELF_DEF_ONLY, "selfDefMs": GI_SELF_DEF_SEC * 1000, "outlawCaution": GI_OUTLAW_CAUTION, "close": GI_CLOSE, "closeDist": GI_CLOSE_DIST, "closeEta": GI_CLOSE_ETA, "closeHp": GI_CLOSE_HP, "closeStuckMs": GI_CLOSE_STUCK_MS, "clear": GI_CLEAR, "clearEta": GI_CLEAR_ETA, "clearMinEta": GI_CLEAR_MIN_ETA, "clearMax": GI_CLEAR_MAX, "clearKillMs": GI_CLEAR_KILL_SEC * 1000, "clearLeash": GI_CLEAR_LEASH, "potHp": GI_POT_HP, "castGap": GI_CAST_GAP_MS, "dragonLock": GI_DRAGON_LOCK, "areaHitRadius": GI_AREA_HIT_RADIUS, "areaHitMs": GI_AREA_HIT_MS, "pvpMode": GI_PVP_MODE, "bigBoxRadius": GI_BIG_BOX_RADIUS, "ignoreAfter": GI_IGNORE_AFTER, "ignoreMs": GI_IGNORE_SEC * 1000, "ours": list(getattr(self.a.hub, "friends", self.a.hub.names)), "skill": GI_SKILLS.get(self.a.name.lower(), 0), "potKey": GI_POTION_KEY,
                "potEvery": GI_POT_EVERY_MS, "hitWindow": GI_HIT_WINDOW_MS, "pvpRadius": GI_PVP_RADIUS,
                "view": GI_VIEW, "boxWait": GI_BOX_WAIT_SEC * 1000, "spamPickMs": GI_SPAM_PICK_MS, "closeRepathMs": GI_CLOSE_REPATH_MS, "loopMs": GI_LOOP_MS,
                "barrierTries": GI_BARRIER_TRIES,
                "buffs": [GI_BARRIER_SKILL, GI_FORTITUDE_SKILL] if GI_BARRIER else [],
                "refreshSkill": GI_FORTITUDE_SKILL if GI_BARRIER else 0, "refreshMs": GI_FORTITUDE_REFRESH * 1000,
                "mageSkill": GI_MAGE_SKILL, "combo": GI_KNIGHT_COMBO, "comboStep": GI_COMBO_STEP_MS}

    def barrier(self, on):
        """Switch the in-page Soul Barrier watcher on/off and log what it did."""
        if not GI_BARRIER:
            return
        ev = self.ev(GI_BARRIER_JS, on)
        if ev is None and on:
            self.ev(GI_INSTALL_JS, self.cfg())          # page reloaded: install the loop again
            ev = self.ev(GI_BARRIER_JS, on)
        for e in ev or []:
            self.log(e)

    def run(self, rel):
        a, c, s = self.a, self.c, self.c.s
        a.mode = "gi"
        self.barrier(True)
        if self.phase == "prep":
            if rel >= 0:
                self.phase = "hunt"
                self.start_hunt()
                return True
            if self.potions_ok:
                self.phase = "wait"               # go to the waiting map right away
            else:
                self.prep()
                return True
        if self.phase == "wait":
            if rel >= 0:
                self.phase = "hunt"
                self.start_hunt()
                return True
            self.go_wait_spot()
            return True
        if self.phase == "hunt":
            if rel >= GI_HUNT_SEC and not self.locked():
                self.end_hunt("event time is over")
                return True
            bc = self.bc_due()
            if bc is not None:
                self.left_for_bc = a.hub.open_at
                self.end_hunt(f"Blood Castle {bc} opens in {max(int(a.hub.open_at - time.time()), 0)} s")
                return True
            self.hunt(rel)
            return True
        if self.phase == "after":
            return self.after()
        return False

    def bc_due(self):
        """The castle tier when this hunter is in the Blood Castle plan and it's time to leave the hunt
        (GI_BC_LEAVE_SEC before the door opens; with the dragon lock on, once the door is open); else None."""
        a = self.a
        hub = a.hub
        plan, open_at = hub.plan, hub.open_at
        if not hub.bc_on or not plan or open_at is None:
            return None
        bcs = a.bcs
        if bcs is not None:
            if bcs.get("entered"):
                return None
            tier = bcs["tier"]
        else:
            asg = plan["assign"].get(a.name)
            if not asg or a.name in plan["out"]:
                return None
            tier = asg[0]
        until = open_at - time.time()
        if until > GI_BC_LEAVE_SEC or until < -(B.BC_DOOR_CLOSE_SEC - B.BC_ENTRY_NEEDS_SEC):
            return None
        if self.locked() and until > 0:
            if not getattr(self, "bc_lock_logged", None) == open_at:
                self.bc_lock_logged = open_at
                self.log(f"Blood Castle {tier} in {int(until)} s, but we're on a Golden Dragon -> "
                         f"staying until the door opens")
            return None
        return tier

    # ── PREP: potions in Noria ──
    def prep(self):
        a, c, s = self.a, self.c, self.c.s
        if (s.get("hp") or 0) <= 0:
            return
        if s.get("map") != MAP_NORIA:
            self.warp(NORIA_WARP)
            return
        pot = self.ev(POTION_SLOT_JS, [GI_POTION_KEY, *GI_POTION_ITEM]) or {}
        if pot.get("hk", 0) < 0 and not getattr(self, "hk_warned", False):
            self.hk_warned = True
            self.log("WARNING: no potion on the Q hotkey -> potion spam won't work. Put Large Healing Potions on Q once by hand.")
        if pot.get("total", 0) >= GI_POTION_TOTAL:
            if not self.potions_ok:
                self.log(f"potions: {pot.get('total')} in the bag")
            self.potions_ok = True
            return
        if self.potions_ok:
            return
        self.buy_potions(pot)
        self.potions_ok = True        # bought what we could; don't loop on it

    def buy_potions(self, pot):
        c = self.c
        before = pot.get("total", 0)
        talked = self.ev(B.TALK_NPC_JS, GI_POTION_NPC)
        if not talked:
            # walk toward the nearest Potion Girl first
            for (x, y) in ((169, 109), (193, 110)):
                self.ev(B.WALK_JS, [x, y])
                for _ in range(16):
                    time.sleep(0.5)
                    c.refresh()
                    if math.hypot((c.s.get("x") or 0) - x, (c.s.get("y") or 0) - y) <= 4:
                        break
                if self.ev(B.TALK_NPC_JS, GI_POTION_NPC):
                    talked = True
                    break
        if not talked:
            self.log("potions: Amy (Potion Girl) not found in Noria")
            return
        idx = None
        for _ in range(10):
            time.sleep(0.5)
            idx = self.ev(SHOP_FIND_JS, [pot.get("group", GI_POTION_ITEM[0]), pot.get("num", GI_POTION_ITEM[1]),
                                         pot.get("lvl") if pot.get("slot", -1) >= 0 else None])
            if idx is not None:
                break
        if idx is None or idx < 0:
            self.log(f"potions: shop did not open / potion {pot.get('group')}/{pot.get('num')} not sold here")
            self.ev("() => window.__store.closeNpcShop()")
            return
        count, buys, stuck = before, 0, 0
        while count < GI_POTION_TOTAL and buys < 400 and stuck < 3:
            self.ev("(i) => window.__store.buyItemFromNpc(i)", idx)
            buys += 1
            for _ in range(10):
                time.sleep(0.15)
                if not self.ev("() => !!window.__store.shopBuyPending"):
                    break
            p2 = self.ev(POTION_SLOT_JS, [GI_POTION_KEY, *GI_POTION_ITEM]) or {}
            if p2.get("total", 0) > count:
                count, stuck = p2["total"], 0
            else:
                stuck += 1
            if buys == 1:
                self.log(f"potions: one purchase gave {count - before} (slot {p2.get('slot')})")
        self.ev("() => { window.__store.closeNpcShop(); window.__store.inventoryEnabled = false; }")
        self.log(f"potions: {before} -> {count} in the bag ({buys} purchases"
                 + (", stopped: the count stopped growing (bag full? zen?)" if stuck >= 3 else "") + ")")

    def warp(self, alias):
        c = self.c
        if time.time() - self.last_warp < 8:
            return
        c.set_helper(False)
        c.close_windows()
        c.chat(f"/move {alias}")
        self.last_warp = time.time()
        c.walk_target = None

    # ── WAIT: outside the Noria safe zone ──
    def hunters(self):
        """Our hunters this invasion on every machine (mu_hub), in roster.json order (= accounts.json
        order on each machine, so both machines compute the same lists)."""
        hub = self.a.hub
        snap = hub.snapshot()
        names = [n for n in hub.names if (snap.get(n) or {}).get("gi_hunter") or n == self.a.name]
        link = getattr(hub, "link", None)
        if link is not None:
            names += [n for n, i in link.remote().items()
                      if i.get("gi_hunter") and i.get("online") and n not in names]
        return sorted(names, key=self.order_key)

    def order_key(self, n):
        """Position in roster.json (both machines agree), then this machine's accounts.json order."""
        low = [x.lower() for x in R.NAMES]
        names = self.a.hub.names
        return (low.index(n.lower()) if n.lower() in low else len(low), names.index(n) if n in names else 0, n)

    def level_of(self, n):
        if n == self.a.name:
            return self.c.s.get("level") or 0
        i = self.a.hub.snapshot().get(n)
        if i is None and getattr(self.a.hub, "link", None) is not None:
            i = self.a.hub.link.remote().get(n)
        return (i or {}).get("level") or 0

    def pick_pre_map(self):
        """Where I wait before the start: hunters spread over GI_PRE_MAPS in accounts order, each
        taking the map with the fewest of ours so far. A hunter below DEVIAS_MIN_LEVEL can't go to
        Devias: it takes the emptiest other map (it used to always fall back to Noria, landing on
        top of whoever had Noria; 2026-10-15 15:49 wizzardo lvl 10 + bruto)."""
        hs = self.hunters()
        if self.a.name not in hs:
            hs = hs + [self.a.name]
        snap = self.a.hub.snapshot()
        used = {m: 0 for m in GI_PRE_MAPS}
        for i, n in enumerate(hs):
            lvl = self.level_of(n)
            ok = [m for m in GI_PRE_MAPS if m != MAP_DEVIAS or lvl >= DEVIAS_MIN_LEVEL]
            pref = GI_PRE_MAPS[i % len(GI_PRE_MAPS)]
            m = min(ok, key=lambda mm: (used[mm], mm != pref, GI_PRE_MAPS.index(mm)))
            used[m] += 1
            if n == self.a.name:
                return m
        return MAP_NORIA

    def go_wait_spot(self):
        c, s = self.c, self.c.s
        if (s.get("hp") or 0) <= 0:
            return
        m = self.pre_map
        if s.get("map") != m:
            self.warp(GI_PRE_WARP[m])
            self.wait_spot = None
            return
        if self.wait_spot is None:
            cx, cy = GI_PRE_CENTER.get(m, (s.get("x"), s.get("y")))
            r = self.ev(WAIT_SPOT_JS, [cx, cy, GI_WAIT_DIST]) or {}
            spots = r.get("spots") or []
            if r.get("hereSafe") is False:
                self.wait_spot = (s.get("x"), s.get("y"))          # landed outside the safe zone: stay here
                self.log(f"waiting in {MAP_NAMES[m]} at {self.wait_spot} (already outside the safe zone)")
            elif spots:
                # our hunters waiting on the same map each take their own spot (GI_SPACING apart)
                hub = self.a.hub
                with hub.lock:
                    taken = (getattr(hub, "gi_shared", None) or {}).setdefault("wait", {})
                    others = [v for n, v in taken.items() if n != self.a.name and v[0] == m]
                    free = [sp for sp in spots
                            if all(math.hypot(sp["x"] - ox, sp["y"] - oy) >= GI_SPACING + 4 for _, ox, oy in others)]
                    sp = free[0] if free else spots[len(others) % len(spots)]
                    self.wait_spot = (sp["x"], sp["y"])
                    taken[self.a.name] = (m, sp["x"], sp["y"])
                self.log(f"waiting in {MAP_NAMES[m]} at {self.wait_spot} ({sp['edge']} tiles to the "
                         f"safe-zone edge; town tile flag={r.get('hereFlag')}"
                         + (f"; {len(others)} of ours already waiting here" if others else "") + ")")
            else:
                self.wait_spot = (cx + 25, cy + 25)
                self.log(f"could not read the safe zone in {MAP_NAMES[m]} (flag={r.get('hereFlag')}) -> "
                         f"waiting at {self.wait_spot}")
        x, y = self.wait_spot
        if math.hypot((s.get("x") or 0) - x, (s.get("y") or 0) - y) > 2.5:
            self.ev(B.WALK_JS, [x, y])
        elif hasattr(self.a, "idle_wiggle"):
            self.a.idle_wiggle((m, x, y))      # small random steps while waiting (mu_auto_bot IDLE_WIGGLE)

    # ── HUNT ──
    def dragon_call(self, s, status, dead, now):
        """Share the dragon I see; walk to a dragon another hunter sees on my map (<= GI_CALL_DIST tiles)."""
        if not GI_CALL:
            return
        hub = self.a.hub
        sh = getattr(hub, "gi_shared", None)
        if sh is None:
            return
        me, m, x, y = self.a.name, s.get("map"), s.get("x"), s.get("y")
        dragon = status.get("dragon")
        with hub.lock:
            calls = sh.setdefault("dragons", {})
            if dragon and not dead:
                calls[me] = {"map": m, "x": dragon[0], "y": dragon[1], "at": now}
            else:
                calls.pop(me, None)
            others = [(n, dict(v)) for n, v in calls.items() if n != me]
        best = None
        if not dead and not dragon and x is not None and status.get("mode") != "pvp":
            for n, v in others:
                if v["map"] != m or now - v["at"] > GI_CALL_FRESH:
                    continue
                d = math.hypot(v["x"] - x, v["y"] - y)
                if d <= GI_CALL_DIST and (best is None or d < best[0]):
                    best = (d, n, v)
        if best:
            d, n, v = best
            if not self.call or self.call["from"] != n or math.hypot(self.call["x"] - v["x"], self.call["y"] - v["y"]) > 8:
                self.log(f"{n} found a Golden Dragon at ({v['x']},{v['y']}), {d:.0f} tiles away -> going to help")
            self.call = {"from": n, "x": v["x"], "y": v["y"]}
            self.ev(GI_CALL_JS, {"x": v["x"], "y": v["y"], "ms": 3000})
        elif self.call:
            self.log(f"dragon call from {self.call['from']} over ("
                     + ("I see the dragon myself" if dragon else "dead" if dead else "no longer reported") + ")")
            self.call = None
            self.ev(GI_CALL_JS, None)

    def locked(self):
        """True while we're fighting over a Golden Dragon / its +5 box: nothing may end or move the hunt."""
        return bool(GI_DRAGON_LOCK and self.phase == "hunt" and getattr(self, "dlock", False))

    def update_lock(self, status, dead, now):
        if not GI_DRAGON_LOCK:
            return
        dragon = status.get("dragon")             # [x, y, distance] of the dragon in view, or None
        big5 = status.get("big5")                 # distance to a free Box +5 on the ground, or None
        mode = status.get("mode")
        if not self.dlock:
            if not dead and dragon and (mode == "dragon" or (mode in ("pvp", "box") and dragon[2] <= GI_DRAGON_LOCK_DIST)):
                self.dlock, self.dlock_seen = True, now
                self.log(f"DRAGON LOCK on: fighting over the Golden Dragon at ({dragon[0]},{dragon[1]}) "
                         f"-> staying until the +5 is ours, it's gone, or we die")
            return
        if dead:
            self.dlock = False
            self.log("DRAGON LOCK off: we died")
            return
        if dragon or big5 is not None:
            self.dlock_seen = now
            return
        if now - self.dlock_seen > GI_DRAGON_BOX_GRACE:
            self.dlock = False
            self.log(f"DRAGON LOCK off: no dragon and no free +5 box in view for {GI_DRAGON_BOX_GRACE} s")

    def start_hunt(self):
        c = self.c
        self.ev(GI_INSTALL_JS, self.cfg())
        self.ev(GI_SET_JS, True)
        c.set_helper(False)
        if GI_RECORD:
            c.rec.start()
        self.hunt_started = time.time()
        self.last_golden = time.time()        # idle clock (GI_IDLE_SEC)
        self.map_target = None
        self.dlock, self.dlock_seen = False, 0.0
        self.call = None
        self.__dict__.pop("focus_wait_since", None)
        self.log(f"=== HUNT START (map {c.s.get('map')} at ({c.s.get('x')},{c.s.get('y')}), "
                 f"hp {c.s.get('hp')}, lvl {c.s.get('level')}) ===")

    def assign_map(self, m):
        sh = getattr(self.a.hub, "gi_shared", None)
        if sh is not None:
            with self.a.hub.lock:
                sh["assign"][self.a.name] = m

    def focus_map(self, banner, counts):
        """Decided once per invasion (the first hunter to get here decides for everybody):
        the focus map = the banner's map (dragons) if it's one of ours, else the one with the most golden
        mobs; GI_FOCUS_COUNT hunters go there (those already waiting there first, then the strongest = first
        in accounts.json), the others hunt where they waited."""
        hub = self.a.hub
        sh = hub.gi_shared
        hunters = self.hunters()       # BEFORE taking hub.lock: hunters() calls snapshot() (inside the lock it froze every bot at HUNT START)
        levels = {n: self.level_of(n) for n in hunters}
        with hub.lock:
            undecided = "focus" not in sh
        if undecided and hunters and hunters[0] not in hub.names:
            # a hunter on the other machine comes first: that machine decides; wait a moment for it
            t0 = self.__dict__.setdefault("focus_wait_since", time.time())
            if time.time() - t0 < GI_FOCUS_WAIT:
                return None, f"waiting for {hunters[0]}'s machine to pick the focus map"
        with hub.lock:
            if sh.get("focus_log"):
                hub_log = sh.pop("focus_log")
            elif "focus" not in sh:
                maps = (counts or {}).get("maps") or {}
                cnt = {m: int(maps.get(str(m), maps.get(m, 0)) or 0) for m in GI_PRE_MAPS}
                by_name = {v.lower(): k for k, v in MAP_NAMES.items()}
                bm = by_name.get((banner or {}).get("name", "").lower())
                if bm in GI_PRE_MAPS:
                    focus, why = bm, f"banner says {MAP_NAMES[bm]} (Golden Dragons)"
                else:
                    focus = max(GI_PRE_MAPS, key=lambda m: cnt[m])
                    why = (f"banner {banner.get('name')} is not one of ours, " if banner else "no banner, ") \
                        + f"most golden mobs in {MAP_NAMES[focus]}"
                pre = sh.get("pre", {})
                order = [n for n in hunters if n in pre]
                ok = [n for n in order if focus != MAP_DEVIAS or levels.get(n, 0) >= DEVIAS_MIN_LEVEL]
                members = [n for n in ok if pre.get(n) == focus][:GI_FOCUS_COUNT]
                members += [n for n in ok if n not in members][:GI_FOCUS_COUNT - len(members)]
                sh["focus"], sh["members"] = focus, members
                hub_log = (f"FOCUS {MAP_NAMES[focus]} ({why}; counts "
                           + ", ".join(f"{MAP_NAMES[m]} {cnt[m]}" for m in GI_PRE_MAPS)
                           + f") -> {', '.join(members)} go there, the others stay")
            else:
                hub_log = None
            focus, members = sh["focus"], sh["members"]
        if hub_log:
            self.log(hub_log)
        if self.a.name in members:
            return focus, f"focus team ({MAP_NAMES[focus]})"
        return self.pre_map, f"staying where I waited ({MAP_NAMES[self.pre_map]})"

    def choose_map(self, counts):
        """Devias or Noria: the most golden mobs per hunter of ours already there (spreads our hunters)."""
        maps = (counts or {}).get("maps") or {}
        dev, nor = int(maps.get(str(MAP_DEVIAS), maps.get(MAP_DEVIAS, 0)) or 0), \
            int(maps.get(str(MAP_NORIA), maps.get(MAP_NORIA, 0)) or 0)
        lvl = self.c.s.get("level") or 0
        sh = getattr(self.a.hub, "gi_shared", None) or {"assign": {}}
        with self.a.hub.lock:
            others = [m for n, m in sh["assign"].items() if n != self.a.name]
        on_dev, on_nor = others.count(MAP_DEVIAS), others.count(MAP_NORIA)
        if lvl < DEVIAS_MIN_LEVEL:
            return MAP_NORIA, f"level {lvl} too low for Devias (Noria {nor}, Devias {dev})"
        if dev / (1 + on_dev) > nor / (1 + on_nor):
            return MAP_DEVIAS, f"Devias {dev} ({on_dev} of ours there) vs Noria {nor} ({on_nor} of ours)"
        return MAP_NORIA, f"Noria {nor} ({on_nor} of ours there) vs Devias {dev} ({on_dev} of ours)"

    def hunt(self, rel):
        c, s = self.c, self.c.s
        if GI_RECORD:
            try:
                c.rec.tick()              # the recorder only ticks by itself while paused
            except Exception:
                pass
        st = self.ev(GI_STATUS_JS) or {}
        if not st.get("on"):
            # page reloaded (disconnect) -> install again
            self.ev(GI_INSTALL_JS, self.cfg())
            self.ev(GI_SET_JS, True)
        for e in st.get("events") or []:
            self.log(e)
            if e.startswith("player DIED"):
                self.stats["player_kills"] = self.stats.get("player_kills", 0) + 1
        if st.get("err"):
            self.log(f"fight loop error: {st['err']}")
        status = st.get("status") or {}
        counts = st.get("counts")
        dead = status.get("dead") or (s.get("hp") or 0) <= 0
        now = time.time()
        # idle clock: reset while a golden is in view, while fighting / picking up, while dead or travelling
        if (dead or status.get("goldens") or status.get("mode") in ("golden", "dragon", "pvp", "box")
                or self.map_target is None or s.get("map") != self.map_target):
            self.last_golden = now
        self.update_lock(status, dead, now)
        # InvasionMonsters only comes when a count changes; total 0 (incl. the 'fd 00' inactive packet) = over
        if counts is not None and counts.get("total") == 0 and rel > GI_BANNER_WAIT + 10 and not self.dlock:
            return self.end_hunt("every golden mob is dead")
        left = counts.get("total") if counts is not None else None
        if (GI_MIN_LEFT and left is not None and 0 < left < GI_MIN_LEFT and rel > GI_BANNER_WAIT + 10
                and not self.dlock and status.get("mode") == "search"):
            return self.end_hunt(f"only {left} golden mob(s) left (< {GI_MIN_LEFT})")
        if now - self.last_golden > GI_IDLE_SEC and not self.dlock:
            return self.end_hunt(f"no golden mob found for {GI_IDLE_SEC} s")
        if dead and not self.was_dead:
            self.deaths += 1
            self.log(f"DIED (#{self.deaths}) at ({s.get('x')},{s.get('y')}) -> respawn and keep hunting")
        self.was_dead = dead
        self.dragon_call(s, status, dead, now)
        if dead:
            return
        self.pkclear_on_kill(status, s)
        if self.map_target is None:
            banner = st.get("banner")
            if banner and banner.get("at", 0) / 1000 < self.cur_start - 30:
                banner = None                                  # an old banner from a previous hour
            if banner or (counts and counts.get("total") and rel > GI_BANNER_WAIT) or rel > GI_BANNER_WAIT + 4:
                target, why = self.focus_map(banner, counts)
                if target is None:
                    return                                     # the other machine is deciding (GI_FOCUS_WAIT)
                self.map_target = target
                self.assign_map(self.map_target)
                self.map_since = now
                self.log(f"map: {MAP_NAMES.get(self.map_target)} ({why})")
            else:
                return
        # switch map when ours is empty and the other one still has plenty
        if counts and s.get("map") == self.map_target and status.get("mode") == "search" and not self.dlock and not self.call:
            maps = counts.get("maps") or {}
            here = int(maps.get(str(self.map_target), 0) or 0)
            lvl_ok = lambda m: m != MAP_DEVIAS or (s.get("level") or 0) >= DEVIAS_MIN_LEVEL
            rest = [m for m in GI_PRE_MAPS if m != self.map_target and lvl_ok(m)]
            other = max(rest, key=lambda m: int(maps.get(str(m), 0) or 0)) if rest else self.map_target
            there = int(maps.get(str(other), 0) or 0)
            explored = (GI_EXPLORED_SWITCH and getattr(self, "explored_map", None) == self.map_target
                        and there > 0 and other != self.map_target
                        and now - getattr(self, "map_since", 0) >= GI_SWITCH_MIN_STAY)
            if (here == 0 and there >= 5) or explored:
                why = "whole map searched" if explored and not (here == 0 and there >= 5) else "nothing left here"
                self.log(f"map: {why} ({here} counted here), {there} left in {MAP_NAMES[other]} -> switching")
                self.map_target, self.goal = other, None
                self.explored_map, self.map_since = None, now
                self.assign_map(other)
        if s.get("map") != self.map_target:
            if self.map_target == MAP_DEVIAS:
                alias = DEVIAS_WARPS[min(self.warp_tries // 2, len(DEVIAS_WARPS) - 1)]
            else:
                alias = GI_PRE_WARP.get(self.map_target, NORIA_WARP)
            if time.time() - self.last_warp >= 8:
                self.warp_tries += 1
                self.warp(alias)
            return
        if self.warp_tries:
            self.log(f"landed at ({s.get('x')},{s.get('y')}) on map {s.get('map')}")
            self.warp_tries = 0
        if self.leave_town(s, status):
            return
        self.mark_seen(s, status)
        if self.call and status.get("mode") == "search":
            self.goal = None
            self.walk_once([self.call["x"], self.call["y"]], now)
            self.last_golden = now                        # walking to a dragon is not idle time
        elif status.get("mode") == "spam_loot":
            self.goal = None
            if status.get("spamPos"):
                self.walk_once(status.get("spamPos"), now)
            self.last_golden = now
        elif status.get("mode") == "search":
            self.share_pos(s)
            if status.get("big5") is not None:
                self.search_step(s)                       # loot around: no spacing walk on top of the pickup
            elif not self.keep_apart(s, now):
                self.search_step(s)
        else:
            self.share_pos(s)
            self.goal = None
        if time.time() - self.last_status_log > 20:
            self.last_status_log = time.time()
            self.log(f"{status.get('mode')} | target {status.get('target')} | players {status.get('players')} "
                     f"(near {status.get('near')}) | goldens {status.get('goldens')} | hp {status.get('hp')}/"
                     f"{status.get('maxHp')} | pots {status.get('pots')} | casts {status.get('casts')}/"
                     f"{status.get('myCasts')} seen | skill {status.get('attackSkill')} | left {counts and counts.get('total')}"
                     + (" | closing in" if status.get("closing") else "")
                     + (f" | dragon ~{status.get('eta')} s left, {status.get('hitters')} other(s) hitting it" if status.get('eta') is not None else ""))

    def walk_once(self, xy, now):
        """Repeat a walk target sparingly to avoid client re-pathing and rubber-banding."""
        xy = [int(xy[0]), int(xy[1])]
        if getattr(self, "walk_last", None) == xy and now - getattr(self, "walk_last_at", 0) < GI_WALK_REPEAT_SEC:
            return
        self.walk_last, self.walk_last_at = xy, now
        self.ev(B.WALK_JS, xy)

    def pk_wait(self, pk, now):
        """Seconds until /pkclear can work: the server's own count if it told us one, else GI_PKCLEAR_WAIT_SEC
        after the last kill we noticed."""
        level = max(pk.get("kills") or 0, (pk.get("hs") or 0) - 3)       # kills, or the outlaw stage as a stand-in
        if level > self.pk_kills_seen:
            self.pk_kill_at, self.pk_live_tries = now, 0                  # a new kill: the 60 s start over
        self.pk_kills_seen = level
        wait = (self.pk_kill_at + GI_PKCLEAR_WAIT_SEC - now) if self.pk_kill_at else 0
        return max(wait, (pk.get("clearIn") or 0) + 1 if pk.get("clearIn") else 0, 0)

    def pkclear_on_kill(self, status, s):
        """GI_PKCLEAR_ON_KILL: we turned outlaw (a kill) -> /pkclear as soon as the server allows it (60 s after
        the last kill, server rule 14/10), so we don't stay outlaw (outlaws can drop gear when they die)."""
        if not GI_PKCLEAR_ON_KILL:
            return
        pk = {"hs": status.get("heroState"), "kills": status.get("pkKills"), "clearIn": status.get("pkClearIn")}
        if pk["hs"] is None and pk["kills"] is None:
            return
        now = time.time()
        if not status.get("outlaw"):
            if self.pk_live_tries:
                self.log(f"/pkclear done mid-hunt (zen {s.get('money') or 0:,})")
            self.pk_live_tries, self.pk_live_state, self.pk_kills_seen, self.pk_kill_at = 0, None, 0, 0
            return
        wait = self.pk_wait(pk, now)
        if wait > 0:
            if now - self.pk_wait_logged > 20:
                self.pk_wait_logged = now
                self.log(f"outlaw ({status.get('pkStage') or 'state ' + str(pk['hs'])}, {pk['kills'] or '?'} kill(s)) "
                         f"-> /pkclear in {int(wait)} s (server: 60 s after the last kill); not starting any fight meanwhile")
            return
        if self.pk_live_tries >= GI_PKCLEAR_MAX_TRIES or now - self.pk_sent_at < GI_PKCLEAR_RETRY_SEC:
            return
        self.pk_live_tries += 1
        self.pk_sent_at = now
        self.log(f"outlaw ({status.get('pkStage') or 'state ' + str(pk['hs'])}) -> /pkclear (try {self.pk_live_tries}, "
                 f"zen {s.get('money') or 0:,})" + ("; if this fails too, I retry after the hunt"
                                                    if self.pk_live_tries == GI_PKCLEAR_MAX_TRIES else ""))
        self.c.chat("/pkclear")

    def leave_town(self, s, status):
        """Respawned (or wandered) into the Devias safe zone: warp to another Devias zone instead of walking out."""
        now = time.time()
        if s.get("map") != MAP_DEVIAS or not status.get("safe") or status.get("mode") not in ("search", None):
            self.town_since = None
            return False
        self.town_since = getattr(self, "town_since", None) or now
        if now - self.town_since < GI_TOWN_MAX_SEC or now - self.last_warp < 8:
            return now - self.town_since >= GI_TOWN_MAX_SEC
        self.out_try = (getattr(self, "out_try", -1) + 1) % len(DEVIAS_OUT_WARPS)
        alias = DEVIAS_OUT_WARPS[self.out_try]
        self.log(f"in the Devias safe zone at ({s.get('x')},{s.get('y')}) -> /move {alias}")
        self.warp(alias)
        self.goal = None
        return True

    # ── searching the map (cells of GI_SEARCH_CELL tiles; shared by all our hunters on that map) ──
    @property
    def grid(self):
        sh = getattr(self.a.hub, "gi_shared", None) or {"grids": {}}
        return sh["grids"].setdefault(self.map_target if self.map_target is not None else self.c.s.get("map"), {})

    # ── spacing between our hunters (GI_SPACING) ──
    def share_pos(self, s):
        sh = getattr(self.a.hub, "gi_shared", None)
        if sh is None or s.get("x") is None:
            return
        with self.a.hub.lock:
            sh.setdefault("pos", {})[self.a.name] = {"map": s.get("map"), "x": s.get("x"), "y": s.get("y"),
                                                     "goal": self.goal, "at": time.time()}

    def mates(self, s):
        """[(name, x, y, goal)] of our other hunters on my map, positions fresher than 5 s."""
        sh = getattr(self.a.hub, "gi_shared", None)
        if sh is None:
            return []
        now = time.time()
        with self.a.hub.lock:
            pos = {n: dict(v) for n, v in (sh.get("pos") or {}).items() if n != self.a.name}
        return [(n, v["x"], v["y"], v.get("goal")) for n, v in pos.items()
                if v["map"] == s.get("map") and now - v["at"] < 5]

    def keep_apart(self, s, now):
        """A teammate closer than GI_SPACING: walk away from him. True = moving away (skip the search step)."""
        if not GI_SPACING:
            return False
        x, y = s.get("x"), s.get("y")
        if x is None:
            return False
        close = [(math.hypot(mx - x, my - y), n, mx, my) for n, mx, my, _ in self.mates(s)]
        close = [c for c in close if c[0] < GI_SPACING]
        if not close:
            return False
        if now - getattr(self, "apart_at", 0) < 1.5:
            return True                                    # already walking away
        # only one of each pair moves: the later one in roster.json (the earlier one keeps searching) --
        # the same order on both machines, so exactly one of a PC/Mac pair steps away
        me = self.order_key(self.a.name)
        movers = [c for c in close if self.order_key(c[1]) < me]
        if not movers:
            return False
        # away from the teammates that are too close (their average), GI_SPACING + 2 tiles from the nearest
        ax = sum(x - mx for _, _, mx, my in movers)
        ay = sum(y - my for _, _, mx, my in movers)
        if abs(ax) + abs(ay) < 0.5:
            ax, ay = 1.0, 0.0
        base = math.atan2(ay, ax)
        dist = GI_SPACING + 2
        pts = []
        for da in (0, 0.5, -0.5, 1.0, -1.0, 1.6, -1.6, 2.4, -2.4, math.pi):
            a = base + da
            pts.append([int(round(x + math.cos(a) * dist)), int(round(y + math.sin(a) * dist))])
        ok = self.ev(WALKABLE_JS, pts) or []
        for w in ok:
            if w and all(math.hypot(w[0] - mx, w[1] - my) >= GI_SPACING for _, _, mx, my in close):
                self.apart_at = now
                self.goal = None
                if now - getattr(self, "apart_logged", 0) > 20:
                    self.apart_logged = now
                    self.log(f"spacing: {', '.join(n for _, n, _, _ in movers)} within {GI_SPACING} tiles "
                             f"-> stepping to ({w[0]},{w[1]})")
                self.ev(B.WALK_JS, [int(w[0]), int(w[1])])
                return True
        return False

    def blocked_cells(self):
        """Search cells with no walkable tile (walls / water / outside the map), per map, shared by our hunters."""
        sh = getattr(self.a.hub, "gi_shared", None)
        if sh is None:
            sh = self.__dict__.setdefault("_own_shared", {})
        m = self.map_target if self.map_target is not None else self.c.s.get("map")
        return sh.setdefault("blocked", {}).setdefault(m, set())

    def cell(self, x, y):
        return (int(x) // GI_SEARCH_CELL, int(y) // GI_SEARCH_CELL)

    def searched(self, key, now):
        t = self.grid.get(key)
        return t is not None and now - t < GI_SEARCH_AGE

    def mark_seen(self, s, status):
        x, y = s.get("x"), s.get("y")
        if x is None:
            return
        now = time.time()
        r = GI_SIGHT
        for cx in range(int((x - r) // GI_SEARCH_CELL), int((x + r) // GI_SEARCH_CELL) + 1):
            for cy in range(int((y - r) // GI_SEARCH_CELL), int((y + r) // GI_SEARCH_CELL) + 1):
                mx, my = cx * GI_SEARCH_CELL + GI_SEARCH_CELL / 2, cy * GI_SEARCH_CELL + GI_SEARCH_CELL / 2
                if math.hypot(mx - x, my - y) <= r + GI_SEARCH_CELL / 2:
                    self.grid[(cx, cy)] = now
        if not status.get("goldens"):
            for lx, ly in status.get("loot") or []:      # leftover loot: someone already went there
                for cx in range(int((lx - GI_LOOT_RADIUS) // GI_SEARCH_CELL), int((lx + GI_LOOT_RADIUS) // GI_SEARCH_CELL) + 1):
                    for cy in range(int((ly - GI_LOOT_RADIUS) // GI_SEARCH_CELL), int((ly + GI_LOOT_RADIUS) // GI_SEARCH_CELL) + 1):
                        if not self.searched((cx, cy), now):
                            self.grid[(cx, cy)] = now

    def search_step(self, s):
        x, y = s.get("x"), s.get("y")
        if x is None:
            return
        now = time.time()
        if self.goal:
            gx, gy = self.goal
            d = math.hypot(gx - x, gy - y)
            if d <= 3 or self.searched(self.cell(gx, gy), now):
                self.goal = None
            elif d < self.goal_best - 0.5:
                self.goal_best, self.goal_progress_at = d, now
            elif now - self.goal_progress_at > GI_WALK_STUCK:
                self.grid[self.cell(gx, gy)] = now          # can't get there: forget it
                self.goal = None
        if not self.goal:
            n = 256 // GI_SEARCH_CELL
            cand = []
            for cx in range(n):
                for cy in range(n):
                    if self.searched((cx, cy), now):
                        continue
                    mx, my = cx * GI_SEARCH_CELL + GI_SEARCH_CELL // 2, cy * GI_SEARCH_CELL + GI_SEARCH_CELL // 2
                    cand.append((math.hypot(mx - x, my - y), cx, cy, mx, my))
            cand.sort()
            if GI_SPACING:
                taken = []
                for _, mx2, my2, g in self.mates(s):
                    taken.append((mx2, my2))
                    if g:
                        taken.append(tuple(g))
                spaced = [c for c in cand if all(math.hypot(c[3] - tx, c[4] - ty) >= GI_SEARCH_CELL / 2 + GI_SPACING
                                                 for tx, ty in taken)]
                if spaced:
                    cand = spaced                     # leave the areas next to a teammate to him
            blocked = self.blocked_cells()
            if not cand:
                self.explored_map = self.map_target       # hunt() warps to the map with the most goldens left
                # (2026-10-14) the whole map was searched in the last GI_SEARCH_AGE s: the hunters used to stand
                # still here until the idle end (12:55, all three at the Noria map edge). Keep patrolling instead:
                # the cells searched longest ago first, far ones before near ones.
                me = self.cell(x, y)
                for cx in range(n):
                    for cy in range(n):
                        if (cx, cy) == me or (cx, cy) in blocked:
                            continue
                        mx, my = cx * GI_SEARCH_CELL + GI_SEARCH_CELL // 2, cy * GI_SEARCH_CELL + GI_SEARCH_CELL // 2
                        dd = math.hypot(mx - x, my - y)
                        if dd <= GI_SIGHT:
                            continue
                        cand.append((self.grid.get((cx, cy), 0) - dd / 10, cx, cy, mx, my))
                cand.sort()
                if cand and now - getattr(self, "patrol_logged", 0) > 30:
                    self.patrol_logged = now
                    self.log("search: whole map searched recently -> patrolling the oldest-searched areas")
            pts = [[mx, my] for _, _, _, mx, my in cand[:12]]
            ok = (self.ev(WALKABLE_JS, pts) or []) if pts else []
            for (dd, cx, cy, mx, my), w in zip(cand[:12], ok):
                if w:
                    self.goal = tuple(w)
                    break
                self.grid[(cx, cy)] = now                     # not walkable (wall / water)
                blocked.add((cx, cy))
            if not self.goal:
                return
            self.goal_best, self.goal_progress_at = 1e9, now
        self.ev(B.WALK_JS, list(self.goal))

    # ── END ──
    def end_hunt(self, why="done"):
        c = self.c
        self.ev(GI_SET_JS, False)
        st = self.ev(GI_STATUS_JS) or {}
        for e in st.get("events") or []:
            self.log(e)
            if e.startswith("player DIED"):
                self.stats["player_kills"] = self.stats.get("player_kills", 0) + 1
        status = st.get("status") or {}
        bag = self.ev(BAG_BOXES_JS) or {}
        got = {box_name(l): bag.get(l, 0) - self.bag0.get(l, 0) for l in set(bag) | set(self.bag0)}
        got = {k: v for k, v in sorted(got.items()) if v}
        self.log(f"=== HUNT OVER ({why}, {int(time.time() - self.hunt_started)} s): boxes {got or 'none'} | players died after I targeted them: "
                 f"{self.stats.get('player_kills', 0)} | deaths {self.deaths} | potions {status.get('pots')} | "
                 f"casts {status.get('casts')} ({status.get('myCasts')} seen by the server) | "
                 f"hits taken from players {status.get('hitsOnMe')} | failed pickups {status.get('pickFails')} ===")
        self.stats = {}
        if GI_RECORD:
            c.rec.stop()
        self.phase = "after"
        self.after_step = "pk"

    def after(self):
        a, c, s = self.a, self.c, self.c.s
        if (s.get("hp") or 0) <= 0:
            return True
        pk = self.ev(PK_STATE_JS) or {}
        hs = pk.get("hs")
        if self.after_step == "pk":
            if not pk.get("outlaw"):
                return self.finish("not outlaw" if pk.get("outlaw") is not None else "hero state unknown")
            wait = self.pk_wait(pk, time.time())
            bc_soon = self.left_for_bc and self.left_for_bc - time.time() < wait + 30
            if wait > 0 and not bc_soon:
                if time.time() - self.pk_wait_logged > 15:
                    self.pk_wait_logged = time.time()
                    self.log(f"outlaw ({pk.get('stage') or 'state ' + str(hs)}, {pk.get('kills') or '?'} kill(s)) "
                             f"-> waiting {int(wait)} s for /pkclear (server: 60 s after the last kill)")
                return True
            if self.pk_tries >= 3:
                return self.finish(f"/pkclear failed {self.pk_tries}x (still state {hs})")
            self.pk_tries += 1
            zen = s.get("money") or 0
            c.chat("/pkclear")
            time.sleep(3)
            c.refresh()
            pk2 = self.ev(PK_STATE_JS) or {}
            paid = zen - (c.s.get("money") or 0)
            if pk2.get("outlaw") is False or (pk2.get("cleanedAgo") is not None and pk2["cleanedAgo"] < 10):
                self.log(f"/pkclear done (outlaw state {hs} -> {pk2.get('hs')}, paid {paid:,} zen)")
                return self.finish("pk cleared")
            self.log(f"/pkclear did not work (state {pk2.get('hs')}, {pk2.get('kills')} kill(s), "
                     f"server says wait {pk2.get('clearIn')} s, zen {c.s.get('money'):,})")
            if pk2.get("clearIn"):
                self.pk_tries -= 1                          # only the 60 s rule: doesn't count as a try
                return True
            if GI_SELL_CHEST_FOR_PK and paid <= 0 and zen < 600_000_000:   # short of zen (a clear cost up to 500M)
                self.after_step = "sell"
            return True
        if self.after_step == "sell":
            done = self.sell_zen_chest()
            if done is None:
                return True
            self.after_step = "pk"
            if not done:
                return self.finish("no Zen Chest sold -> stays outlaw")
            return True
        return self.finish("done")

    def sell_zen_chest(self):
        """None = still walking; True = sold one; False = couldn't."""
        a, c, s = self.a, self.c, self.c.s
        if not a.go(B.BAR_SPOT[0], "Lorencia", B.BAR_SPOT[1], B.BAR_SPOT[2]):
            return None
        slot = self.ev(ZEN_CHEST_SLOT_JS)
        if slot is None or slot < 0:
            self.log("no Zen Chest in the bag to sell")
            return False
        zen = s.get("money") or 0
        if not self.ev(B.TALK_NPC_JS, 255):         # Liaman Barmaid
            self.log("Liaman Barmaid not found")
            return False
        for _ in range(10):
            time.sleep(0.5)
            if self.ev("() => !!(window.__store.npcShop && window.__store.npcShop.items.some(Boolean))"):
                break
        self.ev("(i) => { window.__store.pickInventoryItem(i); }", slot)
        time.sleep(0.5)
        self.ev("() => window.__store.sellPickedItemToNpc()")
        ok = False
        for _ in range(10):
            time.sleep(0.5)
            c.refresh()
            if (c.s.get("money") or 0) > zen:
                ok = True
                break
        self.ev("() => { const s = window.__store; if (s.pickedItem) s.cancelPickedItem(); s.closeNpcShop(); s.inventoryEnabled = false; }")
        self.log(f"Zen Chest {'sold' if ok else 'NOT sold'} (zen {zen:,} -> {c.s.get('money'):,})")
        return ok

    def on_pause(self):
        """You took over the character: stop the in-page fight loop (the hunt resumes it if still running)."""
        self.ev(GI_SET_JS, False)

    def finish(self, why):
        a, c = self.a, self.c
        sh = getattr(a.hub, "gi_shared", None)
        if sh is not None:
            with a.hub.lock:
                sh["assign"].pop(a.name, None)
        self.log(f"=== GOLDEN INVASION finished ({why}) -> back to normal ===")
        self.barrier(False)
        self.phase = "idle"
        if getattr(self, "showed", True):
            a.show_window = False
        a.step_key = None
        c.walk_target = None
        c.spot = None
        c.relocating = True
        return False


def G_HEADLESS():
    return bool(G.HEADLESS)


# ───────────── Golden Invasion across machines (mu_hub, multi-machine phase 4) ─────────────
# hub.gi_shared (wait spots, pre-maps, map assignments, positions for GI_SPACING, dragon calls,
# searched cells, blocked cells, the FOCUS decision) is shared with the other machines through the
# hub: every heartbeat (1 s while hunting) sends this machine's entries and merges the others'.
# Times travel as ages (the machines' clocks differ).
GI_SYNC_GRID_AGE = 30     # searched cells sent each beat: the ones (re)searched in the last N s
GI_FOCUS_WAIT    = 6      # s a hunter waits for the other machine's FOCUS decision (its first hunter decides)


def _cell_key(c):
    return f"{c[0]},{c[1]}"


def _cell(k):
    a, b = k.split(",")
    return (int(a), int(b))


def gi_export(hub):
    """This machine's part of hub.gi_shared, or None when none of its characters is in the invasion."""
    snap = hub.snapshot()
    if not any((snap.get(n) or {}).get("mode") == "gi" for n in hub.names):
        return None
    sh = getattr(hub, "gi_shared", None)
    if not sh:
        return None
    now = time.time()
    local = set(hub.names)
    with hub.lock:
        out = {
            "wait": {n: list(v) for n, v in (sh.get("wait") or {}).items() if n in local},
            "assign": {n: v for n, v in (sh.get("assign") or {}).items() if n in local and v is not None},
            "pre": {n: v for n, v in (sh.get("pre") or {}).items() if n in local},
            "pos": {n: {"map": v["map"], "x": v["x"], "y": v["y"], "goal": v.get("goal"), "age": round(now - v["at"], 1)}
                    for n, v in (sh.get("pos") or {}).items() if n in local and now - v["at"] < 10},
            "dragons": {n: {"map": v["map"], "x": v["x"], "y": v["y"], "age": round(now - v["at"], 1)}
                        for n, v in (sh.get("dragons") or {}).items() if n in local and now - v["at"] < 10},
            "grids": {str(m): {_cell_key(c): round(now - t, 1) for c, t in g.items() if now - t < GI_SYNC_GRID_AGE}
                      for m, g in (sh.get("grids") or {}).items()},
            "blocked": {str(m): [_cell_key(c) for c in cells] for m, cells in (sh.get("blocked") or {}).items()},
        }
        if "focus" in sh and not sh.get("focus_from"):          # decided here (not adopted)
            out["focus"], out["members"] = sh["focus"], list(sh.get("members") or [])
    return out


def gi_import(hub, machine, data, lag):
    """Merge another machine's part into hub.gi_shared (its characters' entries replace the old ones)."""
    sh = getattr(hub, "gi_shared", None)
    rem = hub.__dict__.setdefault("gi_remote_names", {})
    if sh is None:
        return
    now = time.time()
    local = set(hub.names)
    names = set()
    with hub.lock:
        for key in ("wait", "assign", "pre"):
            for n, v in ((data or {}).get(key) or {}).items():
                if n in local:
                    continue
                sh.setdefault(key, {})[n] = tuple(v) if key == "wait" else v
                names.add(n)
        for key in ("pos", "dragons"):
            for n, v in ((data or {}).get(key) or {}).items():
                if n in local:
                    continue
                d = dict(v)
                d["at"] = now - float(d.pop("age", 0) or 0) - float(lag or 0)
                if isinstance(d.get("goal"), list):
                    d["goal"] = tuple(d["goal"])
                sh.setdefault(key, {})[n] = d
                names.add(n)
        for m, g in ((data or {}).get("grids") or {}).items():
            grid = sh.setdefault("grids", {}).setdefault(int(m), {})
            for k, a in g.items():
                t = now - float(a) - float(lag or 0)
                c = _cell(k)
                if grid.get(c, 0) < t:
                    grid[c] = t
        for m, cells in ((data or {}).get("blocked") or {}).items():
            sh.setdefault("blocked", {}).setdefault(int(m), set()).update(_cell(k) for k in cells)
        if data and data.get("focus") is not None and "focus" not in sh:
            sh["focus"], sh["members"], sh["focus_from"] = data["focus"], list(data.get("members") or []), machine
            sh["focus_log"] = f"FOCUS {MAP_NAMES.get(data['focus'], data['focus'])} decided on {machine} -> " \
                              f"{', '.join(sh['members'])} go there, the others stay"
        for gone in rem.get(machine, set()) - names:            # left the invasion over there
            for key in ("wait", "assign", "pre", "pos", "dragons"):
                (sh.get(key) or {}).pop(gone, None)
        rem[machine] = names


def gi_wire(hub):
    """mu_auto_bot: share hub.gi_shared through the hub link (no link = this machine only, as before)."""
    link = getattr(hub, "link", None)
    if link is None:
        return
    link.gi_fn = lambda: gi_export(hub)
    link.on_gi = lambda m, data, age: gi_import(hub, m, data, age)

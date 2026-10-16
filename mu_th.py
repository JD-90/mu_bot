"""
Treasure Hunt hunter — used by mu_auto_bot.py (Juan 2026-10-15). Recon: Lorencia hunt 2026-10-15 10:40
(invalido, mu_record.log) and Noria 2026-10-14 22:40.

What the event is (from the recordings):
  * every 3 h at :40 (01:40, 04:40, 07:40, 10:40, 13:40, 16:40, 19:40, 22:40), on one map that changes
    each time (Noria, Lorencia so far). Event panel row "Treasure": a countdown ("Treasure1:44:14"),
    then "TreasureActive 09:59" at :40:00, then "TreasureLorencia 04:59" from :40:04 (5 minutes now;
    the 22:40 Noria one looked like 10). Server message: "Treasure hunt in Lorencia! A treasure chest
    is hidden somewhere on the map ... whoever breaks it gets 4 great items."
  * ONE chest on the map at a time; when someone breaks it the next one spawns somewhere else
    ("X found the treasure chest! Another one is hidden somewhere in Lorencia."). 14 chests in the
    5-minute Lorencia hunt (one every ~20 s), shared by 10 players; 33 in the Noria one.
  * the chest is a monster entity: "Loot Chest", npcType 691, seen from ~21 tiles. ONE hit breaks it
    (invalido's area skill at 10:42:48: ObjectGotKilled in the same tick).
  * it drops 4 items on its tile for whoever broke it: 3x Box of Kundun +5 + 1 gear item
    (10:42:49: Red Spirit Armor +8 + Luck + Option). Picked up within 2 s.

What every available character does (not paused / offline / in a Blood Castle / trading / at the vault,
level high enough to /move there or already on that map, a few free bag cells):
  GO      helper off, /move to the treasure map (panel row says which, ~4 s after :40)
  SEARCH  the map's walkable area is cut into cells of TH_CELL tiles; the cells are split into pie
          slices around the middle of the walkable area, one slice per character of ours in the hunt
          (roster.json order, recomputed every TH_SECTOR_EVERY s from the hub: the PC and the Mac work
          out the same slices without talking). Each one patrols its own slice: cells not seen for
          TH_SEARCH_AGE s first (nearest first), then the ones seen longest ago.
  CHEST   a Loot Chest in view (any slice) -> walk into skill range and hit it once (in-page loop,
          TH_LOOP_MS): class skill like the Big Boss (Ice Storm if owned, else the Dark Knight combo,
          else the selected skill)
  LOOT    the chest died and the kill was mine (ObjectGotKilled killer == me) -> TH_LOOT_MS of direct
          pickup requests on the drops at its tile, Boxes of Kundun first
  DONE    the panel row is back to a countdown (or TH_MAX_SEC passed) -> back to normal
Every chest any of ours sees goes to th_chests.jsonl (time, map, x, y, who saw it, who broke it):
after a few hunts it shows whether the chests spawn on fixed spots (then parking beats patrolling).
"""
import json
import math
import time
from datetime import datetime

import mu_bc as B
import mu_group_bot as G

try:
    import roster as R
except Exception:            # roster.py missing: only this machine's characters
    R = None

# ───────────────────────────── CONFIG ─────────────────────────────
TH_ENABLED       = True      # code switch; whether it runs is hub.th_on (TH_AUTO at startup, T key in the console)
TH_CHEST_TYPE    = 691       # Loot Chest
TH_CELL          = 16        # search cell size (tiles)
TH_SIGHT         = 15        # tiles around me that count as "seen" (the chest shows from ~21: margin)
TH_SEARCH_AGE    = 60        # a seen cell counts as unseen again after this long (a new chest every ~20 s)
TH_WALK_STUCK    = 4         # seconds without getting closer to a search point -> pick another
TH_SECTOR_EVERY  = 10        # seconds between recomputing who is in the hunt (-> the slices)
TH_RANGE         = {"mage": 4, "knight": 1, "other": 1}       # walk to this many tiles from the chest ...
TH_CAST_RANGE    = {"mage": 6, "knight": 2.5, "other": 3}     # ... and hit it once within this many tiles
TH_VIEW          = 30        # tiles: chests further away are ignored
TH_LOOT_MS       = 6000      # after breaking it: pickup requests on its drops this long (ms)
TH_LOOT_RADIUS   = 4         # drops this close to the chest's tile count as its loot
TH_MIN_BAG_FREE  = 4         # fewer free bag cells than this -> skip the hunt (3 boxes + the gear item)
TH_POT_HP        = 0.6       # potion (hotkey Q) under this fraction of max HP (monsters on the way)
TH_POT_EVERY_MS  = 400
TH_POTION_KEY    = 0
TH_MAGE_SKILL    = 39        # Ice Storm
TH_KNIGHT_COMBO  = [[22, 20, 19, 21, 23], 43, 41]   # same as the Golden Invasion / Big Boss
TH_COMBO_STEP_MS = 700
TH_SKILLS        = {}        # fixed skill per character (lower-case name -> skill number)
TH_LOOP_MS       = 50
TH_SHOW_WINDOW   = True      # restore the window during the hunt (minimized windows cast much slower, see GI)
TH_RECORD        = True      # record with mu_recorder (first runs: data for tuning)
TH_END_GRACE     = 4         # panel back to a countdown this long -> over
TH_MAX_SEC       = 720       # safety cap from the start
TH_WARP_TRIES    = 4
TH_HOLD_RESET_SEC = 180      # no /reset from this long before :40 until the hunt is over (level 1 can't /move)
TH_QUIET_SEC     = 180       # same window: no new box trades / vault trips (mu_auto_bot.trade_quiet)
TH_CHEST_LOG     = G.BASE_DIR / "th_chests.jsonl"
# map name in the panel -> (map index, /move aliases to try, minimum level for /move)
# (levels from the in-game warp window, 2026-10-15: Lorencia 10, Noria 10, Devias 20, Dungeon 30,
#  Lost Tower 50, Atlans 70, Tarkan 140). Already on that map -> no /move, any level.
TH_MAPS = {
    "Lorencia":   (0, ["Lorencia"], 10),
    "Dungeon":    (1, ["Dungeon"], 30),
    "Devias":     (2, ["Devias", "Devias2", "Devias3"], 20),
    "Noria":      (3, ["Noria"], 10),
    "Lost Tower": (4, ["LostTower", "Lost Tower"], 50),
    "Atlans":     (7, ["Atlans", "Atlans2"], 70),
    "Tarkan":     (8, ["Tarkan", "Tarkan2"], 140),
}
# Warp points on maps that have several (Juan 2026-10-16): our hunters are spread over them in roster
# order (both machines work out the same thing): every non-town point once, then town (its landing is in
# the safe zone and chests don't spawn there, but they do just outside), then the non-town points again.
# A point the character's level can't use -> its next one. Each hunter searches the part of the map
# closest to where it landed (hunters sharing a point split that part in slices around it).
# (alias, min level from the warp window, landing (x, y) seen in the logs or None = learned on the first warp)
TH_SPREAD        = True
TH_ASSIGN_WAIT   = 4         # seconds after the hunt starts before warping (the others join within ~3 s)
TH_NEAR_POINT    = 30        # already on the map within this many tiles of my point -> no /move
TH_POINT_TRIES   = 2         # /move tries to my point before falling back to the map's town alias
TH_WARP_POINTS = {
    "Devias":     {"points": [("Devias2", 20, (24, 25)), ("Devias3", 20, (225, 228)), ("Devias4", 20, (70, 179))],
                   "town": ("Devias", 20, (215, 35))},
    "Atlans":     {"points": [("Atlans2", 80, (226, 51)), ("Atlans3", 90, None)],
                   "town": ("Atlans", 70, (22, 15))},
    "Tarkan":     {"points": [("Tarkan2", 140, None)],
                   "town": ("Tarkan", 140, (190, 64))},
    "Dungeon":    {"points": [("Dungeon2", 40, None), ("Dungeon3", 50, None)],
                   "town": ("Dungeon", 30, None)},
    "Lost Tower": {"points": [("LostTower2", 50, None), ("LostTower3", 50, None), ("LostTower4", 60, None),
                              ("LostTower5", 60, None), ("LostTower6", 70, None), ("LostTower7", 70, None)],
                   "town": ("LostTower", 50, None)},
}
TH_WARPS_FILE    = G.BASE_DIR / "th_warps.json"   # landings learned for points with None above
# ──────────────────────────────────────────────────────────────────

# Event panel row: "Treasure1:44:14" / "Treasure00:02" (countdown), "TreasureActive 09:59", "TreasureLorencia 04:59"
TH_TIMER_JS = r"""
() => {
  for (const e of document.querySelectorAll('.event-row-label')) {
    const t = ((e.parentElement || e).textContent || '').replace(/\s+/g, ' ').trim();
    if (!/^Treasure/i.test(t)) continue;
    const rest = t.replace(/^Treasure\s*/i, '');
    const secs = s => { const c = s.match(/(\d+):(\d+)(?::(\d+))?/); if (!c) return null;
      const n = c.slice(1).filter(v => v !== undefined).map(Number);
      return n.length === 3 ? n[0] * 3600 + n[1] * 60 + n[2] : n[0] * 60 + n[1]; };
    if (/^active/i.test(rest)) return {active: true, secs: secs(rest), text: t};
    const m = rest.match(/^([A-Za-z][A-Za-z ]*?)\s*\d+:\d+/);
    if (m) return {map: m[1].split(' ').map(w => w[0].toUpperCase() + w.slice(1).toLowerCase()).join(' '),
                   secs: secs(rest), text: t};
    return {secs: secs(rest), text: t};
  }
  return null;
}
"""

# walkable cells of the current map: [[cx, cy, x, y]] (x, y = a walkable tile in the cell, middle first)
TH_CELLS_JS = """(cell) => { const w = window.__world; if (!w || !w.isWalkable) return null;
  const n = Math.floor(256 / cell), out = [], offs = [];
  const h = Math.floor(cell / 2), q = Math.max(1, Math.floor(cell / 4));
  offs.push([h, h]);
  for (const a of [q, h, cell - q]) for (const b of [q, h, cell - q]) if (a !== h || b !== h) offs.push([a, b]);
  for (let cx = 0; cx < n; cx++) for (let cy = 0; cy < n; cy++) {
    for (const [dx, dy] of offs) { const x = cx * cell + dx, y = cy * cell + dy;
      if (x > 2 && y > 2 && x < 253 && y < 253 && w.isWalkable(x, y)) { out.push([cx, cy, x, y]); break; } }
  }
  return {map: w.mapIndex, cells: out}; }"""

# The chest loop, installed once per page (a reload installs it again); switched on/off with .on
TH_INSTALL_JS = r"""
(cfg) => {
  const W = window;
  if (W.__th && W.__th.version === cfg.version) { W.__th.cfg = cfg; return 'kept'; }
  if (W.__th && W.__th.timer) clearInterval(W.__th.timer);
  const T = W.__th = {version: cfg.version, cfg, on: false, events: [], sights: [], status: {}, myId: null,
    chests: new Map(), targetId: null, breakingId: null, breakingAt: 0, lootUntil: 0, lootPos: null, lootId: null,
    broken: 0, lost: 0, seen: 0, picks: 0, direct: 0, pickFails: 0, picked: 0, lastPick: 0, walkAt: 0,
    lastPot: 0, pots: 0, casts: 0, myCasts: 0, lastMySkill: null, lastMySkillAt: 0, comboIdx: 0, comboAt: 0, kind: null};
  const ev = s => { if (T.events.length < 200) T.events.push(s); };
  const dist = (a, b) => Math.hypot(a.x - b.x, a.y - b.y);
  const P = e => ({x: e.transform.pos.x, y: e.transform.pos.z});
  const R = p => `(${Math.round(p.x)},${Math.round(p.y)})`;
  const u8 = d => new Uint8Array(d.buffer || d, d.byteOffset || 0, d.byteLength);
  const owns = (s, n) => !!(n && s.skills && s.skills.some(k => k.number === n));
  const bus = W.__eventBus;
  T.onKilled = (killed, killer) => {
    const ch = T.chests.get(killed);
    if (!ch || ch.dead || !T.on) return;
    const now = Date.now();
    ch.dead = now;
    const mine = T.myId !== null && killer === T.myId;
    // killer field not matching but I hit this very chest a moment ago: loot anyway (a refused pickup costs nothing)
    const maybe = !mine && T.breakingId === killed && now - T.breakingAt < 1000;
    if (mine || maybe) {
      T.broken++; T.lootUntil = now + T.cfg.lootMs; T.lootPos = {x: ch.x, y: ch.y}; T.lootId = killed;
      ev(`chest #${killed} BROKEN by me at (${ch.x},${ch.y})${maybe ? ' (killer id #' + killer + ' unclear)' : ''} -> looting`);
      T.sights.push({id: killed, x: ch.x, y: ch.y, event: 'broken', mine: true});
    } else {
      T.lost++;
      ev(`chest #${killed} at (${ch.x},${ch.y}) broken by someone else (#${killer})`);
      T.sights.push({id: killed, x: ch.x, y: ch.y, event: 'broken', mine: false, killer});
    }
    if (T.targetId === killed) T.targetId = null;
  };
  if (bus) {
    const mine = d => { try { const u = u8(d); return T.myId !== null && (((u[5] & 0x7f) << 8) | u[6]) === T.myId ? ((u[3] << 8) | u[4]) : null; } catch (e) { return null; } };
    bus.on('SkillAnimation', d => { const k = mine(d); if (k !== null) { T.myCasts++; T.lastMySkill = k; T.lastMySkillAt = Date.now(); } });
    bus.on('AreaSkillAnimation', d => { const k = mine(d); if (k !== null) { T.myCasts++; T.lastMySkill = k; T.lastMySkillAt = Date.now(); } });
    // c1 09 17 <killed u16> <skill u16> <killer u16>  (10:42:48: 04 14 | 00 09 | 02 00 = chest #1044, skill 9, invalido)
    bus.on('ObjectGotKilled', d => { try { const u = u8(d);
      T.onKilled(((u[3] & 0x7f) << 8) | u[4], ((u[7] & 0x7f) << 8) | u[8]); } catch (e) {} });
    bus.on('ItemPickUpRequestFailed', () => { if (T.lootUntil) T.pickFails++; });
    bus.on('ItemAddedToInventory', () => { if (T.lootUntil) T.picked++; });
    bus.on('ItemsDropped', () => { if (!T.on || !T.lootUntil) return;
      setTimeout(() => { try { T.tick(); } catch (e) {} }, 0); setTimeout(() => { try { T.tick(); } catch (e) {} }, 15); });
  }
  // walkable tile r tiles from the chest, on my side of it (nearest walkable around that angle)
  T.nearTile = (w, p, me, r) => {
    const base = Math.atan2(me.y - p.y, me.x - p.x);
    for (const da of [0, 0.4, -0.4, 0.8, -0.8, 1.3, -1.3, 1.9, -1.9, 2.6, -2.6, 3.1]) {
      for (const rr of [r, r + 1, r + 2]) {
        const x = Math.round(p.x + Math.cos(base + da) * rr), y = Math.round(p.y + Math.sin(base + da) * rr);
        if (x > 2 && y > 2 && x < 253 && y < 253 && w.isWalkable(x, y)) return {x, y};
      }
    }
    return null;
  };
  T.walk = (h, t) => { const mv = h.playerMoveTo, pf = h.pathfinding;
    mv.point.x = t.x; mv.point.y = t.y; mv.handled = false; mv.sendToServer = true;
    pf.from.x = h.transform.pos.x; pf.from.y = h.transform.pos.z; pf.to.x = t.x; pf.to.y = t.y; pf.calculated = false; };
  T.pickSkill = (now, s) => {
    const c = T.cfg;
    if (c.skill && owns(s, c.skill)) return c.skill;
    if (owns(s, c.mageSkill)) return c.mageSkill;
    const combo = [];
    for (const step of c.combo || []) { const opts = Array.isArray(step) ? step : [step];
      const n = opts.find(o => owns(s, o)); if (n) combo.push(n); }
    if (!combo.length) return s.currentSkill || null;
    if (T.comboIdx >= combo.length) T.comboIdx = 0;
    const cur = combo[T.comboIdx];
    if ((T.lastMySkill === cur && T.lastMySkillAt > T.comboAt) || now - T.comboAt > c.comboStep) {
      if (T.comboAt) T.comboIdx = (T.comboIdx + 1) % combo.length;
      T.comboAt = now;
    }
    return combo[T.comboIdx];
  };
  T.tick = () => {
    const now = Date.now(), c = T.cfg, w = W.__world, s = W.__store;
    if (!T.on || !w || !s) return;
    const h = w.playerEntity, pd = s.playerData || {};
    if (!h || h.dying || (pd.currentHP || 0) <= 0) { T.status = {mode: 'dead'}; T.targetId = null; return; }
    T.myId = h.netId;
    const me = P(h), hp = pd.currentHP || 0;
    const kind = owns(s, c.mageSkill) ? 'mage' : ((c.combo || []).some(st => (Array.isArray(st) ? st : [st]).some(n => owns(s, n))) ? 'knight' : 'other');
    const castRange = c.castRange[kind] || 3, standR = c.range[kind] || 1;
    if (kind !== T.kind) { T.kind = kind; ev(`breaking chests as ${kind}: walk to ${standR}, hit within ${castRange} tiles`); }
    if (c.potHp && pd.maxHP && hp < c.potHp * pd.maxHP && now - T.lastPot >= c.potEvery) {
      T.lastPot = now; try { s.useItemHotkey(c.potKey); T.pots++; } catch (e) {}
    }
    const base = {hp, maxHp: pd.maxHP, x: Math.round(me.x), y: Math.round(me.y), map: w.mapIndex};
    // chests in view
    let best = null;
    for (const e of w.netObjsQuery.entities) {
      if (e.npcType !== c.chestType || !e.transform) continue;
      const p = P(e), dd = dist(p, me);
      if (!T.chests.has(e.netId)) {
        T.chests.set(e.netId, {x: Math.round(p.x), y: Math.round(p.y), at: now, dead: 0});
        T.seen++;
        ev(`chest in view #${e.netId} at ${R(p)}, ${Math.round(dd)} tiles from me at ${R(me)}`);
        T.sights.push({id: e.netId, x: Math.round(p.x), y: Math.round(p.y), d: Math.round(dd), event: 'seen'});
      }
      const ch = T.chests.get(e.netId);
      if (e.dying || ch.dead || dd > c.view) continue;
      if (!best || dd < best.d) best = {e, p, d: dd};
    }
    // ── loot after my break ──
    if (T.lootUntil) {
      if (now >= T.lootUntil) {
        ev(`loot over: ${T.picked} item(s) into the bag, ${T.picks} pickup requests (${T.pickFails} refused)`);
        T.lootUntil = 0; T.lootPos = null; w.pickupTarget = null;
      } else {
        w.castRequest = null; w.attackTarget = null;
        const items = [];
        for (const e of w.netObjsQuery.entities) {
          if (!e.droppedItem || !e.transform) continue;
          const it = e.droppedItem; if (it.isMoney) continue;
          const p = P(e); if (dist(p, T.lootPos) > c.lootRadius) continue;
          items.push({e, p, d: dist(p, me), prio: (it.group === 14 && it.num === 11) ? 0 : 1});
        }
        items.sort((a, b) => (a.prio - b.prio) || (a.d - b.d));
        if (items.length && now - T.lastPick >= 50) {
          T.lastPick = now;
          const top = items.slice(0, 4), pick = top[T.picks % top.length];
          T.picks++;
          try { if (typeof s.pickupItemRequest === 'function') { s.pickupItemRequest(pick.e.netId); T.direct++; } } catch (x) {}
          w.pickupTarget = items[0].e;
        } else if (!items.length) w.pickupTarget = null;
        T.status = Object.assign({mode: 'loot', items: items.length}, base);
        return;
      }
    }
    if (!best) {
      if (T.targetId !== null) { T.targetId = null; w.castRequest = null; w.attackTarget = null; }
      T.status = Object.assign({mode: 'search'}, base);
      return;
    }
    if (T.targetId !== best.e.netId) { T.targetId = best.e.netId; ev(`going for chest #${best.e.netId} at ${R(best.p)} (${best.d.toFixed(1)} tiles)`); }
    if (best.d > castRange) {
      w.castRequest = null; w.attackTarget = null;
      if (now - T.walkAt > 400) { T.walkAt = now; const t = T.nearTile(w, best.p, me, standR); if (t) try { T.walk(h, t); } catch (e) {} }
      T.status = Object.assign({mode: 'approach', chest: [Math.round(best.p.x), Math.round(best.p.y), Math.round(best.d)]}, base);
      return;
    }
    // ── break it ──
    try { const sk = T.pickSkill(now, s); if (sk && s.currentSkill !== sk) s.selectSkill(sk); } catch (e) {}
    w.attackTarget = best.e;
    if (!w.castRequest) { w.castRequest = {target: best.e, point: {x: best.p.x, y: best.p.y}, forced: false, pvp: false}; T.casts++; }
    T.breakingId = best.e.netId; T.breakingAt = now;
    T.status = Object.assign({mode: 'break', chest: [Math.round(best.p.x), Math.round(best.p.y), Math.round(best.d)]}, base);
  };
  T.timer = setInterval(() => { try { T.tick(); } catch (e) { T.err = String(e).slice(0, 200); } }, cfg.loopMs);
  return 'installed';
}
"""

TH_STATUS_JS = """() => { const T = window.__th; if (!T) return null;
  return {status: T.status, events: T.events.splice(0), sights: T.sights.splice(0), err: T.err || null, on: T.on,
          stats: {seen: T.seen, broken: T.broken, lost: T.lost, casts: T.casts, myCasts: T.myCasts,
                  picks: T.picks, pickFails: T.pickFails, picked: T.picked, pots: T.pots}}; }"""

TH_SET_JS = """(on) => { const T = window.__th; if (!T) return false; T.on = on;
  const w = window.__world;
  if (on) { T.chests = new Map(); T.targetId = null; T.breakingId = null; T.lootUntil = 0; T.lootPos = null;
            T.broken = 0; T.lost = 0; T.seen = 0; T.picks = 0; T.direct = 0; T.pickFails = 0; T.picked = 0;
            T.pots = 0; T.casts = 0; T.myCasts = 0; T.err = null; }
  else if (w) { w.castRequest = null; w.attackTarget = null; w.pickupTarget = null; }
  return true; }"""

_CELLS = {}          # (map index, cell size) -> [(cx, cy, x, y)], shared by the characters of this machine


def split_sectors(cells, names):
    """Pie slices around the middle of the walkable cells, one per name (same order -> same result on
    every machine). Returns {name: set((cx, cy))}."""
    if not cells or not names:
        return {}
    mx = sum(c[2] for c in cells) / len(cells)
    my = sum(c[3] for c in cells) / len(cells)
    start = -math.pi / 2                         # first slice starts straight "up" the map
    def key(c):
        a = (math.atan2(c[3] - my, c[2] - mx) - start) % (2 * math.pi)
        return (round(a, 6), math.hypot(c[2] - mx, c[3] - my), c[0], c[1])
    order = sorted(cells, key=key)
    n, k = len(order), len(names)
    return {name: {(c[0], c[1]) for c in order[i * n // k:(i + 1) * n // k]} for i, name in enumerate(names)}


def learned_landings():
    try:
        return {k: tuple(v) for k, v in json.loads(TH_WARPS_FILE.read_text(encoding="utf-8")).items()}
    except Exception:
        return {}


def save_landing(alias, xy):
    d = learned_landings()
    d[alias] = tuple(xy)
    try:
        TH_WARPS_FILE.write_text(json.dumps({k: list(v) for k, v in sorted(d.items())}, indent=1), encoding="utf-8")
    except OSError:
        pass


def point_anchor(alias, cfg_xy, learned=None):
    if cfg_xy:
        return tuple(cfg_xy)
    return (learned if learned is not None else learned_landings()).get(alias)


def assign_points(map_name, names, levels):
    """Roster-ordered hunters -> {name: (alias, anchor or None)}. Order of points handed out: every non-town
    point, town, then the non-town points again (round robin); a point above a hunter's level is skipped for
    that hunter (it takes the next one it can use, town last)."""
    w = TH_WARP_POINTS.get(map_name)
    if not w:
        return {}
    learned = learned_landings()
    pts = w["points"]
    seq = list(pts) + [w["town"]] + list(pts) * max(1, len(names))
    used = [False] * len(seq)
    out = {}
    for n in names:
        lvl = levels.get(n)
        pick = None
        for i, (al, need, xy) in enumerate(seq):
            if not used[i] and (lvl is None or lvl >= need):
                pick = i
                break
        if pick is None:
            al, need, xy = w["town"]
        else:
            used[pick] = True
            al, need, xy = seq[pick]
        out[n] = (al, point_anchor(al, xy, learned))
    return out


def split_by_points(cells, names, assign):
    """Each cell goes to the nearest landing point among the hunters' points; hunters sharing a point
    split its part in pie slices around it. Hunters whose point has no known landing share pie slices
    of the whole map (old behaviour). Returns {name: set((cx, cy))}."""
    anchors = {}
    for n in names:
        a = (assign.get(n) or (None, None))[1]
        if a:
            anchors.setdefault(tuple(a), []).append(n)
    lost = [n for n in names if not (assign.get(n) or (None, None))[1]]
    if not anchors:
        return split_sectors(cells, names)
    groups = {a: [] for a in anchors}
    for c in cells:
        a = min(anchors, key=lambda p: (math.hypot(c[2] - p[0], c[3] - p[1]), p))
        groups[a].append(c)
    out = {}
    for a, who in anchors.items():
        part = groups[a]
        if len(who) == 1:
            out[who[0]] = {(c[0], c[1]) for c in part}
            continue
        def key(c, a=a):
            return (round(math.atan2(c[3] - a[1], c[2] - a[0]) % (2 * math.pi), 6),
                    math.hypot(c[2] - a[0], c[3] - a[1]), c[0], c[1])
        order = sorted(part, key=key)
        n, k = len(order), len(who)
        for i, name in enumerate(who):
            out[name] = {(c[0], c[1]) for c in order[i * n // k:(i + 1) * n // k]}
    if lost:
        out.update(split_sectors(cells, lost))
    return out


class TreasureHunt:
    """One per character. tick() returns True while the Treasure Hunt owns the character."""

    def __init__(self, agent):
        self.a = agent
        self.phase = "idle"
        self.timer_at = 0
        self.timer = None
        self.handled = None
        self.last_skip_log = None

    def log(self, msg):
        self.a.log("TH: " + msg)

    @property
    def c(self):
        return self.a.c

    def ev(self, js, arg=None):
        return self.a.ev(js, arg)

    def active(self):
        return self.phase != "idle"

    def sleep(self):
        return 0.25 if self.active() else None

    def switched_on(self):
        return TH_ENABLED and bool(getattr(self.a.hub, "th_on", False))

    # ── trigger ──
    def read_timer(self, every):
        now = time.time()
        if now - self.timer_at >= every:
            self.timer_at = now
            self.timer = self.ev(TH_TIMER_JS)
        return self.timer

    def soon(self, secs):
        """True from `secs` before the hunt until it's over (or while I'm in it)."""
        if not self.switched_on() or not secs:
            return False
        if self.active():
            return True
        t = self.timer or {}
        if t.get("map") or t.get("active"):
            return True
        return t.get("secs") is not None and t["secs"] <= secs

    def hold_reset(self):
        return self.soon(TH_HOLD_RESET_SEC)

    def quiet(self):
        return self.soon(TH_QUIET_SEC)

    def skip(self, key, why):
        self.handled = key
        if self.last_skip_log != key:
            self.last_skip_log = key
            self.log(f"skipping this Treasure Hunt: {why}")

    def busy_reason(self):
        a = self.a
        if a.bcs is not None and a.bcs.get("phase") in ("inside", "end"):
            return "inside a Blood Castle"
        if a.my_trade or (a.hub.trade and a.name in (a.hub.trade.get("alt"), a.hub.trade.get("lead"))):
            return "trading boxes"
        v = getattr(a, "vault", None)
        if v and v.get("phase") not in (None, "go"):
            return "at the vault"
        j = getattr(a, "junk", None)
        if j and j.trip and j.trip.get("phase") not in ("go", "find"):
            return "at the Alquimista"
        return None

    def bag_free(self):
        r = self.ev(B.BAG_FREE_JS)
        return r.get("free") if isinstance(r, dict) else None

    def tick(self):
        if not self.switched_on():
            if self.active():
                return self.finish("Treasure Hunt switched off (T)")
            return False
        if self.active():
            try:
                return self.run()
            except Exception as e:
                self.log(f"error: {e!r}")
                return True
        t = self.read_timer(3)
        if not t or not t.get("map"):
            return False
        key = (t["map"], int(time.time() // 1800))
        if self.handled in (key, (t["map"], key[1] - 1)):
            return False
        if t["map"] not in TH_MAPS:
            self.skip(key, f"unknown map '{t['map']}' (add it to TH_MAPS in mu_th.py)")
            return False
        idx, _, min_lvl = TH_MAPS[t["map"]]
        s = self.c.s
        lvl = s.get("level") or 0
        if s.get("map") != idx and lvl < min_lvl:
            self.skip(key, f"level {lvl} can't /move to {t['map']} (needs {min_lvl})")
            return False
        why = self.busy_reason()
        if why:
            if self.last_skip_log != ("busy", key):
                self.last_skip_log = ("busy", key)
                self.log(f"Treasure Hunt in {t['map']} but I'm {why} -> going when that's over")
            return False
        free = self.bag_free()
        if free is not None and free < TH_MIN_BAG_FREE:
            self.skip(key, f"only {free} free bag cell(s) (needs {TH_MIN_BAG_FREE})")
            return False
        self.begin(key, t)
        return True

    def begin(self, key, t):
        a, c = self.a, self.c
        self.handled = key
        self.phase = "hunt"
        self.started = time.time()
        self.map_name = t["map"]
        self.map_idx, self.aliases, _ = TH_MAPS[t["map"]]
        self.warp_tries = 0
        self.last_warp = 0
        self.countdown_since = None
        self.deaths = 0
        self.was_dead = False
        self.last_status_log = 0
        self.items0 = self.bag_count()
        self.stats = {}
        self.seen_cells = {}
        self.sector = None
        self.sector_names = None
        self.sector_at = 0
        self.goal = None
        self.goal_best = 1e9
        self.goal_progress_at = 0
        self.arrived_at = None
        self.point = None            # (alias, anchor) my warp point (TH_WARP_POINTS)
        self.assign = {}
        install = self.ev(TH_INSTALL_JS, self.cfg())
        self.log(f"=== TREASURE HUNT in {self.map_name} -> going (was {a.mode}; chest loop {install}) ===")
        c.set_helper(False)
        c.close_windows()
        c.walk_target = None
        self.showed = bool(TH_SHOW_WINDOW)
        if TH_SHOW_WINDOW:
            if c.minimized and not G.HEADLESS:
                c.set_window("normal")
            a.show_window = True
        if TH_RECORD:
            try:
                c.rec.start()
            except Exception:
                pass

    def cfg(self):
        return {"version": 1, "chestType": TH_CHEST_TYPE, "loopMs": TH_LOOP_MS, "potHp": TH_POT_HP,
                "potEvery": TH_POT_EVERY_MS, "potKey": TH_POTION_KEY, "range": TH_RANGE, "castRange": TH_CAST_RANGE,
                "view": TH_VIEW, "lootMs": TH_LOOT_MS, "lootRadius": TH_LOOT_RADIUS,
                "skill": TH_SKILLS.get(self.a.name.lower(), 0), "mageSkill": TH_MAGE_SKILL,
                "combo": TH_KNIGHT_COMBO, "comboStep": TH_COMBO_STEP_MS}

    def bag_count(self):
        n = self.ev("() => (window.__store.playerData.items || []).filter((x, i) => i >= 12 && x).length")
        return n if isinstance(n, int) else 0

    # ── running ──
    def run(self):
        a, c, s = self.a, self.c, self.c.s
        a.mode = "th"
        now = time.time()
        if TH_RECORD:
            try:
                c.rec.tick()
            except Exception:
                pass
        st = self.ev(TH_STATUS_JS)
        if st is None or not st.get("on"):
            self.ev(TH_INSTALL_JS, self.cfg())       # first tick, or the page reloaded
            self.ev(TH_SET_JS, True)
            st = self.ev(TH_STATUS_JS) or {}
        for e in st.get("events") or []:
            self.log(e)
        for sg in st.get("sights") or []:
            self.write_sight(sg, s)
        if st.get("err"):
            self.log(f"chest loop error: {st['err']}")
        self.stats = st.get("stats") or self.stats
        status = st.get("status") or {}
        mode = status.get("mode")
        dead = mode == "dead" or (s.get("hp") or 0) <= 0
        if dead and not self.was_dead:
            self.deaths += 1
            self.log(f"DIED (#{self.deaths}) at ({s.get('x')},{s.get('y')}) -> respawn and carry on")
            self.goal = None
        self.was_dead = dead
        # panel: back to a countdown = over
        t = self.read_timer(1) or {}
        if t.get("map") or t.get("active"):
            self.countdown_since = None
        elif t.get("secs") is not None:
            self.countdown_since = self.countdown_since or now
        busy = mode in ("loot", "break", "approach")
        if self.countdown_since and now - self.countdown_since >= TH_END_GRACE and not busy:
            return self.finish("the panel is counting down again (hunt over)")
        if now - self.started > TH_MAX_SEC and not busy:
            return self.finish(f"safety cap ({TH_MAX_SEC} s)")
        if dead:
            return True
        if self.point is None and TH_SPREAD and self.map_name in TH_WARP_POINTS:
            if now - self.started < TH_ASSIGN_WAIT:
                return True                          # let the others join first (same assignment everywhere)
            self.pick_point()
        if s.get("map") != self.map_idx or self.wants_point_warp(s):
            self.travel(s, now)
        else:
            if self.arrived_at is None:
                self.arrived_at = now
                self.log(f"on {self.map_name} {int(now - self.started)} s after leaving, at ({s.get('x')},{s.get('y')})"
                         + (f" (point {self.point[0]})" if self.point else ""))
                self.learn_landing(s)
            if mode == "search":
                self.search_step(s, now)
            else:
                self.goal = None
        if now - self.last_status_log > 20:
            self.last_status_log = now
            sec = len(self.sector) if self.sector else 0
            self.log(f"{mode} | me ({s.get('x')},{s.get('y')}) map {s.get('map')} hp {status.get('hp')}/{status.get('maxHp')} | "
                     f"slice {sec} cells of {len(self.sector_names or [])} hunters | chests seen {self.stats.get('seen', 0)}, "
                     f"broken by me {self.stats.get('broken', 0)}, by others {self.stats.get('lost', 0)}")
        return True

    def travel(self, s, now):
        if now - self.last_warp < 8:
            return
        if self.warp_tries >= TH_WARP_TRIES:
            return self.finish(f"could not /move to {self.map_name} after {self.warp_tries} tries")
        if self.point and self.warp_tries < TH_POINT_TRIES:
            alias = self.point[0]
        else:
            k = self.warp_tries - (TH_POINT_TRIES if self.point else 0)
            alias = self.aliases[min(max(k, 0), len(self.aliases) - 1)]
            if self.point and k == 0:
                self.log(f"/move {self.point[0]} didn't work -> town alias instead")
                self.point_failed = True
        self.warp_alias = alias
        self.warp_tries += 1
        self.log(f"/move {alias} (try {self.warp_tries})")
        c = self.c
        c.set_helper(False)
        c.close_windows()
        c.chat(f"/move {alias}")
        self.last_warp = now
        c.walk_target = None
        self.goal = None

    # ── warp points (TH_WARP_POINTS) ──
    def hunter_levels(self, names):
        a, hub = self.a, self.a.hub
        snap = {n.lower(): v for n, v in hub.snapshot().items()}
        rem = {}
        link = getattr(hub, "link", None)
        if link:
            try:
                rem = {n.lower(): v for n, v in (link.remote() or {}).items()}
            except Exception:
                rem = {}
        out = {}
        for n in names:
            v = snap.get(n.lower()) or rem.get(n.lower()) or {}
            out[n] = v.get("level")
        out[a.name] = self.c.s.get("level") or out.get(a.name)
        return out

    def pick_point(self):
        names = self.hunters()
        self.assign = assign_points(self.map_name, names, self.hunter_levels(names))
        self.point = self.assign.get(self.a.name) or (None, None)
        self.point_failed = False
        self.sector = None                           # recomputed around the points
        self.log(f"warp points: " + ", ".join(f"{n} {p[0]}" for n, p in self.assign.items()) + f" -> me: {self.point[0]}")

    def wants_point_warp(self, s):
        """Already on the treasure map but far from my point -> /move to it once (before the hunt search)."""
        if not self.point or not self.point[0] or self.arrived_at is not None or getattr(self, "point_failed", False):
            return False
        if self.warp_tries:                          # I warped already: arrived is decided by the map index
            return False
        anc = self.point[1]
        if anc is None or s.get("x") is None:
            return False
        if math.hypot(s["x"] - anc[0], s["y"] - anc[1]) <= TH_NEAR_POINT:
            return False
        return True

    def learn_landing(self, s):
        al = getattr(self, "warp_alias", None)
        if not al or s.get("x") is None or not self.point or al != self.point[0] or self.point[1]:
            return
        save_landing(al, (s["x"], s["y"]))
        self.point = (al, (s["x"], s["y"]))
        self.assign[self.a.name] = self.point
        self.sector = None
        self.log(f"learned the landing of /move {al}: ({s['x']},{s['y']}) -> {TH_WARPS_FILE.name}")

    # ── who is in the hunt -> my slice of the map ──
    def hunters(self):
        """Our characters in this hunt, roster order. Local ones: mode "th" (+ me). Other machine: mode "th"
        from the hub; hub not connected (or no hub) -> assumed in, so the slices still don't overlap."""
        a = self.a
        hub = a.hub
        order = list(R.NAMES) if (R is not None and R.NAMES) else list(hub.names)
        low = {n.lower() for n in order}
        for n in hub.names:                       # local characters missing from roster.json: at the end
            if n.lower() not in low:
                order.append(n)
        snap = hub.snapshot()
        local = {n.lower(): v for n, v in snap.items()}
        link = getattr(hub, "link", None)
        remote, connected = {}, False
        if link:
            try:
                connected = bool(link.connected())
                remote = {n.lower(): v for n, v in (link.remote() or {}).items()}
            except Exception:
                connected = False
        out = []
        for n in order:
            k = n.lower()
            if k == a.name.lower():
                out.append(a.name)
            elif k in local:
                if (local[k] or {}).get("mode") == "th":
                    out.append(n)
            elif connected:
                if (remote.get(k) or {}).get("mode") == "th":
                    out.append(n)
            else:
                out.append(n)
        return out

    def update_sector(self, now):
        if self.sector is not None and now - self.sector_at < TH_SECTOR_EVERY:
            return
        self.sector_at = now
        k = (self.map_idx, TH_CELL)
        if k not in _CELLS:
            r = self.ev(TH_CELLS_JS, TH_CELL)
            if not r or r.get("map") != self.map_idx or not r.get("cells"):
                return
            _CELLS[k] = [tuple(x) for x in r["cells"]]
        cells = _CELLS[k]
        names = self.hunters()
        if names != self.sector_names or self.sector is None:
            if self.assign:
                for n in names:                      # someone who joined late: give it a point too
                    if n not in self.assign:
                        self.assign[n] = assign_points(self.map_name, names, self.hunter_levels(names)).get(n) \
                            or (None, None)
                sec = split_by_points(cells, names, self.assign).get(self.a.name) or set()
            else:
                sec = split_sectors(cells, names).get(self.a.name) or set()
            self.log(f"slice: {len(sec)} of {len(cells)} cells, hunters {', '.join(names)}")
            self.sector, self.sector_names = sec, names
            self.goal = None
        self.cell_pos = {(c[0], c[1]): (c[2], c[3]) for c in cells}

    def search_step(self, s, now):
        x, y = s.get("x"), s.get("y")
        if x is None:
            return
        self.update_sector(now)
        if not self.sector:
            return
        # mark what I can see
        r = TH_SIGHT
        for cx in range(int((x - r) // TH_CELL), int((x + r) // TH_CELL) + 1):
            for cy in range(int((y - r) // TH_CELL), int((y + r) // TH_CELL) + 1):
                mx, my = cx * TH_CELL + TH_CELL / 2, cy * TH_CELL + TH_CELL / 2
                if math.hypot(mx - x, my - y) <= r + TH_CELL / 2:
                    self.seen_cells[(cx, cy)] = now
        if self.goal:
            key, (gx, gy) = self.goal
            d = math.hypot(gx - x, gy - y)
            fresh = now - self.seen_cells.get(key, 0) < TH_SEARCH_AGE
            if d <= 3 or fresh:
                self.goal = None
            elif d < self.goal_best - 0.5:
                self.goal_best, self.goal_progress_at = d, now
            elif now - self.goal_progress_at > TH_WALK_STUCK:
                self.seen_cells[key] = now           # can't get there right now: forget it for a while
                self.goal = None
        if not self.goal:
            unseen, old = [], []
            for key in self.sector:
                p = self.cell_pos.get(key)
                if not p:
                    continue
                d = math.hypot(p[0] - x, p[1] - y)
                t = self.seen_cells.get(key, 0)
                if now - t >= TH_SEARCH_AGE:
                    unseen.append((d, key, p))
                elif d > TH_SIGHT:
                    old.append((t - d / 10, key, p))
            cand = sorted(unseen) or sorted(old)
            if not cand:
                return
            _, key, p = cand[0]
            self.goal = (key, p)
            self.goal_best, self.goal_progress_at = 1e9, now
        self.ev(B.WALK_JS, [int(self.goal[1][0]), int(self.goal[1][1])])

    # ── chest log (th_chests.jsonl) ──
    def write_sight(self, sg, s):
        rec = {"t": datetime.now().isoformat(timespec="seconds"), "map": self.map_name, "map_idx": s.get("map"),
               "by": self.a.name, **sg}
        try:
            with TH_CHEST_LOG.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec) + "\n")
        except OSError as e:
            self.log(f"chest log: {e!r}")

    # ── end ──
    def on_pause(self):
        self.ev(TH_SET_JS, False)

    def finish(self, why):
        a, c = self.a, self.c
        self.ev(TH_SET_JS, False)
        st = self.ev(TH_STATUS_JS) or {}
        for e in st.get("events") or []:
            self.log(e)
        for sg in st.get("sights") or []:
            self.write_sight(sg, c.s)
        stats = st.get("stats") or self.stats or {}
        got = self.bag_count() - self.items0
        arrive = f"{int(self.arrived_at - self.started)} s" if self.arrived_at else "never"
        self.log(f"=== TREASURE HUNT over ({why}): on the map after {arrive}, chests seen {stats.get('seen', 0)}, "
                 f"broken by me {stats.get('broken', 0)}, by others {stats.get('lost', 0)}, items +{got} in the bag, "
                 f"casts {stats.get('casts', 0)} ({stats.get('myCasts', 0)} seen), deaths {self.deaths} -> back to normal ===")
        if TH_RECORD:
            try:
                c.rec.stop()
            except Exception:
                pass
        self.phase = "idle"
        if getattr(a, "junk", None):
            a.junk.hunt_over()                 # mu_junk: crystallize the excellents picked up (Juan 2026-10-15)
        if getattr(self, "showed", False):
            a.show_window = False
        a.step_key = None
        c.walk_target = None
        c.spot = None
        c.relocating = True
        return False

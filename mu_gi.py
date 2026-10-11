"""
Golden Invasion hunter (used by mu_auto_bot.py) — every hour at :50 for GI_CHARS (wizzardo).

Timeline (start = the event start read from the event panel, normally :50:00):
  start-60s  PREP   warp to Noria, buy Large Healing Potions at Amy until the Q slot holds 255
  start-30s  WAIT   stand ~GI_WAIT_DIST tiles outside the Noria safe zone
  start      HUNT   read the per-map counts (InvasionMonsters packet): stay in Noria, or warp to Devias
                    when Devias has more. Hunt for GI_HUNT_SEC (3 min):
                      * targets are ONLY golden mobs ("Golden ...") and other players; normal mobs ignored
                      * Golden Dragon in view + players within GI_PVP_RADIUS of us or of the dragon ->
                        kill the players first (one at a time), then the dragon, then grab the +5
                      * no PvP over small golden mobs (a small golden with a player next to it is skipped)
                      * a player that hits us is fought back
                      * Ice Storm (skill GI_SKILL) on every target
                      * potion (hotkey Q) every 0.2 s while a player has hit us in the last 2 s
                      * Box of Kundun on the ground -> pick it up; a locked box (someone else's) ->
                        keep trying for GI_BOX_WAIT_SEC while fighting players who come close
                      * nothing in view -> walk to the nearest part of the map not searched yet
                        (areas with leftover loot count as already searched)
                      * never runs back to town; after a death it respawns and carries on
  end        AFTER  /pkclear if outlaw (sells ONE Zen Chest at the Lorencia bar first if zen is short),
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

# ───────────────────────────── CONFIG ─────────────────────────────
GI_ENABLED       = True
GI_CHARS         = {"wizzardo"}     # who hunts (lower case); alts have too little HP
GI_START_MINUTE  = 50               # fallback when the event panel can't be read (PC clock)
GI_PREP_SEC      = 60               # start preparing this long before the start (:49:00)
GI_WAIT_SEC      = 30               # be at the waiting spot this long before the start (:49:30)
GI_HUNT_SEC      = 180              # hunt this long (:50:00 -> :53:00)
GI_WAIT_DIST     = 20               # waiting spot: this many tiles outside the Noria safe zone
GI_SKILL         = 39               # Ice Storm
GI_POTION_KEY    = 0                # item hotkey index: 0 = Q
GI_POTION_ITEM   = (14, 3)          # Large Healing Potion (used if the Q slot can't be read)
GI_POTION_STACK  = 255              # keep ONE slot of this many potions
GI_POTION_NPC    = 253              # Amy, Potion Girl (two of them in Noria)
GI_POT_EVERY_MS  = 200              # potion every 0.2 s ...
GI_HIT_WINDOW_MS = 2000             # ... while a player has hit us in the last 2 s
GI_PVP_RADIUS    = 12               # players this close to us or to the dragon -> fight them first
GI_VIEW          = 30               # tiles: ignore anything further away
GI_BOX_WAIT_SEC  = 20               # a locked Box of Kundun: keep trying this long
GI_LOOP_MS       = 50               # in-page fight loop period (20x/s)
GI_SEARCH_CELL   = 16               # search grid cell size (tiles)
GI_SIGHT         = 18               # tiles around us that count as "searched"
GI_LOOT_RADIUS   = 10               # leftover loot marks this area as searched
GI_WALK_STUCK    = 4                # seconds without getting closer to a search point -> pick another
GI_RECORD        = True             # record the hunt with mu_recorder
GI_SELL_CHEST_FOR_PK = True         # /pkclear short of zen -> sell ONE Zen Chest at the Lorencia bar
MAP_LORENCIA, MAP_DEVIAS, MAP_NORIA = 0, 2, 3
DEVIAS_WARPS     = ["Devias3", "Devias"]   # /move aliases to try (Devias 3 landed at (225,229) for 2500 zen)
NORIA_WARP       = "Noria"
NORIA_TOWN       = (175, 100)       # roughly the middle of Noria town (search for the safe-zone edge from here)
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
  const G = W.__gi = {version: cfg.version, cfg, on: false, counts: null, countsAt: 0, myId: null,
    potUntil: 0, lastPot: 0, pots: 0, casts: 0, myCasts: 0, lastCast: 0, lastHp: null,
    hostile: new Map(), target: null, targetIsPlayer: false, lastTargetName: null,
    box: null, boxFirstFail: new Map(), boxGaveUp: new Set(), lastPick: 0, pickFails: 0,
    hitsOnMe: 0, events: [], status: {}, recentTargets: new Map(), chatSeen: new WeakSet()};
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
      if (!G.on || G.myId === null) return;
      if (caster === G.myId) { G.myCasts++; return; }
      if (target !== G.myId) return;
      const e = (W.__world.byNetId && W.__world.byNetId.get ? W.__world.byNetId.get(caster) : null)
        || W.__world.netObjsQuery.entities.find(x => x.netId === caster);
      if (e && e.npcType === undefined) { G.hostile.set(e.netId, Date.now()); G.potUntil = Date.now() + G.cfg.hitWindow; G.hitsOnMe++; }
    } catch (e) {} });
    bus.on('AreaSkillAnimation', d => { try { const u = u8(d);
      if (G.on && G.myId !== null && ((u[5] << 8) | u[6]) === G.myId) G.myCasts++; } catch (e) {} });
    bus.on('ItemPickUpRequestFailed', () => { G.pickFails++;
      if (G.box !== null && !G.boxFirstFail.has(G.box)) { G.boxFirstFail.set(G.box, Date.now()); ev('box locked (someone else\'s) -> trying for ' + G.cfg.boxWait / 1000 + ' s'); } });
  }
  const dist = (a, b) => Math.hypot(a.x - b.x, a.y - b.y);
  const P = e => ({x: e.transform.pos.x, y: e.transform.pos.z});
  G.tick = () => {
    const now = Date.now(), cfg = G.cfg, w = W.__world, s = W.__store, soc = W.__social;
    if (!G.on || !w || !s) return;
    const h = w.playerEntity, pd = s.playerData || {};
    if (!h) { G.status = {dead: true}; return; }
    G.myId = h.netId;
    const hp = pd.currentHP || 0;
    if (h.dying || hp <= 0) { G.status = {dead: true}; G.target = null; G.lastHp = hp; return; }
    const me = P(h);
    const ours = new Set(cfg.ours.map(n => n.toLowerCase()));
    const party = new Set(((soc && soc.partyMembers) || []).map(m => (m.name || '').trim().toLowerCase()));
    // "Self defense is initiated by X's attack to <me>" -> X is hostile
    for (const l of ((soc && soc.chatLines) || []).slice(-6)) {
      if (!l || typeof l !== 'object' || G.chatSeen.has(l)) continue;
      G.chatSeen.add(l);
      const t = [l.text, l.message, l.msg, l.content].filter(Boolean).join(' ');
      const m = t.match(/initiated by (\S+?)'s attack to (\S+?)!?$/);
      if (m && m[2].toLowerCase() === (pd.name || '').toLowerCase()) {
        const e = w.netObjsQuery.entities.find(x => x.objectNameInWorld && x.objectNameInWorld.trim() === m[1]);
        if (e) G.hostile.set(e.netId, now);
        G.potUntil = now + cfg.hitWindow;
        ev('attacked by ' + m[1]);
      }
    }
    const players = [], goldens = [], boxes = [], loot = [];
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
        players.push({e, p, d, name});
      } else if (e.monsterAnimation && /^Golden/i.test(name)) {
        goldens.push({e, p, d, name, dragon: e.npcType === 79});
      }
    }
    // HP going down with a player close -> potions (a monster hit alone does not start them)
    if (G.lastHp !== null && hp < G.lastHp && players.some(x => x.d <= cfg.pvpRadius)) G.potUntil = now + cfg.hitWindow;
    G.lastHp = hp;
    if (now < G.potUntil && now - G.lastPot >= cfg.potEvery) {
      G.lastPot = now;
      try { s.useItemHotkey(cfg.potKey); G.pots++; } catch (e) {}
    }
    for (const [id, t] of G.hostile) if (now - t > 8000) G.hostile.delete(id);
    const byD = (a, b) => a.d - b.d;
    players.sort(byD); goldens.sort(byD); boxes.sort((a, b) => (b.lvl - a.lvl) || (a.d - b.d));
    const dragon = goldens.find(g => g.dragon) || null;
    const hostiles = players.filter(x => G.hostile.has(x.e.netId));
    let target = null, why = '', pick = null;
    if (dragon) {
      // 1. at a dragon: players first (whoever hit me first, else the closest), then the dragon
      const threats = players.filter(x => x.d <= cfg.pvpRadius || dist(x.p, dragon.p) <= cfg.pvpRadius);
      const hot = threats.filter(x => G.hostile.has(x.e.netId));
      if (hot.length) { target = hot[0]; why = 'hit me (at a dragon)'; }
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
        const ff = G.boxFirstFail.get(b.e.netId);
        if (ff && now - ff > cfg.boxWait) { G.boxGaveUp.add(b.e.netId); ev('gave up a locked Box +' + (b.lvl - 7)); continue; }
        pick = b; break;
      }
      if (pick && G.boxFirstFail.has(pick.e.netId)) {
        const guard = players.filter(x => dist(x.p, pick.p) <= cfg.pvpRadius || x.d <= cfg.pvpRadius);
        if (guard.length) { target = guard[0]; why = 'guarding a locked box'; pick = null; }
      }
      // 4. small golden mobs with no player next to them (no PvP over small ones)
      if (!target && !pick) {
        const free = goldens.filter(g => !players.some(x => dist(x.p, g.p) <= cfg.pvpRadius));
        if (free.length) { target = free[0]; why = 'golden'; }
      }
    }
    const isPlayer = !!(target && target.e.npcType === undefined);
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
    if (target) {
      G.box = null;
      try { if (s.currentSkill !== cfg.skill && s.skills && s.skills.some(k => k.number === cfg.skill)) s.selectSkill(cfg.skill); } catch (e) {}
      if (!isPlayer) w.attackTarget = target.e;
      if (!w.castRequest && now - G.lastCast > 100) {
        w.castRequest = {target: target.e, point: {x: target.p.x, y: target.p.y}, forced: false, pvp: isPlayer};
        G.lastCast = now; G.casts++;
      }
    } else if (pick) {
      if (G.box !== pick.e.netId) { G.box = pick.e.netId; ev(`picking up Box +${pick.lvl - 7} (${Math.round(pick.d)} tiles)`); }
      if (now - G.lastPick > 250) { G.lastPick = now; w.pickupTarget = pick.e; }
    } else {
      G.box = null;
    }
    if (!target && G.target !== null) { w.attackTarget = null; w.castRequest = null; }
    G.target = target ? target.e.netId : null;
    G.status = {mode: target ? (isPlayer ? 'pvp' : (target.e.npcType === 79 ? 'dragon' : 'golden')) : (pick ? 'box' : 'search'),
      target: tName, why, players: players.length, near: players.filter(x => x.d <= cfg.pvpRadius).length,
      goldens: goldens.length, dragon: dragon ? [Math.round(dragon.p.x), Math.round(dragon.p.y), Math.round(dragon.d)] : null,
      boxes: boxes.length, loot: loot.slice(0, 20), hp, maxHp: pd.maxHP, heroState: h.heroState,
      pots: G.pots, casts: G.casts, myCasts: G.myCasts, hitsOnMe: G.hitsOnMe, pickFails: G.pickFails,
      skill: s.currentSkill, map: w.mapIndex, x: Math.round(me.x), y: Math.round(me.y)};
  };
  G.timer = setInterval(() => { try { G.tick(); } catch (e) { G.err = String(e).slice(0, 200); } }, cfg.loopMs);
  return 'installed';
}
"""

GI_STATUS_JS = """() => { const G = window.__gi; if (!G) return null;
  const ev = G.events.splice(0); return {status: G.status, events: ev, counts: G.counts, countsAt: G.countsAt,
  err: G.err || null, on: G.on}; }"""

GI_SET_JS = """(on) => { const G = window.__gi; if (!G) return false; G.on = on;
  if (!on) { const w = window.__world; if (w) { w.castRequest = null; w.attackTarget = null; w.pickupTarget = null; }
             G.target = null; G.box = null; G.potUntil = 0; }
  return true; }"""

# potion slot behind the Q hotkey: {slot, count, group, num, lvl}
POTION_SLOT_JS = """([key, g, n]) => { const s = window.__store, items = s.playerData.items || [];
  let i = -1; try { i = s.hotkeyItemSlot(key); } catch (e) {}
  let it = i >= 0 ? items[i] : null;
  if (!it) { i = items.findIndex((x, j) => j >= 12 && x && x.group === g && x.num === n); it = i >= 0 ? items[i] : null; }
  if (!it) return {slot: -1, count: 0, group: g, num: n, lvl: 0};
  return {slot: i, count: it.durability ?? it.count ?? 1, group: it.group, num: it.num, lvl: it.lvl || 0}; }"""

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
  return {spots: out.slice(0, 4), hereSafe: h ? safe(here[0], here[1]) : null, hereFlag: h ? w.getTerrainFlag(here[0], here[1]) : null};
}
"""

WALKABLE_JS = """(pts) => { const w = window.__world; if (!w) return null;
  return pts.map(([x, y]) => { for (const [dx, dy] of [[0,0],[3,0],[-3,0],[0,3],[0,-3],[5,5],[-5,-5],[5,-5],[-5,5]]) {
      const a = x + dx, b = y + dy; if (a > 2 && b > 2 && a < 253 && b < 253 && w.isWalkable(a, b)) return [a, b]; }
    return null; }); }"""

HERO_STATE_JS = "() => { const h = window.__world && window.__world.playerEntity; return h ? h.heroState : null; }"

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
        self.phase = "idle"
        self.start_at = None
        self.timer_at = 0
        self.last_text = None
        self.hour_done = None        # start_at of the invasion we already handled
        self.potions_ok = False
        self.wait_spot = None
        self.map_target = None
        self.grid = {}
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

    def eligible(self):
        a, s = self.a, self.c.s
        if not GI_ENABLED or a.name.lower() not in GI_CHARS:
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
        if not GI_ENABLED or self.a.name.lower() not in GI_CHARS:
            return False
        now = time.time()
        if self.phase != "idle" and now - self.cur_start > GI_HUNT_SEC + 300:
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
        self.grid = {}
        self.goal = None
        self.deaths = 0
        self.was_dead = False
        self.pk_tries = 0
        self.after_step = None
        self.warp_tries = 0
        self.bag0 = self.ev(BAG_BOXES_JS) or {}
        self.zen0 = c.s.get("money") or 0
        install = self.ev(GI_INSTALL_JS, self.cfg())
        self.log(f"=== GOLDEN INVASION at {time.strftime('%H:%M:%S', time.localtime(self.start_at))} "
                 f"-> preparing (fight loop {install}) ===")
        a.leave_party("Golden Invasion")
        c.set_helper(False)
        if c.minimized and not G_HEADLESS():
            c.set_window("normal")
        a.show_window = True

    def cfg(self):
        return {"version": 3, "ours": list(self.a.hub.names), "skill": GI_SKILL, "potKey": GI_POTION_KEY,
                "potEvery": GI_POT_EVERY_MS, "hitWindow": GI_HIT_WINDOW_MS, "pvpRadius": GI_PVP_RADIUS,
                "view": GI_VIEW, "boxWait": GI_BOX_WAIT_SEC * 1000, "loopMs": GI_LOOP_MS}

    def run(self, rel):
        a, c, s = self.a, self.c, self.c.s
        a.mode = "gi"
        if self.phase == "prep":
            if rel >= 0:
                self.phase = "hunt"
                self.start_hunt()
                return True
            if rel >= -GI_WAIT_SEC and self.potions_ok:
                self.phase = "wait"
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
            if rel >= GI_HUNT_SEC:
                self.end_hunt()
                return True
            self.hunt(rel)
            return True
        if self.phase == "after":
            return self.after()
        return False

    # ── PREP: potions in Noria ──
    def prep(self):
        a, c, s = self.a, self.c, self.c.s
        if (s.get("hp") or 0) <= 0:
            return
        if s.get("map") != MAP_NORIA:
            self.warp(NORIA_WARP)
            return
        pot = self.ev(POTION_SLOT_JS, [GI_POTION_KEY, *GI_POTION_ITEM]) or {}
        if pot.get("count", 0) >= GI_POTION_STACK:
            if not self.potions_ok:
                self.log(f"potions: {pot.get('count')} in the Q slot")
            self.potions_ok = True
            return
        if self.potions_ok:
            return
        self.buy_potions(pot)
        self.potions_ok = True        # bought what we could; don't loop on it

    def buy_potions(self, pot):
        c = self.c
        before = pot.get("count", 0)
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
        while count < GI_POTION_STACK and buys < 300 and stuck < 3:
            self.ev("(i) => window.__store.buyItemFromNpc(i)", idx)
            buys += 1
            for _ in range(10):
                time.sleep(0.15)
                if not self.ev("() => !!window.__store.shopBuyPending"):
                    break
            p2 = self.ev(POTION_SLOT_JS, [GI_POTION_KEY, *GI_POTION_ITEM]) or {}
            if p2.get("count", 0) > count:
                count, stuck = p2["count"], 0
            else:
                stuck += 1
            if buys == 1:
                self.log(f"potions: one purchase gave {count - before} (slot {p2.get('slot')})")
        self.ev("() => { window.__store.closeNpcShop(); window.__store.inventoryEnabled = false; }")
        self.log(f"potions: {before} -> {count} in the Q slot ({buys} purchases"
                 + (", stopped: the stack stopped growing (new slot? zen?)" if stuck >= 3 else "") + ")")

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
    def go_wait_spot(self):
        c, s = self.c, self.c.s
        if (s.get("hp") or 0) <= 0:
            return
        if s.get("map") != MAP_NORIA:
            self.warp(NORIA_WARP)
            return
        if self.wait_spot is None:
            r = self.ev(WAIT_SPOT_JS, [NORIA_TOWN[0], NORIA_TOWN[1], GI_WAIT_DIST]) or {}
            spots = r.get("spots") or []
            if spots:
                self.wait_spot = (spots[0]["x"], spots[0]["y"])
                self.log(f"waiting spot {self.wait_spot} ({spots[0]['edge']} tiles to the safe-zone edge; "
                         f"town tile safe={r.get('hereSafe')} flag={r.get('hereFlag')})")
            else:
                self.wait_spot = (NORIA_TOWN[0] + 25, NORIA_TOWN[1] + 25)
                self.log(f"could not read the safe zone (flag={r.get('hereFlag')}) -> waiting at {self.wait_spot}")
        x, y = self.wait_spot
        if math.hypot((s.get("x") or 0) - x, (s.get("y") or 0) - y) > 2:
            self.ev(B.WALK_JS, [x, y])

    # ── HUNT ──
    def start_hunt(self):
        c = self.c
        self.ev(GI_INSTALL_JS, self.cfg())
        self.ev(GI_SET_JS, True)
        c.set_helper(False)
        if GI_RECORD:
            c.rec.start()
        self.hunt_started = time.time()
        self.map_target = None
        self.log(f"=== HUNT START (map {c.s.get('map')} at ({c.s.get('x')},{c.s.get('y')}), "
                 f"hp {c.s.get('hp')}, lvl {c.s.get('level')}) ===")

    def choose_map(self, counts):
        maps = (counts or {}).get("maps") or {}
        dev, nor = int(maps.get(str(MAP_DEVIAS), maps.get(MAP_DEVIAS, 0)) or 0), \
            int(maps.get(str(MAP_NORIA), maps.get(MAP_NORIA, 0)) or 0)
        lvl = self.c.s.get("level") or 0
        if dev > nor and lvl >= DEVIAS_MIN_LEVEL:
            return MAP_DEVIAS, f"Devias {dev} > Noria {nor}"
        return MAP_NORIA, f"Noria {nor} >= Devias {dev}" if dev <= nor else f"level {lvl} too low for Devias"

    def hunt(self, rel):
        c, s = self.c, self.c.s
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
        if dead and not self.was_dead:
            self.deaths += 1
            self.log(f"DIED (#{self.deaths}) at ({s.get('x')},{s.get('y')}) -> respawn and keep hunting")
        self.was_dead = dead
        if dead:
            return
        if self.map_target is None:
            if counts or rel > 6:
                self.map_target, why = self.choose_map(counts)
                self.log(f"map: {'Devias' if self.map_target == MAP_DEVIAS else 'Noria'} ({why}; "
                         f"counts {counts and counts.get('maps')})")
            else:
                return
        # switch map when ours is empty and the other one still has plenty
        if counts and s.get("map") == self.map_target and status.get("mode") == "search":
            maps = counts.get("maps") or {}
            here = int(maps.get(str(self.map_target), 0) or 0)
            other = MAP_NORIA if self.map_target == MAP_DEVIAS else MAP_DEVIAS
            there = int(maps.get(str(other), 0) or 0)
            if here == 0 and there >= 5 and (other != MAP_DEVIAS or (s.get("level") or 0) >= DEVIAS_MIN_LEVEL):
                self.log(f"map: nothing left here, {there} left on the other map -> switching")
                self.map_target, self.grid, self.goal = other, {}, None
        if s.get("map") != self.map_target:
            if self.map_target == MAP_DEVIAS:
                alias = DEVIAS_WARPS[min(self.warp_tries // 2, len(DEVIAS_WARPS) - 1)]
            else:
                alias = NORIA_WARP
            if time.time() - self.last_warp >= 8:
                self.warp_tries += 1
                self.warp(alias)
            return
        if self.warp_tries:
            self.log(f"landed at ({s.get('x')},{s.get('y')}) on map {s.get('map')}")
            self.warp_tries = 0
        self.mark_seen(s, status)
        if status.get("mode") == "search":
            self.search_step(s)
        else:
            self.goal = None
        if time.time() - self.last_status_log > 20:
            self.last_status_log = time.time()
            self.log(f"{status.get('mode')} | target {status.get('target')} | players {status.get('players')} "
                     f"(near {status.get('near')}) | goldens {status.get('goldens')} | hp {status.get('hp')}/"
                     f"{status.get('maxHp')} | pots {status.get('pots')} | casts {status.get('casts')}/"
                     f"{status.get('myCasts')} seen | left {counts and counts.get('total')}")

    # ── searching the map (cells of GI_SEARCH_CELL tiles) ──
    def cell(self, x, y):
        return (int(x) // GI_SEARCH_CELL, int(y) // GI_SEARCH_CELL)

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
                        self.grid.setdefault((cx, cy), now)

    def search_step(self, s):
        x, y = s.get("x"), s.get("y")
        if x is None:
            return
        now = time.time()
        if self.goal:
            gx, gy = self.goal
            d = math.hypot(gx - x, gy - y)
            if d <= 3 or self.cell(gx, gy) in self.grid:
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
                    if (cx, cy) in self.grid:
                        continue
                    mx, my = cx * GI_SEARCH_CELL + GI_SEARCH_CELL // 2, cy * GI_SEARCH_CELL + GI_SEARCH_CELL // 2
                    cand.append((math.hypot(mx - x, my - y), cx, cy, mx, my))
            cand.sort()
            pts = [[mx, my] for _, _, _, mx, my in cand[:12]]
            ok = self.ev(WALKABLE_JS, pts) or []
            for (dd, cx, cy, mx, my), w in zip(cand[:12], ok):
                if w:
                    self.goal = tuple(w)
                    break
                self.grid[(cx, cy)] = now                     # not walkable (wall / water)
            if not self.goal:
                return
            self.goal_best, self.goal_progress_at = 1e9, now
        self.ev(B.WALK_JS, list(self.goal))

    # ── END ──
    def end_hunt(self):
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
        self.log(f"=== HUNT OVER: boxes {got or 'none'} | players died after I targeted them: "
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
        hs = self.ev(HERO_STATE_JS)
        if self.after_step == "pk":
            if hs is None or hs <= 3:
                return self.finish("not outlaw" if hs is not None else "hero state unknown")
            if self.pk_tries >= 3:
                return self.finish(f"/pkclear failed {self.pk_tries}x (still state {hs})")
            self.pk_tries += 1
            zen = s.get("money") or 0
            c.chat("/pkclear")
            time.sleep(3)
            c.refresh()
            hs2 = self.ev(HERO_STATE_JS)
            paid = zen - (c.s.get("money") or 0)
            if hs2 is not None and hs2 <= 3:
                self.log(f"/pkclear done (outlaw state {hs} -> {hs2}, paid {paid:,} zen)")
                return self.finish("pk cleared")
            self.log(f"/pkclear did not work (state {hs2}, zen {c.s.get('money'):,})")
            if GI_SELL_CHEST_FOR_PK and paid <= 0:
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
        self.log(f"=== GOLDEN INVASION finished ({why}) -> back to normal ===")
        self.phase = "idle"
        a.show_window = False
        a.step_key = None
        c.walk_target = None
        c.spot = None
        c.relocating = True
        return False


def G_HEADLESS():
    return bool(G.HEADLESS)

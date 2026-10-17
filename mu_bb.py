"""
Big Boss (Balrog Colosal) hunter — used by mu_auto_bot.py. Design: claude/big-boss-design.md,
recon: claude/big-boss-recon.md (Juan 2026-10-15).

Every character that is available (not paused / offline / inside a Blood Castle / trading boxes, and
whose level can /move to the boss map) drops what it is doing when the event panel's "Big Boss" row
turns into "<Map> <x>,<y>":
  GO     leave any party, helper off, /move to the boss map (or walk if already close), walk to the
         panel's coordinates (they follow the boss as it walks)
  PARTY  one party of OURS only (party damage adds up for the 5 % loot share). Whoever gets there first
         leads (no leader switching, Juan 2026-10-15): a character alone invites the others of ours in
         view who aren't in a party yet; a character alone accepts the first invite from one of ours.
         Two parties of ours at the boss: a smaller one (2) joins the bigger one. Strangers: refused / left.
  FIGHT  in-page loop (__bb, like the Golden Invasion one): every character hits the boss with its class
         skill (Ice Storm if owned, else the Dark Knight combo, else the selected skill), Soul Barrier /
         Greater Fortitude once on arrival, potions (Q) under BB_POT_HP. Each of ours keeps its own side
         of the boss (slot = its place in BB_ORDER, BB_ORDER spread around 360 degrees), so we cover all
         the drop tiles. No PvP: other players are never targeted.
  CLOSE  boss HP <= BB_CLOSE_HP (3 %) or ~BB_CLOSE_ETA s left -> walk to BB_CLOSE_DIST tile from it on
         my own side, then keep casting from there
  LOOT   boss dead -> every one of ours picks: a direct pickup request (no walking) for EVERY drop of the boss,
         best first (rings / pendants, Box of Kundun +5, the rest; my side first), each again every
         BB_PICK_REPEAT_MS, + the client's walk-in pickup + Space presses. At least BB_LOOT_SEC, then until the
         ground has been empty BB_LOOT_EMPTY s (drops that are someone else's become free later), max BB_LOOT_MAX.
         (Juan 2026-10-15 night: no delays, nobody skips picking)
         BB_LIMIT_LOOT (feature flag, Juan 2026-10-16: back on so we don't take the whole pile): only BB_LOOTERS
         of the party loot (same pick on every machine), each ignores BB_SKIP_CHANCE of the items, only
         rings / pendants / Box +5 (BB_VALUABLE_ONLY), no Space (it grabs whatever is nearest)
  LINGER after the loot, BB_LINGER_SEC (6-18 s, random per character) of small steps around the corpse,
         so our characters don't all leave at the same moment
  DONE   linger over, or the panel is back to a countdown (killed before we got there / kill not seen)
         -> leave the boss party, back to whatever it was doing
         (a Golden Invasion it interrupted can start again if it is still running)
"""
import math
import random
import re
import time

import mu_bc as B
import mu_group_bot as G

# ───────────────────────────── CONFIG ─────────────────────────────
BB_ENABLED       = True      # code switch; whether it runs is hub.bb_on (BB_AUTO at startup, O key in the console)
BB_ORDER         = ["wizzardo", "bruto", "cucaracha", "invalido", "mogulin"]   # each one's side of the boss
BB_END_GRACE     = 10        # panel back to a countdown for this long (boss dead / gone) -> done (after the loot)
BB_LOOT_SEC      = 3         # after the kill: loot at least this long ...
BB_LOOT_MAX      = 30        # ... and keep looting while anything of the boss's is on the ground, up to this long
BB_LOOT_EMPTY    = 2         # ... done once the ground around the corpse has been empty this long
BB_PICK_REPEAT_MS = 80       # ask for the same item again after this long (every item, every loop turn)
BB_PICK_PER_TURN = 2         # pickup requests per loop turn (BB_LOOP_MS) at most, best first
BB_LIMIT_LOOT    = False      # (Juan 2026-10-16, feature flag) don't take everything off the corpse; False = v4, all grab all:
BB_LOOTERS       = 3         # ... only this many of our party loot each boss (picked from the boss's id + the party,
                             #     so every machine agrees without talking); no party -> 3 in 5 chance
BB_SKIP_CHANCE   = 0.15       # ... a looter ignores each item with this chance (decided once per item)
BB_VALUABLE_ONLY = True      # ... only rings / pendants and Box of Kundun +5, nothing else
                             # (Space is not pressed while this is on: it would grab whatever is nearest)
BB_SPACE_SPAM    = False      # also press Space while looting (Juan 2026-10-15) ...
BB_SPACE_EVERY   = 0.1       # ... this often (s)
# rings and pendants (group 13): Ring of Ice 8, Poison 9, Pendant of Lightning 12, Fire 13, Ring of Fire 21,
# Earth 22, Wind 23, Magic 24, Pendant of Ice 25, Wind 26, Water 27, Ability 28 (99B item list; 13/9, 13/12 and
# 13/13 seen on the ground in the recordings)
BB_JEWELRY       = [8, 9, 12, 13, 21, 22, 23, 24, 25, 26, 27, 28]
BB_LOOT_RADIUS   = 8         # drops this close to where it died count as its loot
BB_RANGE         = {"mage": 4, "knight": 2, "other": 2}       # stand this many tiles from the boss on my side ...
BB_CAST_RANGE    = {"mage": 6, "knight": 2.5, "other": 3}     # ... and cast while within this many tiles of it
                                                              # (Ice Storm ~6; knights' skills are melee, as in mu_bc)
BB_CLOSE_HP      = 0.03      # (Juan 2026-10-15) close in when its HP bar is at or under 3 % ...
BB_CLOSE_ETA     = 3         # ... or it has about this many seconds left (from the HP bar)
BB_CLOSE_DIST    = 1         # ... to this many tiles from it, on my side
BB_CLOSE_STUCK_MS = 2000     # no progress walking for this long -> cast from where I am
BB_PARTY_RADIUS  = 15        # ours this close to me (at the boss) take part in the party
BB_INVITE_EVERY  = 4         # seconds between invites to the same character
BB_MERGE_AFTER   = 4         # (Juan 2026-10-15) two parties of ours at the boss for this long -> the smaller one
                             # disbands and joins the other (works without the hub: sizes from what's in view)
BB_POT_HP        = 0.7       # potion (hotkey Q) when HP is under this fraction ...
BB_POT_EVERY_MS  = 300       # ... at most this often
BB_POTION_KEY    = 0         # item hotkey index: 0 = Q
BB_MAGE_SKILL    = 39        # Ice Storm (Soul Masters)
BB_KNIGHT_COMBO  = [[22, 20, 19, 21, 23], 43, 41]   # same combo as the Golden Invasion
BB_COMBO_STEP_MS = 700
BB_BUFFS         = [16, 48]  # Soul Barrier (mages) / Greater Fortitude (knights): once on arrival
BB_SKILLS        = {}        # fixed skill per character (lower-case name -> skill number)
BB_SHOW_WINDOW   = True      # restore the window during the boss (minimized windows cast much slower, see GI)
BB_RECORD        = True      # record with mu_recorder while the boss runs (first runs: data for tuning)
BB_LOOP_MS       = 100
BB_BOSS_TYPE     = 694       # Balrog Colosal
BB_WALK_EVERY    = 2.0       # seconds between walk orders toward the boss while travelling
BB_WARP_IF_FARTHER = 70      # on the boss map but further than this -> /move to the town first
# map name in the panel -> (map index, /move aliases to try, minimum level to /move there)
# (levels: standard 99B move list; a failed warp is retried with the next alias, then the boss is skipped)
BB_MAPS = {
    "Lorencia": (0, ["Lorencia"], 1),
    "Devias":   (2, ["Devias", "Devias2", "Devias3"], 20),
    "Noria":    (3, ["Noria"], 10),
    "Atlans":   (7, ["Atlans", "Atlans2"], 70),
    "Tarkan":   (8, ["Tarkan", "Tarkan2"], 140),
}
# Multiple /move aliases land at different points on these maps. Pick the
# closest entry point to the boss coordinates so characters spend less time
# crossing the map. These are the known landing tiles for this server.
BB_MAP_ENTRIES = {
    "Atlans": [("Atlans", (19, 22)), ("Atlans2", (227, 51)), ("Atlas3", (65, 162))],
    "Devias": [("Devias", (212, 47)), ("Devias2", (24, 26)),
                ("Devias3", (225, 229)), ("Devias4", (165, 177))],
    "Tarkan": [("Tarkan", (200, 66)), ("Tarkan2", (192, 160))],
}
BB_WARP_TIMEOUT  = 15        # one /move that did not land on the map -> skip this boss
BB_LINGER_SEC    = (6, 18)   # (Juan 2026-10-15) after the loot: idle this long (random per character) walking
BB_LINGER_RADIUS = 3         # around the boss corpse (within this many tiles), so we don't all warp out at once ...
BB_LINGER_STEP   = (1.5, 4)  # ... a small step every this many seconds (random)
BB_HOLD_RESET_SEC = 360      # (Juan 2026-10-15) no /reset from this long before the spawn until the boss is over:
                             # a reset drops the level to 1, below every boss map's /move level (Tarkan 140)
# ──────────────────────────────────────────────────────────────────

# Event panel row: "Big Boss04:53" / "Big Boss2:57:12" (countdown), "Big BossActive", "Big BossNoria 37,159"
BB_TIMER_JS = r"""
() => {
  for (const e of document.querySelectorAll('.event-row-label')) {
    const t = ((e.parentElement || e).textContent || '').replace(/\s+/g, ' ').trim();
    if (!/^Big Boss/i.test(t)) continue;
    const rest = t.replace(/^Big Boss\s*/i, '');
    const m = rest.match(/^(Lorencia|Devias|Noria|Atlans|Tarkan)\s*(\d+)\s*,\s*(\d+)/i);
    if (m) return {map: m[1][0].toUpperCase() + m[1].slice(1).toLowerCase(), x: Number(m[2]), y: Number(m[3]), text: t};
    if (/active/i.test(rest)) return {active: true, text: t};
    const c = rest.match(/(\d+):(\d+)(?::(\d+))?/);
    if (c) { const n = c.slice(1).filter(v => v !== undefined).map(Number);
             return {secs: n.length === 3 ? n[0] * 3600 + n[1] * 60 + n[2] : n[0] * 60 + n[1], text: t}; }
    return {text: t};
  }
  return null;
}
"""

# ours in view: [{name, d}]
OURS_JS = """(names) => { const w = window.__world, h = w && w.playerEntity; if (!h) return [];
  const low = new Set(names.map(n => n.toLowerCase())), out = [];
  for (const e of w.netObjsQuery.entities) {
    if (e.localPlayer || e.npcType !== undefined || !e.transform || e.dying) continue;
    const n = (e.objectNameInWorld || '').trim();
    if (!low.has(n.toLowerCase())) continue;
    out.push({name: n, d: Math.hypot(e.transform.pos.x - h.transform.pos.x, e.transform.pos.z - h.transform.pos.z)});
  }
  return out; }"""

# The fight loop, installed once per page (a reload installs it again); switched on/off with .on
BB_INSTALL_JS = r"""
(cfg) => {
  const W = window;
  if (W.__bb && W.__bb.version === cfg.version) { W.__bb.cfg = cfg; return 'kept'; }
  if (W.__bb && W.__bb.timer) clearInterval(W.__bb.timer);
  const B = W.__bb = {version: cfg.version, cfg, on: false, events: [], status: {}, myId: null,
    hp: [], bossId: null, bossPos: null, bossSeenAt: 0, deadAt: 0, deadPos: null, lastPot: 0, pots: 0,
    casts: 0, myCasts: 0, lastMySkill: null, lastMySkillAt: 0, comboIdx: 0, comboAt: 0,
    buffQueue: null, buffAt: 0, buffTries: 0, buffsDone: [],
    closing: false, closeBest: 1e9, closeProg: 0, closeWalkAt: 0, closeFailed: false, walkAt: 0,
    picks: 0, direct: 0, directErr: null, kind: null, looter: null, skip: new Map(), skipped: 0, asked: new Map(), lastPick: 0, pickFails: 0, picked: [], seenItems: new Map(), hitSeen: 0};
  const ev = s => { if (B.events.length < 200) B.events.push(s); };
  const dist = (a, b) => Math.hypot(a.x - b.x, a.y - b.y);
  const P = e => ({x: e.transform.pos.x, y: e.transform.pos.z});
  const u8 = d => new Uint8Array(d.buffer || d, d.byteOffset || 0, d.byteLength);
  const owns = (s, n) => !!(n && s.skills && s.skills.some(k => k.number === n));
  const bus = W.__eventBus;
  if (bus) {
    // boss HP bar (c1 06 fc <netId u16> <hp 0-255>): only while it is our target
    bus.on('MonsterHealthStatus', d => { try { const u = u8(d);
      const id = ((u[3] & 0x7f) << 8) | u[4];
      if (B.bossId === null || id !== B.bossId) return;
      const now = Date.now(); B.hp.push([now, u[5]]);
      while (B.hp.length && now - B.hp[0][0] > 15000) B.hp.shift();
    } catch (e) {} });
    const mine = d => { try { const u = u8(d); return B.myId !== null && (((u[5] & 0x7f) << 8) | u[6]) === B.myId ? ((u[3] << 8) | u[4]) : null; } catch (e) { return null; } };
    bus.on('SkillAnimation', d => { const k = mine(d); if (k !== null) { B.myCasts++; B.lastMySkill = k; B.lastMySkillAt = Date.now(); } });
    bus.on('AreaSkillAnimation', d => { const k = mine(d); if (k !== null) { B.myCasts++; B.lastMySkill = k; B.lastMySkillAt = Date.now(); } });
    bus.on('ItemPickUpRequestFailed', () => { B.pickFails++; });
    // a drop packet: look right away (the entity may only exist on the next turn of the event loop)
    bus.on('ItemsDropped', () => { if (!B.on) return;
      setTimeout(() => { try { B.tick(); } catch (e) {} }, 0); setTimeout(() => { try { B.tick(); } catch (e) {} }, 15); });
  }
  B.eta = (now) => {
    const h = B.hp; if (h.length < 2) return null;
    const last = h[h.length - 1], first = h.find(x => last[0] - x[0] <= 10000) || h[0];
    const dt = (last[0] - first[0]) / 1000, dh = first[1] - last[1];
    return (dt >= 2 && dh > 0) ? Math.max(0, last[1] / (dh / dt) - (now - last[0]) / 1000) : null;
  };
  // walkable tile r tiles from p in my slot's direction (nearest walkable around that angle)
  B.sideTile = (w, p, r) => {
    const base = cfg.angle;
    for (const da of [0, 0.35, -0.35, 0.7, -0.7, 1.1, -1.1, 1.6, -1.6, 2.2, -2.2, 3.1]) {
      for (const rr of [r, r + 1, Math.max(1, r - 1)]) {
        const x = Math.round(p.x + Math.cos(base + da) * rr), y = Math.round(p.y + Math.sin(base + da) * rr);
        if (x > 2 && y > 2 && x < 253 && y < 253 && w.isWalkable(x, y)) return {x, y};
      }
    }
    return null;
  };
  B.walk = (h, t) => { const mv = h.playerMoveTo, pf = h.pathfinding;
    mv.point.x = t.x; mv.point.y = t.y; mv.handled = false; mv.sendToServer = true;
    pf.from.x = h.transform.pos.x; pf.from.y = h.transform.pos.z; pf.to.x = t.x; pf.to.y = t.y; pf.calculated = false; };
  B.pickSkill = (now, s) => {
    const c = B.cfg;
    if (c.skill && owns(s, c.skill)) return c.skill;
    if (owns(s, c.mageSkill)) return c.mageSkill;
    const combo = [];
    for (const step of c.combo || []) { const opts = Array.isArray(step) ? step : [step];
      const n = opts.find(o => owns(s, o)); if (n) combo.push(n); }
    if (!combo.length) return s.currentSkill || null;
    if (B.comboIdx >= combo.length) B.comboIdx = 0;
    const cur = combo[B.comboIdx];
    if ((B.lastMySkill === cur && B.lastMySkillAt > B.comboAt) || now - B.comboAt > c.comboStep) {
      if (B.comboAt) B.comboIdx = (B.comboIdx + 1) % combo.length;
      B.comboAt = now;
    }
    return combo[B.comboIdx];
  };
  B.tick = () => {
    const now = Date.now(), c = B.cfg, w = W.__world, s = W.__store;
    if (!B.on || !w || !s) return;
    const h = w.playerEntity, pd = s.playerData || {};
    if (!h || h.dying || (pd.currentHP || 0) <= 0) { B.status = {dead: true, bossDead: !!B.deadAt}; return; }
    B.myId = h.netId;
    const me = P(h), hp = pd.currentHP || 0;
    const kind = owns(s, c.mageSkill) ? 'mage' : ((c.combo || []).some(st => (Array.isArray(st) ? st : [st]).some(n => owns(s, n))) ? 'knight' : 'other');
    const castRange = c.castRange[kind] || 3, standR = c.range[kind] || 2;
    if (kind !== B.kind) { B.kind = kind; ev(`fighting as ${kind}: stand ${standR}, cast within ${castRange} tiles`); }
    // potions
    if (c.potHp && pd.maxHP && hp < c.potHp * pd.maxHP && now - B.lastPot >= c.potEvery) {
      B.lastPot = now; try { s.useItemHotkey(c.potKey); B.pots++; } catch (e) {}
    }
    let boss = null;
    for (const e of w.netObjsQuery.entities) {
      if (e.npcType !== c.bossType || !e.transform) continue;
      if (e.dying) {
        if (!B.deadAt && B.bossId === e.netId) { B.deadAt = now; B.deadPos = P(e); ev(`boss DIED at (${Math.round(B.deadPos.x)},${Math.round(B.deadPos.y)})`); }
        continue;
      }
      boss = e; break;
    }
    if (boss) {
      if (B.bossId !== boss.netId) { B.bossId = boss.netId; B.hp = []; ev(`boss in view (#${boss.netId}) at (${Math.round(P(boss).x)},${Math.round(P(boss).y)})`); }
      B.bossPos = P(boss); B.bossSeenAt = now;
    }
    const safe = !!(w.getTerrainFlag && (w.getTerrainFlag(Math.round(me.x), Math.round(me.y)) & 1) === 1);
    let mode = 'travel', hpNow = B.hp.length ? B.hp[B.hp.length - 1][1] : null, eta = B.eta(now);
    // ── loot after the kill ──
    if (B.deadAt) {
      mode = 'loot';
      w.castRequest = null; w.attackTarget = null;
      const items = [];
      for (const e of w.netObjsQuery.entities) {
        if (!e.droppedItem || !e.transform) continue;
        const it = e.droppedItem; if (it.isMoney) continue;
        const p = P(e);
        if (!B.seenItems.has(e.netId)) {
          if (now - B.deadAt > 20000 || dist(p, B.deadPos) > c.lootRadius) { B.seenItems.set(e.netId, false); continue; }
          B.seenItems.set(e.netId, true);
          const nm = (e.objectNameInWorld || (it.item && it.item.name) || `${it.group}/${it.num}`);
          ev(`drop: ${nm} [${it.group}/${it.num}${it.item && it.item.lvl ? ' +' + it.item.lvl : ''}] at (${Math.round(p.x)},${Math.round(p.y)})`);
        }
        if (!B.seenItems.get(e.netId)) continue;
        const lvl = it.item ? it.item.lvl : 0;
        const jewel = it.group === 13 && (c.jewelry || []).includes(it.num);
        const prio = jewel ? 0 : ((it.group === 14 && it.num === 11 && lvl >= 12) ? 1 : 2);
        const a = Math.atan2(p.y - B.deadPos.y, p.x - B.deadPos.x);
        let da = Math.abs(a - c.angle) % (2 * Math.PI); if (da > Math.PI) da = 2 * Math.PI - da;
        items.push({e, p, d: dist(p, me), prio, mySide: da <= Math.PI / Math.max(2, c.slots) ? 0 : 1});
      }
      // limited loot (BB_LIMIT_LOOT): only some of us loot, each skips some items, valuable ones only
      if (c.limit) {
        if (B.looter === null) {
          const hash = str => { let x = 2166136261; for (let i = 0; i < str.length; i++) { x ^= str.charCodeAt(i); x = Math.imul(x, 16777619) >>> 0; } return x; };
          const myName = (pd.name || '').trim().toLowerCase();
          const party = [...new Set(((W.__social && W.__social.partyMembers) || []).map(m => (m.name || '').trim().toLowerCase()).filter(Boolean))];
          if (party.length > 1) {
            const order = party.sort((a, b) => (hash(B.bossId + ':' + a) - hash(B.bossId + ':' + b)) || (a < b ? -1 : 1));
            const chosen = order.slice(0, c.looters);
            B.looter = chosen.includes(myName);
            ev(`limited loot - looters this boss: ${chosen.join(', ')} -> ${B.looter ? 'I loot' : 'I don\'t loot'}`);
          } else {
            B.looter = hash(B.bossId + ':' + myName) % 5 < 3;
            ev(`limited loot - no party -> ${B.looter ? 'I loot' : 'I don\'t loot'} (3 in 5 chance)`);
          }
        }
        for (let i = items.length - 1; i >= 0; i--) {
          const x = items[i];
          if (!B.looter || (c.valuableOnly && x.prio > 1)) { items.splice(i, 1); continue; }
          if (!B.skip.has(x.e.netId)) { const sk = Math.random() < c.skipChance; B.skip.set(x.e.netId, sk);
            if (sk) { B.skipped++; ev(`limited loot - leaving item #${x.e.netId} on purpose`); } }
          if (B.skip.get(x.e.netId)) items.splice(i, 1);
        }
      }
      items.sort((a, b) => (a.prio - b.prio) || (a.mySide - b.mySide) || (a.d - b.d));
      // v4: a direct pickup request (no walking) for EVERY drop on the ground, best first, each one again every
      // pickRepeat ms (one that is someone else's now becomes free later), up to pickPerTurn per loop turn;
      // the client's walk-in pickup on the best one as the fallback
      let sent = 0;
      for (const x of items) {
        if (sent >= c.pickPerTurn) break;
        if (now - (B.asked.get(x.e.netId) || 0) < c.pickRepeat) continue;
        B.asked.set(x.e.netId, now); sent++; B.picks++;
        try { if (typeof s.pickupItemRequest === 'function') { s.pickupItemRequest(x.e.netId); B.direct++; } } catch (er) {
          if (!B.directErr) { B.directErr = String(er).slice(0, 100); ev('direct pickup error: ' + B.directErr); } }
      }
      if (items.length) { if (w.pickupTarget !== items[0].e) w.pickupTarget = items[0].e; }
      else if (w.pickupTarget) w.pickupTarget = null;
      B.status = {mode, boss: null, deadAgo: (now - B.deadAt) / 1000, items: items.length, hp, maxHp: pd.maxHP, pots: B.pots,
        casts: B.casts, myCasts: B.myCasts, picks: B.picks, direct: B.direct, pickFails: B.pickFails, looter: B.looter, skipped: B.skipped, x: Math.round(me.x), y: Math.round(me.y), map: w.mapIndex};
      return;
    }
    if (!boss) {
      B.status = {mode: B.bossSeenAt && now - B.bossSeenAt < 5000 ? 'lost' : 'travel', boss: null, lastBoss: B.bossPos,
        hp, maxHp: pd.maxHP, pots: B.pots, casts: B.casts, myCasts: B.myCasts, x: Math.round(me.x), y: Math.round(me.y), map: w.mapIndex, safe};
      return;
    }
    const bp = B.bossPos, d = dist(bp, me);
    // ── buffs once on arrival (outside the safe zone, boss close) ──
    if (!safe && d <= castRange + 6) {
      if (B.buffQueue === null) B.buffQueue = (c.buffs || []).filter(n => owns(s, n));
      if (B.buffQueue.length) {
        const n = B.buffQueue[0];
        if (B.lastMySkill === n && B.lastMySkillAt > B.buffAt - 50 && B.buffAt) { B.buffQueue.shift(); B.buffsDone.push(n); ev('buff ' + n + ' cast'); B.buffTries = 0; B.buffAt = 0; }
        else if (now - B.buffAt >= 1200) {
          if (B.buffTries >= 3) { B.buffQueue.shift(); ev('buff ' + n + ': no cast seen after 3 tries'); B.buffTries = 0; }
          else { B.buffTries++; B.buffAt = now;
            try { if (s.currentSkill !== n) s.selectSkill(n); w.attackTarget = null;
                  w.castRequest = {target: h, point: {x: me.x, y: me.y}, forced: false, pvp: false}; } catch (e) {} }
          B.status = {mode: 'buff', hp, maxHp: pd.maxHP}; return;
        } else { B.status = {mode: 'buff', hp, maxHp: pd.maxHP}; return; }
      }
    }
    // ── close in at the end ──
    const dying = (hpNow !== null && hpNow <= c.closeHp * 255) || (eta !== null && eta <= c.closeEta);
    if (dying && !B.closeFailed && d > c.closeDist + 0.9) {
      if (!B.closing) { B.closing = true; B.closeBest = d; B.closeProg = now; B.closeWalkAt = 0;
        ev(`closing in (hp ${hpNow === null ? '?' : Math.round(hpNow / 2.55)}%${eta !== null ? ', ~' + eta.toFixed(1) + ' s left' : ''}, ${d.toFixed(1)} tiles) -> ${c.closeDist} tile on my side`); }
      if (d < B.closeBest - 0.3) { B.closeBest = d; B.closeProg = now; }
      else if (now - B.closeProg > c.closeStuckMs) { B.closeFailed = true; B.closing = false; ev(`could not get next to the boss in ${c.closeStuckMs / 1000} s -> casting from here`); }
      if (B.closing) {
        w.castRequest = null; w.attackTarget = null;
        const t = B.sideTile(w, bp, c.closeDist);
        if (!t) { B.closeFailed = true; B.closing = false; ev('no free tile next to the boss -> casting from here'); }
        else if (now - B.closeWalkAt > 400) { B.closeWalkAt = now; try { B.walk(h, t); } catch (e) {} }
        B.status = {mode: 'closing', boss: [Math.round(bp.x), Math.round(bp.y), Math.round(d)], bossHp: hpNow, eta, hp, maxHp: pd.maxHP, pots: B.pots, casts: B.casts, myCasts: B.myCasts};
        return;
      }
    } else if (B.closing && d <= c.closeDist + 0.9) { B.closing = false; ev(`next to the boss (${d.toFixed(1)} tiles) -> casting from here`); }
    // ── out of range: walk to my side of the boss ──
    if (d > castRange && !dying) {
      w.castRequest = null; w.attackTarget = null;
      if (now - B.walkAt > 500) { B.walkAt = now; const t = B.sideTile(w, bp, standR); if (t) try { B.walk(h, t); } catch (e) {} }
      B.status = {mode: 'approach', boss: [Math.round(bp.x), Math.round(bp.y), Math.round(d)], bossHp: hpNow, eta, hp, maxHp: pd.maxHP, pots: B.pots, casts: B.casts, myCasts: B.myCasts, x: Math.round(me.x), y: Math.round(me.y), map: w.mapIndex};
      return;
    }
    // ── fight ──
    try { const sk = B.pickSkill(now, s); B.skill = sk; if (sk && s.currentSkill !== sk) s.selectSkill(sk); } catch (e) {}
    w.attackTarget = boss;
    if (!w.castRequest) { w.castRequest = {target: boss, point: {x: bp.x, y: bp.y}, forced: false, pvp: false}; B.casts++; }
    B.status = {mode: 'fight', boss: [Math.round(bp.x), Math.round(bp.y), Math.round(d)], bossHp: hpNow, eta, hp, maxHp: pd.maxHP,
      pots: B.pots, casts: B.casts, myCasts: B.myCasts, skill: B.skill, x: Math.round(me.x), y: Math.round(me.y), map: w.mapIndex};
  };
  B.timer = setInterval(() => { try { B.tick(); } catch (e) { B.err = String(e).slice(0, 200); } }, cfg.loopMs);
  return 'installed';
}
"""

BB_STATUS_JS = """() => { const B = window.__bb; if (!B) return null;
  return {status: B.status, events: B.events.splice(0), err: B.err || null, on: B.on, deadAt: B.deadAt}; }"""

BB_SET_JS = """(on) => { const B = window.__bb; if (!B) return false; B.on = on;
  const w = window.__world;
  if (on) { B.deadAt = 0; B.deadPos = null; B.bossId = null; B.hp = []; B.closing = false; B.closeFailed = false;
            B.buffQueue = null; B.seenItems = new Map(); B.casts = 0; B.myCasts = 0; B.pots = 0; B.picks = 0; B.direct = 0; B.pickFails = 0;
            B.asked = new Map(); B.looter = null; B.skip = new Map(); B.skipped = 0; }
  else if (w) { w.castRequest = null; w.attackTarget = null; w.pickupTarget = null; }
  return true; }"""


def order_index(name):
    low = [n.lower() for n in BB_ORDER]
    return low.index(name.lower()) if name.lower() in low else len(low) + 1


class BigBoss:
    """One per character. tick() returns True while the Big Boss owns the character."""

    def __init__(self, agent):
        self.a = agent
        self.phase = "idle"
        self.timer_at = 0
        self.timer = None
        self.handled = None          # spawn key we already handled (or skipped)
        self.last_skip_log = None

    def log(self, msg):
        self.a.log("BB: " + msg)

    @property
    def c(self):
        return self.a.c

    def ev(self, js, arg=None):
        return self.a.ev(js, arg)

    def active(self):
        return self.phase != "idle"

    def sleep(self):
        return 0.25 if self.active() else None

    # ── trigger ──
    def read_timer(self, every):
        now = time.time()
        if now - self.timer_at >= every:
            self.timer_at = now
            self.timer = self.ev(BB_TIMER_JS)
        return self.timer

    def skip(self, key, why):
        self.handled = key
        if self.last_skip_log != key:
            self.last_skip_log = key
            self.log(f"skipping this Big Boss: {why}")

    def hold_reset(self):
        """True from BB_HOLD_RESET_SEC before the spawn until the boss is over (or while I'm at it).
        Only matters for a character whose level can /move to every boss map (>= the highest minimum)."""
        if not self.switched_on() or not BB_HOLD_RESET_SEC:
            return False
        if self.active():
            return True
        t = self.timer or {}
        if t.get("map") or t.get("active"):
            return True                        # the boss is up right now
        return t.get("secs") is not None and t["secs"] <= BB_HOLD_RESET_SEC

    def busy_reason(self):
        a = self.a
        if a.bcs is not None and a.bcs.get("phase") in ("inside", "end"):
            return "inside a Blood Castle"
        if a.my_trade or (a.hub.trade and a.name in (a.hub.trade.get("alt"), a.hub.trade.get("lead"))):
            return "trading boxes"
        return None

    def switched_on(self):
        return BB_ENABLED and bool(getattr(self.a.hub, "bb_on", False))

    def tick(self):
        if not self.switched_on():
            if self.active():
                return self.finish("Big Boss switched off (O)")
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
        if t["map"] not in BB_MAPS:
            return False
        # one key per spawn: the boss lives <= 30 min and spawns every 3 h -> map + 30-min slot is unique enough
        key = (t["map"], int(time.time() // 1800))
        if self.handled in (key, (t["map"], key[1] - 1)):
            return False
        idx, aliases, min_lvl = BB_MAPS[t["map"]]
        lvl = self.c.s.get("level") or 0
        if lvl < min_lvl:
            self.skip(key, f"level {lvl} can't /move to {t['map']} (needs {min_lvl})")
            return False
        why = self.busy_reason()
        if why:
            if self.last_skip_log != ("busy", key):
                self.last_skip_log = ("busy", key)
                self.log(f"Big Boss in {t['map']} but I'm {why} -> going when that's over")
            return False
        self.begin(key, t)
        return True

    def begin(self, key, t):
        a, c = self.a, self.c
        self.handled = key
        self.phase = "go"
        self.started = time.time()
        self.map_name = t["map"]
        self.target = (t["x"], t["y"])
        self.map_idx, self.aliases, _ = BB_MAPS[t["map"]]
        self.aliases = self.entry_aliases(t["map"], self.target)
        self.warp_tries = 0
        self.last_warp = 0
        self.last_walk = 0
        self.arrived_at = None
        self.first_hit_at = None
        self.dead_at = None
        self.dead_pos = None
        self.linger_until = None
        self.linger_next = 0
        self.countdown_since = None
        self.deaths = 0
        self.was_dead = False
        self.invited = {}
        self.last_status_log = 0
        self.items0 = self.bag_count()
        self.party_note = None
        # interrupt a Golden Invasion hunt (it can start again afterwards if still running)
        gi = getattr(a, "gi", None)
        self.gi_interrupted = False
        if gi is not None and gi.active():
            try:
                import mu_gi as GI
                self.ev(GI.GI_SET_JS, False)
                gi.finish("Big Boss")
                gi.hour_done = None
                self.gi_interrupted = True
            except Exception as e:
                self.log(f"stopping the Golden Invasion: {e!r}")
        install = self.ev(BB_INSTALL_JS, self.cfg())
        self.log(f"=== BIG BOSS in {self.map_name} at {self.target} -> going (was {a.mode}"
                 f"{', Golden Invasion stopped' if self.gi_interrupted else ''}; fight loop {install}) ===")
        a.leave_party("Big Boss")
        c.set_helper(False)
        c.close_windows()
        c.walk_target = None
        self.showed = bool(BB_SHOW_WINDOW)
        if BB_SHOW_WINDOW:
            if c.minimized and not G.HEADLESS:
                c.set_window("normal")
            a.show_window = True
        if BB_RECORD:
            try:
                c.rec.start()
            except Exception:
                pass

    @staticmethod
    def entry_aliases(map_name, target):
        """Order map /move aliases by distance from their landing tile to target."""
        entries = BB_MAP_ENTRIES.get(map_name)
        if not entries:
            return BB_MAPS[map_name][1]
        x, y = target
        ordered = [alias for alias, _ in sorted(
            entries, key=lambda entry: math.hypot(entry[1][0] - x, entry[1][1] - y))]
        # Keep any existing aliases without a known landing tile as fallbacks.
        ordered.extend(alias for alias in BB_MAPS[map_name][1] if alias not in ordered)
        return ordered

    def cfg(self):
        slots = max(len(BB_ORDER), 1)
        i = order_index(self.a.name) % slots
        return {"version": 5, "bossType": BB_BOSS_TYPE, "loopMs": BB_LOOP_MS, "potHp": BB_POT_HP,
                "potEvery": BB_POT_EVERY_MS, "potKey": BB_POTION_KEY, "range": BB_RANGE, "castRange": BB_CAST_RANGE,
                "closeHp": BB_CLOSE_HP, "closeEta": BB_CLOSE_ETA, "closeDist": BB_CLOSE_DIST,
                "closeStuckMs": BB_CLOSE_STUCK_MS, "lootRadius": BB_LOOT_RADIUS, "jewelry": BB_JEWELRY,
                "pickRepeat": BB_PICK_REPEAT_MS, "pickPerTurn": BB_PICK_PER_TURN,
                "limit": BB_LIMIT_LOOT, "looters": BB_LOOTERS, "skipChance": BB_SKIP_CHANCE,
                "valuableOnly": BB_VALUABLE_ONLY,
                "angle": 2 * math.pi * i / slots - math.pi / 2, "slots": slots,
                "skill": BB_SKILLS.get(self.a.name.lower(), 0), "mageSkill": BB_MAGE_SKILL,
                "combo": BB_KNIGHT_COMBO, "comboStep": BB_COMBO_STEP_MS, "buffs": BB_BUFFS}

    def bag_count(self):
        n = self.ev("() => (window.__store.playerData.items || []).filter((x, i) => i >= 12 && x).length")
        return n if isinstance(n, int) else 0

    # ── running ──
    def run(self):
        a, c, s = self.a, self.c, self.c.s
        a.mode = "bb"
        now = time.time()
        if self.linger_until is not None:
            return self.linger_tick(now)
        if BB_RECORD:
            try:
                c.rec.tick()
            except Exception:
                pass
        st = self.ev(BB_STATUS_JS)
        if st is None or not st.get("on"):
            self.ev(BB_INSTALL_JS, self.cfg())       # first tick, or the page reloaded
            self.ev(BB_SET_JS, True)
            st = self.ev(BB_STATUS_JS) or {}
        for e in st.get("events") or []:
            self.log(e)
            if e.startswith("boss DIED"):
                self.dead_at = now
                m = re.search(r"\((\d+),(\d+)\)", e)
                if m:
                    self.dead_pos = (int(m.group(1)), int(m.group(2)))
        if st.get("err"):
            self.log(f"fight loop error: {st['err']}")
        status = st.get("status") or {}
        mode = status.get("mode")
        dead = status.get("dead") or (s.get("hp") or 0) <= 0
        if dead and not self.was_dead:
            self.deaths += 1
            self.log(f"DIED (#{self.deaths}) at ({s.get('x')},{s.get('y')}) -> respawn and go back")
        self.was_dead = dead
        # panel: follow the boss; back to a countdown = it's over (dead or gone)
        t = self.read_timer(1) or {}
        if t.get("map"):
            self.countdown_since = None
            if t["map"] != self.map_name:
                self.log(f"panel now says {t['map']} {t.get('x')},{t.get('y')} -> following")
                self.map_name = t["map"]
                self.map_idx, self.aliases, _ = BB_MAPS.get(t["map"], (self.map_idx, self.aliases, 0))
            self.target = (t["x"], t["y"])
            if self.warp_tries == 0:
                self.aliases = self.entry_aliases(self.map_name, self.target)
        elif t.get("secs") is not None:
            self.countdown_since = self.countdown_since or now
        if self.dead_at:
            self.loot_burst()
            self.ev(BB_SET_JS, False)                  # loot over: stop the in-page loop, just hang around
            self.linger_until = time.time() + random.uniform(*BB_LINGER_SEC)
            self.log(f"lingering {self.linger_until - time.time():.0f} s around the corpse before going back")
            return True
        elif self.countdown_since and now - self.countdown_since >= BB_END_GRACE and mode not in ("fight", "closing", "approach"):
            return self.finish("the panel is counting down again (boss dead or gone)")
        if dead:
            return True
        self.party_tick(status)
        if mode in ("fight", "closing", "buff", "approach", "loot"):
            if self.arrived_at is None:
                self.arrived_at = now
                self.log(f"at the boss {int(now - self.started)} s after leaving")
            if mode == "fight" and self.first_hit_at is None:
                self.first_hit_at = now
        else:
            self.travel(s)
        if now - self.last_status_log > 15:
            self.last_status_log = now
            self.log(f"{mode} | boss {status.get('boss')} hp {self.pct(status.get('bossHp'))} eta {status.get('eta')} | "
                     f"me ({s.get('x')},{s.get('y')}) map {s.get('map')} hp {status.get('hp')}/{status.get('maxHp')} | "
                     f"casts {status.get('casts')}/{status.get('myCasts')} seen | pots {status.get('pots')} | "
                     f"party {s.get('party')}")
        return True

    @staticmethod
    def pct(v):
        return "?" if v is None else f"{round(v / 2.55)}%"

    def travel(self, s):
        """Warp to the boss map if needed, then walk to the panel coordinates."""
        c = self.c
        now = time.time()
        x, y = self.target
        here = s.get("map")
        far = here == self.map_idx and math.hypot((s.get("x") or 0) - x, (s.get("y") or 0) - y) > BB_WARP_IF_FARTHER \
            and self.warp_tries == 0
        if here != self.map_idx or far:
            if self.warp_tries:
                if here != self.map_idx and now - self.last_warp >= BB_WARP_TIMEOUT:
                    return self.finish(f"single /move to {self.map_name} did not land")
                return
            alias = self.aliases[0]
            self.warp_tries += 1
            self.log(f"/move {alias} (nearest entry to {self.target})")
            c.set_helper(False)
            c.close_windows()
            c.chat(f"/move {alias}")
            self.last_warp = now
            c.walk_target = None
            return
        if now - self.last_walk >= BB_WALK_EVERY:
            self.last_walk = now
            ok = self.ev(B.WALK_JS, [int(x), int(y)])
            if not ok:
                # target tile not walkable: try a few around it
                for dx, dy in ((2, 0), (-2, 0), (0, 2), (0, -2), (3, 3), (-3, -3), (3, -3), (-3, 3)):
                    if self.ev(B.WALK_JS, [int(x + dx), int(y + dy)]):
                        break

    def party_of(self, name):
        """Party list of one of ours as last shared (local snapshot, or the other machine via the hub)."""
        hub = self.a.hub
        info = hub.snapshot().get(name)
        if info is None and getattr(hub, "link", None):
            try:
                info = (hub.link.remote() or {}).get(name)
            except Exception:
                info = None
        return list((info or {}).get("party") or [])

    def party_tick(self, status):
        """One party of ours at the boss; whoever got there first leads (no leader switching)."""
        a, s = self.a, self.c.s
        now = time.time()
        low = lambda n: (n or "").lower()
        friends = list(getattr(a.hub, "friends", a.hub.names))
        ours_low = {low(n) for n in friends} | {low(a.name)}
        party = s.get("party") or []
        in_party = len(party) > 1
        # invites: the first one from ours while I'm alone; never strangers'
        if s.get("partyRequest"):
            text = s.get("partyRequestText", "")
            who = next((n for n in friends if low(n) != low(a.name) and f'"{n}"' in text), None)
            mi = getattr(self, "merge_into", None)
            if who and mi and now < mi[1] and low(who) not in mi[0]:
                who = None                     # joining the other party: not my old partner's invite
            if who and not in_party:
                self.log(f"accepting {who}'s party invite")
                self.ev("() => window.__social.partyRespond(true)")
            else:
                self.ev("() => window.__social.partyRespond(false)")
            return
        strangers = [n for n in party if low(n) not in ours_low]
        if in_party and strangers:
            return a.leave_party(f"Big Boss: strangers in it ({', '.join(strangers)})")
        near = [o["name"] for o in (self.ev(OURS_JS, friends) or []) if o["d"] <= BB_PARTY_RADIUS]
        if not near:
            return
        mine = {low(n) for n in party}
        others = {n: self.party_of(n) for n in near if low(n) not in mine}
        # two parties of ours here (2026-10-15: PC trio + Mac pair arrived in separate parties, the hub was
        # offline): after BB_MERGE_AFTER s the smaller party leaves and joins the other one. The other
        # party's size comes from the hub when known, else from how many of ours in view aren't in mine.
        # Same size -> the party whose best BB_ORDER member comes later leaves. Both sides compute the same.
        if in_party:
            known = [p for p in others.values() if len(p) > 1 and all(low(x) in ours_low for x in p)]
            other_size = max(len(p) for p in known) if known else len(others)
            if other_size >= 2:
                self.split_since = getattr(self, "split_since", None) or now
                mine_best = min(order_index(n) for n in party)
                other_best = min(order_index(n) for n in others)
                leave = other_size > len(party) or (other_size == len(party) and other_best < mine_best)
                if leave and now - self.split_since >= BB_MERGE_AFTER:
                    self.split_since = None
                    # for a while: only accept the other party's invite and don't invite anyone myself
                    # (my old partner leaves too: we must not just re-form our pair)
                    self.merge_into = ({low(n) for n in others}, now + 15)
                    return a.leave_party(f"Big Boss: another party of ours here ({other_size} vs our {len(party)}: "
                                         f"{', '.join(others)}) -> joining theirs")
            else:
                self.split_since = None
        else:
            self.split_since = None
        if not in_party and any(len(p) > 1 for p in others.values()):
            note = "waiting for an invite to the party of ours that's already here"
            if note != self.party_note:
                self.party_note = note
                self.log(note)
            return
        if len(party) >= 5:
            return
        mi = getattr(self, "merge_into", None)
        if mi and now < mi[1] and not in_party:
            return                             # waiting for the other party's invite
        # alone (first here) or already in a party of ours: invite ours here who aren't in a party
        for n, p in others.items():
            if len(p) > 1:
                continue
            if now - self.invited.get(n, 0) < BB_INVITE_EVERY:
                continue
            self.invited[n] = now
            self.log(f"party invite {n}: {self.ev(G.INVITE_JS, n)}")

    def linger_tick(self, now):
        """After the loot: a few small random steps around the corpse, then back to normal."""
        s = self.c.s
        if now >= self.linger_until:
            return self.finish("boss dead, loot + linger over")
        if (s.get("hp") or 0) <= 0:
            return True
        if now >= self.linger_next:
            self.linger_next = now + random.uniform(*BB_LINGER_STEP)
            cx, cy = self.dead_pos or (s.get("x"), s.get("y"))
            if cx is None:
                return True
            for _ in range(6):
                a = random.uniform(0, 2 * math.pi)
                r = random.uniform(1, BB_LINGER_RADIUS)
                x, y = int(round(cx + math.cos(a) * r)), int(round(cy + math.sin(a) * r))
                if (x, y) != (s.get("x"), s.get("y")) and self.ev(B.WALK_JS, [x, y]):
                    break
        return True

    def loot_burst(self):
        """The boss died: the in-page loop asks for every drop non-stop (it started the instant the boss died).
        Keep it going + press Space for at least BB_LOOT_SEC, then until the ground around the corpse has been
        empty BB_LOOT_EMPTY s, at most BB_LOOT_MAX s from the kill."""
        c = self.c
        start = self.dead_at or time.time()
        presses, check_at, empty_since, status = 0, 0, None, {}
        while True:
            now = time.time()
            if now - start >= BB_LOOT_MAX:
                break
            if BB_SPACE_SPAM and not (BB_LIMIT_LOOT and BB_VALUABLE_ONLY):
                try:
                    c.page.keyboard.press("Space")
                    presses += 1
                except Exception:
                    pass
            if now - check_at >= 0.3:
                check_at = now
                st = self.ev(BB_STATUS_JS) or {}
                for e in st.get("events") or []:
                    self.log(e)
                status = st.get("status") or status
                if status.get("items"):
                    empty_since = None
                else:
                    empty_since = empty_since or now
                if now - start >= BB_LOOT_SEC and empty_since and now - empty_since >= BB_LOOT_EMPTY:
                    break
            time.sleep(BB_SPACE_EVERY)
        st = self.ev(BB_STATUS_JS) or {}
        for e in st.get("events") or []:
            self.log(e)
        status = st.get("status") or status
        self.log(f"loot: {time.time() - start:.0f} s, {status.get('picks')} pickup requests ({status.get('direct')} direct, "
                 f"{status.get('pickFails')} refused), {presses} Space presses"
                 + (f" | limited: {'looter' if status.get('looter') else 'not a looter'}, "
                    f"{status.get('skipped')} item(s) left on purpose" if BB_LIMIT_LOOT else ""))

    # ── end ──
    def on_pause(self):
        self.ev(BB_SET_JS, False)

    def finish(self, why):
        a, c = self.a, self.c
        self.ev(BB_SET_JS, False)
        st = self.ev(BB_STATUS_JS) or {}
        for e in st.get("events") or []:
            self.log(e)
        status = st.get("status") or {}
        got = self.bag_count() - self.items0
        now = time.time()
        arrive = f"{int(self.arrived_at - self.started)} s" if self.arrived_at else "never"
        hit = f"{int((self.dead_at or now) - self.first_hit_at)} s" if self.first_hit_at else "0 s"
        self.log(f"=== BIG BOSS over ({why}): arrived after {arrive}, hit it for {hit}, "
                 f"items +{got} in the bag, casts {status.get('casts')} ({status.get('myCasts')} seen), "
                 f"pickups tried {status.get('picks')} (failed {status.get('pickFails')}), deaths {self.deaths} -> back to normal ===")
        if BB_RECORD:
            try:
                c.rec.stop()
            except Exception:
                pass
        if len(c.s.get("party") or []) > 1:
            a.leave_party("Big Boss over")
        self.phase = "idle"
        if getattr(self, "showed", False):
            a.show_window = False
        a.step_key = None
        c.walk_target = None
        c.spot = None
        c.relocating = True
        return False

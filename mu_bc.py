"""
Blood Castle detour for mu_group_bot.py.

When wizzardo's level is in the range of Blood Castle 2, 3 or 4 and the next Blood Castle
opens in <= 10 minutes, the group stops leveling and:
  1. everyone warps to Lorencia; characters in the SAME castle level as wizzardo buy one
     Invisibility Cloak from Liaman Barmaid, the others wait at the bar;
  2. participants warp to Devias, stand next to the Spirit of Archangel and talk to him as
     soon as the entry window opens (-> warped into the castle);
  3. inside, wizzardo invites our participants (and never accepts a stranger's invite); if there are 1-3 other players, he kicks that
     many alts (they wait in the castle) and invites the strangers until 30 s before the door
     closes; free slots are then given back to our alts;
  4. wizzardo walks up the corridor (helper on for a few seconds whenever he gets stuck),
     destroys the statue, picks up the Archangel weapon and returns it to the Archangel; the alts
     follow him by position (in the party or not). Whoever of ours has the weapon delivers it.
     If ANOTHER player gets the weapon, everyone goes back to the Archangel (entrance) at once,
     with the same stuck -> helper-burst walking, to be there when it is delivered;
  5. every character tries each Box of Kundun +5 on the ground until one pickup succeeds;
  6. everyone leaves to Lorencia and leaves a stranger party;
  7. at the Lorencia bar every alt trades its Boxes of Kundun (+1..+5) to wizzardo;
     then the normal reset loop resumes.
"""
import math
import time

import roster as R

# ───────────────────────────── CONFIG ─────────────────────────────
BC_ENABLED        = True
BC_TIERS          = [(1, 15, 80), (2, 81, 130), (3, 131, 180), (4, 181, 230),
                     (5, 231, 280), (6, 281, 330), (7, 331, 400)]   # (castle, min lvl, max lvl)
BC_TARGET_TIERS   = {2, 3, 4}       # castles we go to (the less crowded ones)
BC_PREP_SEC       = 600             # start preparing this long before the entry window opens
BC_DOOR_CLOSE_SEC = 300             # entry closes this long after the window opens (castle starts ~5.5 min)
BC_INVITE_MARGIN  = 30              # stop waiting for strangers this long before the door closes
BC_MAX_STRANGERS  = 3               # 1..3 other players -> make room for them in our party
BC_REBUY_NEEDS_SEC = 90            # cloak missing: go buy another only if this long is left before the door closes
BC_MAX_REBUYS     = 2             # ... and at most this many times per castle
BC_ENTRY_NEEDS_SEC = 60             # window already open: still go if at least this long is left before the door closes

BAR_SPOT      = (0, 122, 133)       # Lorencia bar (Liaman Barmaid at 123,135)
BAR_NPC       = "Liaman Barmaid"
CLOAK         = (13, 18)            # Invisibility Cloak, item level = castle level
SPIRIT_SPOT   = (2, 218, 29)        # Devias, next to Spirit of Archangel (217,29)
SPIRIT_NPC    = 233
BC_MAP        = 12                  # Castle 2 (kept for reference)
# Each castle level is its OWN map (classic MU: Blood Castle 1-7 = maps 11-17, 8 = 52).
# Only Castle 2 (map 12) had been seen, so the bot used to think a character in Castle 4
# was outside and /move-d it back to Devias.
BC_MAPS       = {11, 12, 13, 14, 15, 16, 17, 52}


def in_bc(m):
    return m in BC_MAPS
BC_START      = (12, 6)
BC_WAIT       = (13, 13)            # wait here (by the bridge) until the castle starts; falls back to
                                    # the nearest walkable tile within 2, then to BC_START
ARCHANGEL     = (12, 12)            # stand here to deliver / wait for the boxes (Archangel ≈ 12,11)
ARCHANGEL_NPC = 232
WEAPON        = (5, 10)             # Archangel's weapon: "Weapon of Archangel" [5/10 lvl 0] (seen 2026-10-10, BC3)
WEAPON_CODES  = [(5, 10), (13, 19), (0, 19), (4, 18)]   # 5/10 = how it shows on the ground; 13/19 = in the bag (confirmed 2026-10-10 15:07, bruto BC7); 0/19, 4/18 = classic codes, unconfirmed
BOX           = (14, 11, 12)        # Box of Kundun +5
# The weapon ALWAYS drops on the Statue of Saint's tile (14,95): 27 of 27 sightings in the logs,
# 2026-10-10/11, castles 2-7. We stand on the tile next to it (corridor side) so the pickup is
# one tile away, and hit the statue / its guards from there.
WEAPON_DROP   = (14, 95)
STATUE        = (14, 94)            # our stand spot, next to the statue / weapon drop
STATUE_RADIUS = 1                   # this close = on the spot (helper mode: helper on; no-helper mode: guard the spot)
STUCK_SEC     = 2.0                 # no walking progress this long -> stuck
HELPER_BURST  = 3.0                 # helper on this long when stuck, then keep walking
CLEAR_RADIUS  = 7                   # scan radius for the weapon on the ground
FOLLOW_DIST   = 4
BOX_TRY_SEC   = 3.5
END_WAIT_SEC  = 5                  # after the castle ends: time to pick boxes before leaving
TRADE_BOXES   = True                # afterwards, alts trade every Box of Kundun (+1..+5) to wizzardo
KUNDUN_MIN_LVL = 8                  # Box of Kundun +1 = item level 8 ... +5 = 12 (Zen Chest = 4, not traded)
TRADE_SPOT    = (0, 122, 133)       # Lorencia bar
# ──────────────────────────────────────────────────────────────────

BC_TIMER_JS = """
() => {
  for (const e of document.querySelectorAll('.event-row-label')) {
    const t = (e.parentElement || e).textContent || '';
    if (!/Blood Castle/i.test(t)) continue;
    const text = t.replace(/\\s+/g, ' ').trim().slice(0, 80);
    if (/\\bopen/i.test(t)) return {open: true, text};      // "Open" wins over any number in the row
    const m = t.match(/(\\d+):(\\d+)(?::(\\d+))?/);
    if (!m) return {open: true, text};
    const n = m.slice(1).filter(v => v !== undefined).map(Number);
    return {secs: n.length === 3 ? n[0] * 3600 + n[1] * 60 + n[2] : n[0] * 60 + n[1], text};
  }
  return null;
}
"""

BC_HOOK_JS = """
() => {
  if (window.__bcHook || !window.__eventBus) return !!window.__bcHook;
  window.__bcHook = true; window.__bcState = null; window.__bcPickFail = 0;
  window.__eventBus.on('BloodCastleState', d => { try {
    const u = new Uint8Array(d.buffer, d.byteOffset, d.byteLength);
    window.__bcState = {st: u[3], rem: u[4] | u[5] << 8, max: u[6] | u[7] << 8, cur: u[8] | u[9] << 8,
                        own: u[10] | u[11] << 8, wpn: u[12], t: Date.now()};
  } catch (e) {} });
  window.__eventBus.on('ItemPickUpRequestFailed', () => { window.__bcPickFail++; });
  // the server's answer to an entry click (0 = ok; anything else = refused, see BC_ENTER_RESULTS)
  window.__eventBus.on('BloodCastleEnterResult', d => { try {
    const u = new Uint8Array(d.buffer, d.byteOffset, d.byteLength);
    window.__bcEnter = {hex: [...u].map(b => b.toString(16).padStart(2, '0')).join(' '), code: u[u.length - 1], t: Date.now()};
  } catch (e) {} });
  window.__eventBus.on('ServerMessage', d => { try {
    const u = new Uint8Array(d.buffer, d.byteOffset, d.byteLength);
    const txt = new TextDecoder().decode(u.slice(4)).replace(/\\0.*$/s, '');
    if (!/entered the game/.test(txt)) window.__bcServerMsg = {txt, t: Date.now()};
  } catch (e) {} });
  return true;
}
"""

BC_STATE_JS = "() => window.__bcState"

# party members vs the spawn area (delivery wait). A member is "here" when we can see him within r tiles
# of (ax, ay); "gone" when the party data puts him on another map. keys = the party entry's fields
# (logged once, to learn what the client keeps there).
PARTY_SPAWN_JS = """([ax, ay, r, me]) => {
  const soc = window.__social, w = window.__world, pd = (window.__store && window.__store.playerData) || {};
  const pm = (soc && soc.partyMembers) || [];
  const ents = {};
  for (const e of (w ? w.netObjsQuery.entities : [])) {
    if (!e.transform || e.droppedItem || e.npcType !== undefined) continue;
    const n = (e.objectNameInWorld || '').trim().toLowerCase(); if (n) ents[n] = e;
  }
  const myMap = pd.map ?? pd.mapNumber ?? pd.mapId ?? null;
  const out = [];
  for (const m of pm) {
    const name = (m.name || '').trim(); if (!name || name.toLowerCase() === (me || '').toLowerCase()) continue;
    const e = ents[name.toLowerCase()];
    const mp = m.map ?? m.mapNumber ?? m.mapId ?? m.mapIndex ?? null;
    const x = e ? e.transform.pos.x : null, y = e ? e.transform.pos.z : null;
    out.push({name, visible: !!e, x: x === null ? null : Math.round(x), y: y === null ? null : Math.round(y),
              map: mp, here: !!e && Math.hypot(x - ax, y - ay) <= r,
              dead: !!(e && e.dying), keys: Object.keys(m).slice(0, 25).join(',')});
  }
  return {members: out, myMap};
}"""

# last entry answer + last server message (both since time t, epoch ms)
BC_ENTER_RESULT_JS = """
(t) => ({enter: window.__bcEnter && window.__bcEnter.t >= t ? window.__bcEnter : null,
         msg: window.__bcServerMsg && window.__bcServerMsg.t >= t ? window.__bcServerMsg.txt : null})
"""
# classic MU BloodCastleEnterResult codes (this server's may differ: the raw bytes are logged too)
BC_ENTER_RESULTS = {0: "ok", 1: "wrong cloak / no cloak", 2: "not open", 3: "level too low",
                    4: "level too high", 5: "castle full", 6: "no more entries today"}

INV_COUNT_JS = """
([g, n, l]) => (window.__store.playerData.items || []).filter((it, i) =>
    i >= 12 && it && it.group === g && it.num === n && (l === null || it.lvl === l)).length
"""

# how many Archangel weapons (any of WEAPON_CODES) are in the bag
WEAPON_INV_JS = """
(codes) => (window.__store.playerData.items || []).filter((it, i) =>
    i >= 12 && it && codes.some(([g, n]) => it.group === g && it.num === n)).length
"""

# what is open on screen (store windows + visible DOM dialogs), to learn the post-delivery dialog
OPEN_UI_JS = """
() => {
  const s = window.__store, out = {flags: [], dom: []};
  for (const k of Object.keys(s)) if (/Enabled$/.test(k) && k !== 'minimapEnabled' && s[k] === true) out.flags.push(k);
  for (const k of ['msgWin', 'npcShop', 'pendingNpcTalk', 'offlineNpcWindow'])
    if (s[k]) { let v; try { v = JSON.stringify(s[k]).slice(0, 200); } catch (e) { v = String(s[k]); } out.flags.push(k + '=' + v); }
  if (window.__commands && window.__commands.windowOpen) out.flags.push('commands.windowOpen');
  for (const b of document.querySelectorAll('button, [role=button], .mu-button, [class*=button], [class*=btn]')) {
    const r = b.getBoundingClientRect(), t = (b.textContent || '').trim();
    if (r.width && r.height && t && t.length < 20) out.dom.push(t);
  }
  out.dom = [...new Set(out.dom)].slice(0, 30);
  return out;
}
"""

# the game's message window (e.g. the one the Archangel leaves after the delivery; it stays on
# screen even after leaving the castle). Returns its text and closes it: closeMsgWin / msgWinOk /
# a visible OK button. null = nothing open.
MSGWIN_JS = """
() => {
  const s = window.__store; if (!s || !s.msgWin) return null;
  let txt; try { txt = JSON.stringify(s.msgWin).slice(0, 300); } catch (e) { txt = String(s.msgWin); }
  let how = null;
  try { if (typeof s.closeMsgWin === 'function') { s.closeMsgWin(); how = 'closeMsgWin'; } } catch (e) {}
  try { if (s.msgWin && typeof s.msgWinOk === 'function') { s.msgWinOk(); how = 'msgWinOk'; } } catch (e) {}
  if (s.msgWin) {
    for (const b of document.querySelectorAll('button, [role=button], .mu-button, [class*=button], [class*=btn]')) {
      const r = b.getBoundingClientRect(), t = (b.textContent || '').trim();
      if (r.width && r.height && /^(ok|aceptar)$/i.test(t)) { b.click(); how = 'clicked ' + t; break; }
    }
  }
  return {txt, how, still: !!s.msgWin};
}
"""
MSGWIN_CHECK_SEC = 5                  # look for an open message window this often

# The Archangel's "Ah! Great warrior..." dialog after the delivery is NOT msgWin: it is the store's
# pendingNpcTalk, and its OK is not a <button> (text is letter-spaced "O K"), so neither MSGWIN_JS
# nor CLICK_OK_JS caught it. Closes it by: a store close/clear function, clearing the field, and
# clicking any visible element whose text is "OK" once spaces are removed. null = nothing open.
NPC_TALK_CLOSE_JS = """
(types) => {
  const s = window.__store; if (!s || !s.pendingNpcTalk) return null;
  const t = s.pendingNpcTalk;
  if (types && types.length && !types.includes(t.npcType)) return null;
  const how = [];
  for (const k of Object.keys(s)) {
    if (typeof s[k] !== 'function' || k === 'talkToNpc') continue;
    if (/^(close|clear|cancel|end|dismiss|reset).*(npc|talk|dialog)/i.test(k)) {
      try { s[k](); how.push(k + '()'); } catch (e) {}
    }
  }
  const fire = (el) => {
    for (const ev of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click'])
      el.dispatchEvent(new MouseEvent(ev, {bubbles: true, cancelable: true, view: window}));
  };
  for (const el of document.querySelectorAll('body *')) {
    if (el.children.length > 1) continue;
    const txt = (el.textContent || '').replace(/\\s+/g, '');
    if (!/^(ok|aceptar)$/i.test(txt)) continue;
    const r = el.getBoundingClientRect(); if (!r.width || !r.height) continue;
    fire(el); how.push('clicked <' + el.tagName.toLowerCase() + '>'); break;
  }
  if (s.pendingNpcTalk) { try { s.pendingNpcTalk = null; how.push('cleared'); } catch (e) {} }
  return {name: t.name, npcType: t.npcType, how: how.join(', '), still: !!s.pendingNpcTalk};
}
"""
NPC_TALK_CLOSE_TYPES = [232]          # Archangel only (don't touch BC entry / shop talks)
NPC_TALK_CLOSE_AFTER = 8              # seconds it must stay open before we close it (delivery done)

# The Archangel's "Ah! Great warrior..." dialog after the delivery is NOT msgWin: it is the store's
# pendingNpcTalk, and its OK is not a <button> (text is letter-spaced "O K"), so neither MSGWIN_JS
# nor CLICK_OK_JS caught it. Closes it by: a store close/clear function, clearing the field, and
# clicking any visible element whose text is "OK" once spaces are removed. null = nothing open.
NPC_TALK_CLOSE_JS = """
(types) => {
  const s = window.__store; if (!s || !s.pendingNpcTalk) return null;
  const t = s.pendingNpcTalk;
  if (types && types.length && !types.includes(t.npcType)) return null;
  const how = [];
  for (const k of Object.keys(s)) {
    if (typeof s[k] !== 'function' || k === 'talkToNpc') continue;
    if (/^(close|clear|cancel|end|dismiss|reset).*(npc|talk|dialog)/i.test(k)) {
      try { s[k](); how.push(k + '()'); } catch (e) {}
    }
  }
  const fire = (el) => {
    for (const ev of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click'])
      el.dispatchEvent(new MouseEvent(ev, {bubbles: true, cancelable: true, view: window}));
  };
  for (const el of document.querySelectorAll('body *')) {
    if (el.children.length > 1) continue;
    const txt = (el.textContent || '').replace(/\\s+/g, '');
    if (!/^(ok|aceptar)$/i.test(txt)) continue;
    const r = el.getBoundingClientRect(); if (!r.width || !r.height) continue;
    fire(el); how.push('clicked <' + el.tagName.toLowerCase() + '>'); break;
  }
  if (s.pendingNpcTalk) { try { s.pendingNpcTalk = null; how.push('cleared'); } catch (e) {} }
  return {name: t.name, npcType: t.npcType, how: how.join(', '), still: !!s.pendingNpcTalk};
}
"""
NPC_TALK_CLOSE_TYPES = [232]          # Archangel only (don't touch BC entry / shop talks)
NPC_TALK_CLOSE_AFTER = 8              # seconds it must stay open before we close it (delivery done)

# click a visible OK / Close button (fallback when closing the store windows didn't clear the dialog)
CLICK_OK_JS = """
() => {
  const want = /^(ok|close|cerrar|aceptar|confirm|continue|x)$/i;
  for (const b of document.querySelectorAll('button, [role=button], .mu-button, [class*=button], [class*=btn]')) {
    const r = b.getBoundingClientRect(), t = (b.textContent || '').trim();
    if (r.width && r.height && want.test(t)) { b.click(); return t; }
  }
  return null;
}
"""

TALK_NPC_JS = """
(t) => {
  const w = window.__world, h = w.playerEntity; if (!h) return false;
  const p = h.transform.pos;
  const e = w.netObjsQuery.entities.filter(e => e.npcType === t)
      .sort((a, b) => Math.hypot(a.transform.pos.x - p.x, a.transform.pos.z - p.z)
                    - Math.hypot(b.transform.pos.x - p.x, b.transform.pos.z - p.z))[0];
  if (!e) return false;
  window.__store.talkToNpc({netId: e.netId, name: e.objectNameInWorld, npcType: e.npcType});
  return true;
}
"""

WAIT_SPOT_JS = """
([x, y, fx, fy]) => {
  const w = window.__world;
  if (!w || !w.isWalkable) return [fx, fy];
  for (let r = 0; r <= 2; r++)
    for (let dx = -r; dx <= r; dx++)
      for (let dy = -r; dy <= r; dy++)
        if (Math.max(Math.abs(dx), Math.abs(dy)) === r && w.isWalkable(x + dx, y + dy)) return [x + dx, y + dy];
  return [fx, fy];
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

# other players in view (not ours), monsters near me, ground items of a kind
SCAN_JS = """
([ours, r, g, n, l]) => {
  const w = window.__world, h = w.playerEntity; if (!h) return null;
  const p = h.transform.pos, out = {players: [], monsters: 0, items: []};
  for (const e of w.netObjsQuery.entities) {
    if (e.localPlayer) continue;
    const d = Math.hypot(e.transform.pos.x - p.x, e.transform.pos.z - p.z);
    if (e.droppedItem) {
      const it = e.droppedItem;
      if (!it.isMoney && it.group === g && it.num === n && (l === null || (it.item && it.item.lvl === l)))
        out.items.push({id: e.netId, d});
    } else if (e.npcType !== undefined) {
      if (e.monsterAnimation && !e.dying && e.npcType !== 232 && e.npcType !== 233 && d <= r) out.monsters++;
    } else if (e.charAppearance && e.objectNameInWorld && !ours.includes(e.objectNameInWorld)) {
      out.players.push(e.objectNameInWorld);
    }
  }
  out.items.sort((a, b) => a.d - b.d);
  return out;
}
"""

# ── weapon diagnostics (2026-10-10: solo BC4 run never saw the weapon on the ground) ──
# ground-item name fallback when the 13/19 code doesn't match. Only group-13 items, and only the
# quest weapons: "archangel|divine" also matched Divine Boots/Gloves (11/19, 10/19) and the
# Scroll of Archangel (13/16) -> 2026-10-10 13:05 castle lost by three characters "delivering" armor.
# 2026-10-10 14:07: the ground name is exactly "Weapon of Archangel" (5/10). Anchored, so traded
# "Excellent Staff of Archangel +14", Divine armor and the Scroll of Archangel never match.
WEAPON_NAME_RE = "^(weapon|staff|sword|crossbow) of archangel$"
DELIVER_TIMEOUT_SEC = 3               # talking to the Archangel this long with no weapon in the bag -> not the weapon
GROUND_RADIUS  = 15                   # ground-item dump radius near the statue
GROUND_NEAR    = 12                   # start dumping the ground this close to the statue
GROUND_LOG_SEC = 5                    # check the ground this often near the statue
GROUND_LOG_FORCE_SEC = 30             # log the ground even if unchanged this often

# every ground item within r tiles (r null = all): name, codes, position
GROUND_JS = """
(r) => {
  const w = window.__world, h = w.playerEntity; if (!h) return null;
  const p = h.transform.pos, out = [];
  for (const e of w.netObjsQuery.entities) {
    if (!e.droppedItem) continue;
    const it = e.droppedItem;
    const d = Math.hypot(e.transform.pos.x - p.x, e.transform.pos.z - p.z);
    if (r !== null && d > r) continue;
    out.push({id: e.netId, name: e.objectNameInWorld || '', money: !!it.isMoney,
              g: it.group, n: it.num, lvl: it.item ? it.item.lvl : null,
              x: Math.round(e.transform.pos.x), y: Math.round(e.transform.pos.z), d: Math.round(d)});
  }
  out.sort((a, b) => a.d - b.d);
  return out;
}
"""

# the Archangel's weapon on the ground: by item code OR by name
WEAPON_SCAN_JS = """
([g, n, re]) => {
  const w = window.__world, h = w.playerEntity; if (!h) return [];
  const p = h.transform.pos, rx = new RegExp(re, 'i'), out = [];
  for (const e of w.netObjsQuery.entities) {
    if (!e.droppedItem || e.droppedItem.isMoney) continue;
    const it = e.droppedItem, name = e.objectNameInWorld || '';
    const byCode = it.group === g && it.num === n, byName = rx.test(name.trim());
    if (!byCode && !byName) continue;
    out.push({id: e.netId, name, g: it.group, n: it.num, lvl: it.item ? it.item.lvl : null,
              by: byCode ? 'code' : 'name',
              d: Math.hypot(e.transform.pos.x - p.x, e.transform.pos.z - p.z)});
  }
  out.sort((a, b) => a.d - b.d);
  return out;
}
"""

# bag contents (slots 12+) as "group/num/lvl" strings
BAG_JS = """
() => (window.__store.playerData.items || []).map((it, i) =>
    i >= 12 && it ? `${it.group}/${it.num}/${it.lvl}` : null).filter(Boolean)
"""

# ── fast weapon grab (2026-10-11) ──
# Logs showed the weapon on the ground for 5-6 s before we even sent the first pickup: the Python
# tick is 1/s and every MU Helper toggle blocks it ~5 s waiting for the server. Same fix as the
# Golden Invasion fight loop: a loop INSIDE the page watches the ground while the castle runs.
#
# v3 (2026-10-11 19:06, NicoBM took it from bruto): v1 saw the weapon 2 tiles away and reacted in
# 0 ms, but it was gone 251 ms later with 0 pickups refused -> our pickup never reached the server.
# `pickupTarget` is a mouse click: the client WALKS next to the item first and only then sends the
# pickup. v3:
#  1. sends the pickup packet DIRECTLY (`__store.pickupItemRequest(netId)`, no walking) the instant
#     the weapon appears, re-sent every GRAB_PICK_MS; `pickupTarget` is still set too, so if the
#     server refuses from too far (refused count in the log) the client walks in and retries itself.
#  2. reacts to the game's `ItemsDropped` event (no waiting for the next 50 ms poll).
#  3. Statue of Saint (npcType 132, appears at (14,95) once the phase-4 guards die) in view ->
#     attack it ourselves, no MU Helper: the helper wanders off to other targets, and the last
#     hit decides who may pick the weapon up first ("This item doesn't belong to you") when the
#     killer is not in our party. Python falls back to the helper after STATUE_SOLO_SEC.
# Python only arms/disarms it, logs it, and turns the helper off (without waiting).
# v5 (2026-10-12, Juan): NO MU HELPER IN BLOOD CASTLE. The helper made the characters slow to react
# (every toggle ~5 s, and on 2026-10-12 it never turned on in the minimized windows: 3 min stuck at
# the gate), picked its own targets and walked them away from the statue. The in-page loop now also
# WALKS and FIGHTS, 20x/s:
#   1. weapon on the ground -> direct pickup (as v3)
#   2. Statue of Saint in view and we're on the stand spot -> attack it ourselves (last hit = ours)
#   3. buffs: Soul Barrier (mages) / Greater Fortitude (knights) when the fight starts, then every
#      FIGHT_BUFF_EVERY_MS
#   4. a monster IN SKILL RANGE that is in the way -> cast at it from where we stand (never chase):
#        advance : monsters in a corridor ahead of us, towards the goal (FIGHT_CONE_HALF tiles wide
#                  each side); stuck (no progress FIGHT_STUCK_MS) -> anything in range, any direction;
#                  still nothing for FIGHT_HUNT_MS (e.g. alone in the kill phase at the gate) -> the
#                  nearest monster within FIGHT_LEASH tiles (the client walks into range for that cast)
#        rush    : running to the Archangel: only monsters right in front (FIGHT_RUSH_RANGE), anything
#                  in range only when stuck
#        guard   : on the stand spot by the statue: anything in range, but we don't leave the spot
#        hold    : no fighting (standing at the Archangel to talk to him)
#      monsters first, the Castle Gate only when no monster is in range
#   5. nothing to hit -> walk to the goal (re-sent every FIGHT_WALK_MS)
#   + a potion (hotkey Q) every FIGHT_POT_MS while HP < FIGHT_POT_HP of max
# Skills: Ice Storm (39) for whoever owns it (mages, range FIGHT_MAGE_RANGE); Dark Knights the same
# combo as the Golden Invasion (weapon skill -> Death Stab -> Twisting Slash, range FIGHT_KNIGHT_RANGE);
# anyone else: the selected skill, FIGHT_OTHER_RANGE. castRequest only (no attackTarget: that makes
# the client chase the monster).
# Python sets the goal + mode every tick, logs the events and a status line every FIGHT_LOG_SEC.
GRAB_VERSION   = 7                   # v7 (2026-10-14): kill-phase zone at the bridge, no gate before the bridge drops,
                                    # self-start at castle state 0, no buffs while just walking
# Kill phase (Juan 2026-10-14): the bridge to the gate only drops once enough monsters are dead (castle
# state 1 -> 4). Until then: no attacking the gate; clear monsters, never further than BRIDGE_ZONE_R tiles
# from the bridge edge (where everybody stalls, (13-15, 68-69) in every castle).
BRIDGE_EDGE    = (14, 68)
BRIDGE_ZONE_R  = 8
# Delivery (Juan 2026-10-14): holding the weapon at the Archangel, wait until every party member is in the
# spawn area (within DELIVER_SPAWN_R of the Archangel); members who left the castle (other map / gone /
# not in the party any more) don't count. Never longer than DELIVER_WAIT_MAX, nor with less than
# DELIVER_MIN_REM seconds left on the castle clock.
DELIVER_WAIT_PARTY = True
DELIVER_SPAWN_R    = 12
DELIVER_WAIT_MAX   = 6
DELIVER_MIN_REM    = 60
BC_USE_HELPER  = False               # True = the old behaviour (helper bursts when stuck, helper at the statue)
GRAB_LOOP_MS   = 50                  # in-page check period (20x/s), on top of the ItemsDropped event
GRAB_PICK_MS   = 100                 # re-send the pickup this often while the weapon is on the ground
GRAB_RADIUS    = 20                  # tiles: chase the weapon this far
STATUE_TYPES   = [132, 133, 134]     # Statue of Saint (132 seen 2026-10-11 00:06, BC map 12); 133/134 = classic variants
GATE_TYPES     = [131]               # Castle gate (131, at (14,75), seen 2026-10-11)
NPC_TYPES      = [232, 233]          # Archangel / Spirit of Archangel: never targets
STATUE_ATTACK_RADIUS = 15            # tiles: attack the statue in-page from this close
STATUE_CAST_MS = 100                 # re-send the attack this often
STATUE_SOLO_SEC = 10                 # (helper mode only) statue still standing this long -> MU Helper fallback
BC_FAST_TICK   = 0.25                # Python tick inside the castle (normal: 1 s)
HELPER_ASYNC_GUARD = 4.5             # s: don't press the helper key again before the last press could land
FIGHT_MAGE_SKILL   = 39              # Ice Storm
FIGHT_KNIGHT_COMBO = [[22, 20, 19, 21, 23], 43, 41]   # same as mu_gi.GI_KNIGHT_COMBO
FIGHT_COMBO_STEP_MS = 700
FIGHT_MAGE_RANGE   = 6.0             # tiles: cast at monsters this close (Ice Storm)
FIGHT_KNIGHT_RANGE = 2.5             # knights' skills are melee-ish
FIGHT_OTHER_RANGE  = 3.0
FIGHT_RANGES       = {}              # per character (lower case) -> tiles, overrides the above
FIGHT_CONE_HALF    = 3.0             # "in the way" = within this many tiles of the line towards the goal
FIGHT_RUSH_RANGE   = 3.0             # rushing back: only hit monsters this close in front
FIGHT_STUCK_MS     = 1500            # no progress towards the goal this long -> hit anything in range
FIGHT_HUNT_MS      = 3000            # stuck this long with nothing in range -> go for the nearest monster ...
FIGHT_LEASH        = 10              # ... within this many tiles
FIGHT_CAST_MS      = 100             # re-send the cast this often
FIGHT_WALK_MS      = 400             # re-send the walk this often
FIGHT_POT_KEY      = 0               # item hotkey 0 = Q
FIGHT_POT_HP       = 0.5             # potion while HP below this fraction
FIGHT_POT_MS       = 300
FIGHT_BUFFS        = [16, 48]        # Soul Barrier, Greater Fortitude (each only if owned)
FIGHT_BUFF_EVERY_MS = 55000
FIGHT_BUFF_TRIES   = 3
FIGHT_LOG_SEC      = 5               # Python status line this often

# cfg = {version, on, g, n, re, radius, loopMs, pickMs, weapon, statue, statueTypes, statueRadius, castMs,
#        stand, standR, fight, goal, goalR, mode, gateTypes, npcTypes, mageSkill, combo, comboStep,
#        mageRange, knightRange, otherRange, range, coneHalf, rushRange, stuckMs, huntMs, leash,
#        walkMs, potKey, potHp, potMs, buffs, buffEvery, buffTries}
# installs once per page; returns status + events
BC_GRAB_JS = r"""
(cfg) => {
  const W = window;
  let G = W.__bcGrab;
  if (!G || G.version !== cfg.version) {
    if (G && G.timer) clearInterval(G.timer);
    if (G) G.dead = true;                       // an older version's bus listeners go quiet
    G = W.__bcGrab = {version: cfg.version, on: false, cfg, id: null, seenAt: 0, firstPickAt: 0,
                      lastPick: 0, picks: 0, direct: 0, fails: 0, statueId: null, statueAt: 0,
                      lastCast: 0, casts: 0, events: [], err: null, directErr: null, dead: false,
                      myId: null, myCasts: 0, lastMySkill: null, lastMySkillAt: 0, comboIdx: 0, comboAt: 0,
                      kind: null, range: null, skill: null, tgt: null, tgtName: null, why: '', lastWhy: '',
                      fightCasts: 0, lastWalk: 0, goalKey: null, best: 1e9, bestAt: 0, pots: 0, lastPot: 0,
                      bQueue: [], bTries: 0, bAt: 0, buffAt: {}, buffs: 0, mode: null, walks: 0};
    const me = G;
    const ev = s => { if (G.events.length < 50) G.events.push(s); };
    G.ev = ev;
    const bus = W.__eventBus;
    const u8 = d => new Uint8Array(d.buffer || d, d.byteOffset || 0, d.byteLength);
    if (bus) {
      bus.on('ItemPickUpRequestFailed', () => { if (!me.dead && me.id !== null) me.fails++; });
      // a drop packet arrived: look right away (the entity may be created in this same handler,
      // so check now and once more on the next turn of the event loop)
      bus.on('ItemsDropped', () => {
        if (me.dead || !me.on) return;
        setTimeout(() => { try { me.tick('drop'); } catch (e) {} }, 0);
        setTimeout(() => { try { me.tick('drop'); } catch (e) {} }, 15);
      });
      // c3 09 19 00 <skill u16> <caster u16> <target u16>: my own casts as the server shows them
      const mine = (d) => { try {
        if (me.dead || me.myId === null) return;
        const u = u8(d), skill = (u[3] << 8) | u[4], caster = (u[5] << 8) | u[6];
        if (caster !== me.myId) return;
        me.myCasts++; me.lastMySkill = skill; me.lastMySkillAt = Date.now();
        if (me.bQueue.length && skill === me.bQueue[0]) {
          me.buffAt[skill] = Date.now(); me.bQueue.shift(); me.buffs++; me.bTries = 0; me.bAt = 0;
          ev('buff ' + skill + ' cast');
        }
      } catch (e) {} };
      bus.on('SkillAnimation', mine);
      bus.on('AreaSkillAnimation', mine);
    }
    const P = e => ({x: e.transform.pos.x, y: e.transform.pos.z});
    const owns = (s, n) => !!(n && s.skills && s.skills.some(k => k.number === n));
    const stopStatue = (w, now, why) => {
      if (G.statueId === null) return;
      ev(`statue ${why} ${now - G.statueAt} ms after we started attacking it (${G.casts} attacks sent)`);
      G.statueId = null;
      if (w) { w.attackTarget = null; w.castRequest = null; }
    };
    G.stopStatue = stopStatue;
    const pick = (w, e, now) => {
      G.lastPick = now; G.picks++;
      if (!G.firstPickAt) G.firstPickAt = now;
      const s = W.__store;
      if (s && typeof s.pickupItemRequest === 'function') {
        try { s.pickupItemRequest(e.netId); G.direct++; }
        catch (x) { if (!G.directErr) { G.directErr = String(x).slice(0, 120); ev('direct pickup error: ' + G.directErr); } }
      } else if (!G.directErr) { G.directErr = 'no __store.pickupItemRequest'; ev(G.directErr); }
      w.pickupTarget = e;                        // walk-in fallback (server range check)
    };
    // class from the skills: Ice Storm -> mage; knight combo -> knight; else the selected skill
    const classify = (s, c) => {
      let kind = 'other', range = c.otherRange;
      if (owns(s, c.mageSkill)) { kind = 'mage'; range = c.mageRange; }
      else if ((c.combo || []).some(st => (Array.isArray(st) ? st : [st]).some(n => owns(s, n)))) { kind = 'knight'; range = c.knightRange; }
      if (c.range) range = c.range;
      if (kind !== G.kind || range !== G.range) { G.kind = kind; G.range = range; ev(`fighting as ${kind}, range ${range} tiles`); }
    };
    const pickSkill = (now, s, c) => {
      if (G.kind === 'mage') return c.mageSkill;
      if (G.kind === 'knight') {
        const combo = [];
        for (const st of c.combo || []) { const n = (Array.isArray(st) ? st : [st]).find(o => owns(s, o)); if (n) combo.push(n); }
        if (!combo.length) return null;
        if (G.comboIdx >= combo.length) G.comboIdx = 0;
        const cur = combo[G.comboIdx];
        if ((G.lastMySkill === cur && G.lastMySkillAt > G.comboAt) || now - G.comboAt > c.comboStep) {
          if (G.comboAt) G.comboIdx = (G.comboIdx + 1) % combo.length;
          G.comboAt = now;
        }
        return combo[G.comboIdx];
      }
      return null;                               // other: whatever is selected in the client
    };
    const cast = (w, s, e, now, sk) => {
      try { if (sk && s.currentSkill !== sk) s.selectSkill(sk); } catch (x) {}
      if (!w.castRequest && now - G.lastCast >= G.cfg.castMs) {
        const p = P(e);
        w.castRequest = {target: e, point: {x: p.x, y: p.y}, forced: false, pvp: false};
        G.lastCast = now; return true;
      }
      return false;
    };
    const walkTo = (w, h, x, y) => {
      if (!w.isWalkable || !w.isWalkable(x, y)) return false;
      const m = h.playerMoveTo, pf = h.pathfinding;
      m.point.x = x; m.point.y = y; m.handled = false; m.sendToServer = true;
      pf.from.x = h.transform.pos.x; pf.from.y = h.transform.pos.z;
      pf.to.x = x; pf.to.y = y; pf.calculated = false;
      return true;
    };
    const OFFS = [[0, 0], [1, 0], [-1, 0], [0, -1], [0, 1], [1, -1], [-1, -1], [1, 1], [-1, 1], [2, 0], [-2, 0], [0, 2], [0, -2]];
    // buffs (owned ones only): queued when the fight starts and every buffEvery; one cast at a time,
    // confirmed by the server's skill animation. True = casting a buff now (skip the fight this tick).
    const buffTick = (now, w, s, h) => {
      const c = G.cfg;
      for (const n of c.buffs || [])
        if (owns(s, n) && !G.bQueue.includes(n) && now - (G.buffAt[n] || 0) > c.buffEvery) { G.bQueue.push(n); G.buffAt[n] = now; }
      if (!G.bQueue.length) return false;
      const n = G.bQueue[0];
      if (now - G.bAt < 1200) return true;
      if (G.bTries >= c.buffTries) { ev('buff ' + n + ': no cast seen after ' + G.bTries + ' tries -> skipped'); G.bQueue.shift(); G.bTries = 0; return false; }
      G.bTries++; G.bAt = now;
      try {
        if (s.currentSkill !== n) s.selectSkill(n);
        const me = P(h);
        w.castRequest = {target: h, point: {x: me.x, y: me.y}, forced: false, pvp: false};
      } catch (x) { ev('buff ' + n + ' error: ' + String(x).slice(0, 100)); }
      return true;
    };
    G.tick = (why) => {
      if (!G.on) return;
      const w = W.__world, h = w && w.playerEntity; if (!h) return;
      const s = W.__store, pd = (s && s.playerData) || {};
      const c = G.cfg, rx = new RegExp(c.re, 'i'), p = h.transform.pos, now = Date.now();
      G.myId = h.netId;
      const hp = pd.currentHP || 0, maxHp = pd.maxHP || 0;
      if (h.dying || hp <= 0) { G.tgt = null; return; }
      // potions
      if (c.fight && maxHp && hp < c.potHp * maxHp && now - G.lastPot >= c.potMs) {
        G.lastPot = now;
        try { s.useItemHotkey(c.potKey); G.pots++; } catch (x) {}
      }
      let best = null, statue = null, gate = null;
      const mobs = [];
      for (const e of w.netObjsQuery.entities) {
        if (!e.transform || e.localPlayer) continue;
        const d = Math.hypot(e.transform.pos.x - p.x, e.transform.pos.z - p.z);
        if (e.droppedItem) {
          if (!c.weapon || e.droppedItem.isMoney) continue;
          const it = e.droppedItem;
          if (!((it.group === c.g && it.num === c.n) || rx.test((e.objectNameInWorld || '').trim()))) continue;
          if (d <= c.radius && (!best || d < best.d)) best = {e, d};
          continue;
        }
        if (e.npcType === undefined || e.dying) continue;
        if (c.statueTypes.includes(e.npcType)) {
          if (c.statue && (!c.stand || Math.hypot(p.x - c.stand[0], p.z - c.stand[1]) <= c.standR)
              && d <= c.statueRadius && (!statue || d < statue.d)) statue = {e, d};
          continue;
        }
        if ((c.npcTypes || []).includes(e.npcType) || !e.monsterAnimation) continue;
        if ((c.gateTypes || []).includes(e.npcType)) { if (!gate || d < gate.d) gate = {e, d}; continue; }
        mobs.push({e, d, x: e.transform.pos.x, y: e.transform.pos.z});
      }
      if (best) {
        stopStatue(null, now, 'down (weapon on the ground)');
        if (G.id !== best.e.netId) {
          G.id = best.e.netId; G.seenAt = now; G.picks = 0; G.direct = 0; G.fails = 0; G.firstPickAt = 0;
          ev(`weapon seen ${best.d.toFixed(1)} tiles away (via ${why || 'poll'}; me @(${Math.round(p.x)},${Math.round(p.z)}), ` +
             `weapon @(${Math.round(best.e.transform.pos.x)},${Math.round(best.e.transform.pos.z)})) -> grabbing`);
        }
        w.attackTarget = null; w.castRequest = null; G.tgt = null;
        if (now - G.lastPick >= c.pickMs) pick(w, best.e, now);
        return;
      }
      if (G.id !== null) {
        ev(`weapon left the ground ${now - G.seenAt} ms after we saw it ` +
           `(first pickup after ${G.firstPickAt ? G.firstPickAt - G.seenAt : '-'} ms, ` +
           `${G.picks} pickups / ${G.direct} direct, ${G.fails} refused)`);
        G.id = null;
      }
      if (statue) {
        if (G.statueId !== statue.e.netId) {
          G.statueId = statue.e.netId; G.statueAt = now; G.casts = 0;
          ev(`Statue of Saint seen ${Math.round(statue.d)} tiles away -> attacking it (no helper)`);
        }
        w.pickupTarget = null;
        if (c.fight) { classify(s, c); try { const sk = pickSkill(now, s, c); if (sk && s.currentSkill !== sk) s.selectSkill(sk); } catch (x) {} }
        w.attackTarget = statue.e;
        if (!w.castRequest && now - G.lastCast >= c.castMs) {
          w.castRequest = {target: statue.e, point: {x: statue.e.transform.pos.x, y: statue.e.transform.pos.z},
                           forced: false, pvp: false};
          G.lastCast = now; G.casts++;
        }
        return;
      }
      stopStatue(w, now, 'gone');
      if (!c.fight) return;
      classify(s, c);
      // standing on a safe-zone tile (castle start area): the client refuses every cast there, so
      // fighting from it never works (2026-10-12, invalido stood 2.5 min at (13,14) casting 830x, 0 seen)
      // -> no buffs / no fighting until we're out, just walk
      const meSafe = !!(w.getTerrainFlag && (w.getTerrainFlag(Math.round(p.x), Math.round(p.z)) & 1) === 1);
      // casts going out but none seen by the server for a while -> walk a bit instead (any other reason)
      if (G.myCasts !== G.ncSeen || G.tgt === null || !G.ncAt) { G.ncSeen = G.myCasts; G.ncAt = now; }
      const noCast = now - G.ncAt > 3000;
      if (noCast && !G.walkUntil) { G.walkUntil = now + 2000; ev('casts not going through for 3 s -> walking for 2 s'); }
      if (G.walkUntil && now > G.walkUntil) { G.walkUntil = 0; G.ncAt = now; }
      const noFight = meSafe || (G.walkUntil && now <= G.walkUntil);
      if (meSafe !== !!G.wasSafe) { G.wasSafe = meSafe; if (meSafe) ev('in a safe zone: no fighting, walking out'); }
      // castle state straight from the packet hook (fresher than Python's tick)
      const S = W.__bcState, fresh = !!(S && S.t >= (c.stateSince || 0));
      const killPhase = fresh ? (S.st === 0 || S.st === 1) : !!c.killPhase;
      // ── v7: waiting by the bridge: start the moment the castle state says it started (phase 0 =
      //    the barrier opens; we used to wait for phase 1, ~2 s later, + a Python tick) ──
      let goal = c.goal, mode = c.mode, goalR = c.goalR;
      if (mode === 'wait') {
        if (fresh && [0, 1, 4].includes(S.st) && c.startGoal) {
          if (!G.selfStart) { G.selfStart = true; ev(`castle started (state ${S.st}) -> going in right away`); }
          mode = c.startMode || 'advance'; goal = c.startGoal; goalR = c.startR || 2;
        } else {
          G.selfStart = false; G.why = 'waiting for the start'; G.mode = 'wait';
          if (G.tgt !== null) { G.tgt = null; w.castRequest = null; w.attackTarget = null; }
          return;
        }
      }
      // buffs: never while just walking up the corridor (they cost 1-4 s at the start); only with a
      // monster close, or when guarding / clearing
      const nearMob = mobs.some(m => m.d <= (G.range || 3) + 2);
      if (!noFight && mode !== 'hold' && (mode !== 'advance' || nearMob) && buffTick(now, w, s, h)) return;
      // ── fight / walk ──
      const dGoal = goal ? Math.hypot(p.x - goal[0], p.z - goal[1]) : 0;
      const atGoal = !goal || dGoal <= goalR;
      // v7 kill phase at the bridge: the gate can't be hit until enough monsters are dead (the bridge
      // drops at the second phase) -> clear monsters, never further than zoneR from the bridge edge
      const zone = c.zone, zoneR = c.zoneR || 8;
      const dZone = zone ? Math.hypot(p.x - zone[0], p.z - zone[1]) : 1e9;
      const inZone = killPhase && zone && mode === 'advance' && dZone <= zoneR + 1;
      if (inZone !== !!G.wasZone) { G.wasZone = inZone; ev(inZone ? `kill phase at the bridge: clearing monsters within ${zoneR} tiles of (${zone})`
                                                           : 'left the bridge kill zone'); }
      const key = goal ? goal.join(',') + mode : null;
      if (key !== G.goalKey) { G.goalKey = key; G.best = dGoal; G.bestAt = now; }
      if (atGoal || dGoal < G.best - 0.5) { G.best = dGoal; G.bestAt = now; }
      const stuckMs = now - G.bestAt, stuck = !atGoal && stuckMs > c.stuckMs;
      let ux = 0, uy = 0;
      if (goal && dGoal > 0.01) { ux = (goal[0] - p.x) / dGoal; uy = (goal[1] - p.z) / dGoal; }
      const ahead = m => {
        if (!goal || atGoal) return true;
        const vx = m.x - p.x, vy = m.y - p.z, along = vx * ux + vy * uy, lat = Math.abs(vx * uy - vy * ux);
        return along >= -0.5 && lat <= c.coneHalf;
      };
      const R = G.range;
      mobs.sort((a, b) => a.d - b.d);
      let tgt = null, reason = '';
      let chase = null;
      if (inZone && !noFight) {
        const inR = mobs.filter(m => m.d <= R && Math.hypot(m.x - zone[0], m.y - zone[1]) <= zoneR + R);
        tgt = inR[0]; reason = 'kill phase: in range';
        if (!tgt) chase = mobs.find(m => Math.hypot(m.x - zone[0], m.y - zone[1]) <= zoneR + R) || null;
      } else if (mode !== 'hold' && !noFight) {
        const inR = mobs.filter(m => m.d <= R);
        if (mode === 'guard' || atGoal) { tgt = inR[0]; reason = 'in range at the spot'; }
        else if (mode === 'rush') {
          tgt = inR.find(m => m.d <= c.rushRange && ahead(m)); reason = 'in the way (rushing)';
          if (!tgt && stuck) { tgt = inR[0]; reason = 'stuck: in range'; }
        } else {
          tgt = inR.find(ahead); reason = 'in the way';
          if (!tgt && stuck) { tgt = inR[0]; reason = 'stuck: in range'; }
        }
        if (!tgt && gate && !killPhase && gate.d <= Math.max(R, 3) && (ahead({x: gate.e.transform.pos.x, y: gate.e.transform.pos.z}) || stuck)) {
          tgt = gate; reason = 'castle gate';
        }
        if (!tgt && stuck && mode !== 'rush' && stuckMs > c.huntMs) {
          tgt = mobs.find(m => m.d <= c.leash); reason = 'stuck: going for the nearest';
        }
      }
      if (reason !== G.lastWhy && tgt) { G.lastWhy = reason; ev(`target: ${(tgt.e.objectNameInWorld || '').trim() || tgt.e.npcType} (${reason}, ${tgt.d.toFixed(1)} tiles)`); }
      if (!tgt && G.lastWhy) G.lastWhy = '';
      G.why = tgt ? reason : (meSafe ? 'safe zone: walking out' : noFight ? 'casts not going through: walking'
              : atGoal ? 'at the goal' : (stuck ? 'stuck, nothing in range' : 'walking'));
      G.tgtName = tgt ? ((tgt.e.objectNameInWorld || '').trim() || String(tgt.e.npcType)) : null;
      G.stuck = stuck; G.dGoal = dGoal; G.inRange = mobs.filter(m => m.d <= R).length; G.mode = mode;
      G.hp = hp; G.maxHp = maxHp;
      if (tgt) {
        G.tgt = tgt.e.netId;
        w.pickupTarget = null;
        const sk = pickSkill(now, s, c); G.skill = sk || (s && s.currentSkill);
        if (cast(w, s, tgt.e, now, sk)) G.fightCasts++;
        return;
      }
      if (G.tgt !== null) { G.tgt = null; w.castRequest = null; w.attackTarget = null; G.lastWalk = 0; }
      if (inZone) {
        // nothing in range: step towards the nearest monster near the bridge (stopping R-1 tiles short),
        // clamped to zoneR tiles from the bridge edge; no monsters -> back to the edge
        if (now - G.lastWalk < c.walkMs) return;
        let tx = zone[0], ty = zone[1];
        if (chase) {
          const k = Math.max(chase.d, 0.01), stop = Math.max(R - 1, 1);
          tx = chase.x - (chase.x - p.x) / k * Math.min(stop, chase.d);
          ty = chase.y - (chase.y - p.z) / k * Math.min(stop, chase.d);
          const vx = tx - zone[0], vy = ty - zone[1], L = Math.hypot(vx, vy);
          if (L > zoneR) { tx = zone[0] + vx / L * zoneR; ty = zone[1] + vy / L * zoneR; }
          G.why = `kill phase: closing in on ${(chase.e.objectNameInWorld || '').trim() || chase.e.npcType}`;
        } else {
          G.why = 'kill phase: nothing near the bridge, waiting at the edge';
        }
        if (Math.hypot(tx - p.x, ty - p.z) < 1.5) return;
        G.lastWalk = now;
        for (const [ox, oy] of OFFS) if (walkTo(w, h, Math.round(tx) + ox, Math.round(ty) + oy)) { G.walks++; break; }
        return;
      }
      if (atGoal || now - G.lastWalk < c.walkMs) return;
      G.lastWalk = now;
      // stuck with nothing to hit: sidestep (try the tiles around the goal in turn)
      const offs = stuck ? OFFS.slice(1 + (G.walks % (OFFS.length - 1))).concat(OFFS) : OFFS;
      for (const [ox, oy] of offs) if (walkTo(w, h, Math.round(goal[0]) + ox, Math.round(goal[1]) + oy)) { G.walks++; break; }
    };
    G.timer = setInterval(() => { try { G.tick(); } catch (e) { G.err = String(e).slice(0, 200); } }, cfg.loopMs);
  }
  G.cfg = cfg;
  if (G.statueId !== null && (!cfg.on || !cfg.statue)) G.stopStatue(W.__world, Date.now(), 'left (statue off)');
  if (G.on !== cfg.on) {
    G.on = cfg.on;
    if (!cfg.on) {
      const w = W.__world;
      if (w) { if (G.id !== null) w.pickupTarget = null; if (G.tgt !== null) { w.castRequest = null; w.attackTarget = null; } }
      G.id = null; G.tgt = null; G.goalKey = null; G.bQueue = [];
    }
  }
  return {on: G.on, chasing: G.id !== null, id: G.id, picks: G.picks, fails: G.fails,
          statue: G.statueId !== null, statueMs: G.statueId !== null ? Date.now() - G.statueAt : 0,
          fight: {mode: G.mode, kind: G.kind, range: G.range, skill: G.skill, target: G.tgtName, why: G.why,
                  stuck: !!G.stuck, dGoal: G.dGoal, inRange: G.inRange, casts: G.fightCasts, seen: G.myCasts,
                  pots: G.pots, buffs: G.buffs, hp: G.hp, maxHp: G.maxHp},
          events: G.events.splice(0), err: G.err};
}
"""

PICKUP_JS = """
(id) => {
  const w = window.__world;
  const e = w.netObjsQuery.entities.find(e => e.netId === id && e.droppedItem);
  if (!e) return false;
  w.pickupTarget = e;
  return true;
}
"""

PARTY_KICK_JS = """
(name) => {
  const s = window.__social, m = (s.partyMembers || []).find(m => m.name === name);
  if (!m) return false;
  s.partyKick(m.index);
  return true;
}
"""

BUY_JS = """
([g, n, l]) => {
  const shop = window.__store.npcShop;
  if (!shop || !shop.items.some(Boolean)) return null;
  const i = shop.items.findIndex(it => it && it.group === g && it.num === n && it.lvl === l);
  if (i >= 0) window.__store.buyItemFromNpc(i);
  return i;
}
"""


KUNDUN_SLOTS_JS = """
(minLvl) => (window.__store.playerData.items || []).map((it, i) =>
    (i >= 12 && it && it.group === 14 && it.num === 11 && it.lvl >= minLvl) ? i : -1).filter(i => i >= 0)
"""

# free bag cells for 1x1 items (boxes). Bag = slots 12..75 (8 columns x 8 rows). An item sits in its
# top-left slot; its size comes from the item if the client gives one, otherwise 1x1 is assumed,
# so this can over-count (never under-count). keys = the first item's field names (to learn sizes).
BAG_FREE_JS = """
() => {
  const items = window.__store.playerData.items || [], used = new Array(64).fill(false);
  let keys = null, sized = 0;
  const num = v => (typeof v === 'number' && v >= 1 && v <= 4) ? v : null;
  for (let i = 12; i < 76; i++) {
    const it = items[i]; if (!it) continue;
    if (!keys) keys = Object.keys(it).join(',');
    const d = it.def || it.info || it.attr || {};
    const w = num(it.width) || num(it.w) || num(d.width) || num(d.w) || 1;
    const h = num(it.height) || num(it.h) || num(d.height) || num(d.h) || 1;
    if (w > 1 || h > 1) sized++;
    const c0 = (i - 12) % 8, r0 = Math.floor((i - 12) / 8);
    for (let r = r0; r < Math.min(8, r0 + h); r++) for (let c = c0; c < Math.min(8, c0 + w); c++) used[r * 8 + c] = true;
  }
  return {free: used.filter(u => !u).length, sized, keys};
}
"""

TRADE_STATE_JS = """
() => { const e = window.__eco; const n = a => Object.values(a || {}).filter(Boolean).length;
  return {open: !!e.tradeOpen, req: !!e.tradeRequest, mine: n(e.myTradeItems), yours: n(e.yourTradeItems),
          myOk: !!e.myTradeConfirm, yourOk: !!e.yourTradeConfirm,
          busy: !!(window.__store.pickedItem || window.__store.pendingItemMove)}; }
"""

TRADE_REQUEST_JS = """
(name) => {
  const e = window.__world.netObjsQuery.entities.find(e => e.objectNameInWorld === name && !e.localPlayer);
  if (!e) return 'not in view';
  window.__eco.requestTrade(e);
  return 'requested';
}
"""

# The Spirit only opens the "Messenger of Archangel" dialog; entering needs a click on the
# castle row. Real DOM (seen 2026-10-09):
#   div.mu-item-window.v097d-window.v097d-event.v097d-event-castle
#     div.v097d-event-row[role=button][data-row=0..6]  (+ class "disabled" when the
#         character's level is outside that castle's range; the cloak doesn't matter)
#       div.v097d-event-label  "Castle 2 (level 81-130)"
# The whole row (354x83) is the button (React onClick on the row).
ENTER_BUTTON_JS = """
(tier) => {
  const root = document.querySelector('.v097d-event-castle');
  if (!root) return {dialog: false};
  const re = new RegExp('Castle\\\\s*' + tier + '\\\\b');
  const rows = [...root.querySelectorAll('.v097d-event-row')];
  const row = rows.find(r => re.test((r.querySelector('.v097d-event-label') || r).textContent || ''))
           || rows[tier - 1];
  if (!row) return {dialog: true, text: (root.textContent || '').slice(0, 200)};
  const r = row.getBoundingClientRect();
  return {dialog: true, x: r.left + r.width / 2, y: r.top + r.height / 2,
          label: (row.textContent || '').trim(), disabled: row.classList.contains('disabled')};
}
"""

# fallback when the mouse click lands on something else: fire the row's click directly
CLICK_ROW_JS = """
(tier) => {
  const rows = [...document.querySelectorAll('.v097d-event-castle .v097d-event-row')];
  const re = new RegExp('Castle\\\\s*' + tier + '\\\\b');
  const row = rows.find(r => re.test(r.textContent || '')) || rows[tier - 1];
  if (!row) return false;
  row.click();
  return true;
}
"""

INVITE_BY_NAME_JS = """(name) => {
  const e = window.__world.netObjsQuery.entities.find(e => e.objectNameInWorld === name && !e.localPlayer);
  if (!e) return 'not in view';
  window.__social.partyInvite({netId: e.netId, name}); return 'invited'; }"""


def tier_of(level):
    for t, lo, hi in BC_TIERS:
        if lo <= (level or 0) <= hi:
            return t
    return None


def dist(s, x, y):
    return math.hypot((s.get("x") or 0) - x, (s.get("y") or 0) - y)


class BloodCastle:
    """Owned by Group; tick() returns True while the detour is running (normal loop paused)."""

    def __init__(self, group):
        self.g = group
        self.active = None          # dict while running
        self.done_key = None        # entry window already handled (or skipped)

    # ── helpers on a character ──
    @staticmethod
    def ev(c, js, arg=None):
        try:
            return c.page.evaluate(js, arg) if arg is not None else c.page.evaluate(js)
        except Exception:
            return None

    def go(self, c, map_idx, alias, x, y, radius=2):
        """Warp/walk towards (map, x, y). Returns True once there."""
        s = c.s
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
            c.log("stuck walking -> re-warping")
            c.chat(f"/move {alias}")
            c.last_warp, c.walk_target = time.time(), None
            return False
        self.ev(c, WALK_JS, [x, y])
        return False

    def inv(self, c, item):
        g, n, l = (item + (None,))[:3]
        return self.ev(c, INV_COUNT_JS, [g, n, l]) or 0

    def cloak_count(self, c, tier):
        """Cloaks of this castle level in the bag; None if the page could not be read
        (then we don't send the character shopping by mistake)."""
        try:
            return c.page.evaluate(INV_COUNT_JS, [CLOAK[0], CLOAK[1], tier]) or 0
        except Exception:
            return None

    def buy_cloak(self, c, tier):
        """At the bar: buy one cloak of this castle level. True when we have one."""
        item = (CLOAK[0], CLOAK[1], tier)
        before = self.inv(c, item)
        if before:
            return True
        found = self.ev(c, """(name) => {
          const e = window.__world.netObjsQuery.entities.find(e =>
              e.npcType !== undefined && (e.objectNameInWorld || '').trim() === name);
          if (!e) return false;
          window.__store.talkToNpc({netId: e.netId, name: e.objectNameInWorld, npcType: e.npcType});
          return true; }""", BAR_NPC)
        if not found:
            c.log(f"BC: {BAR_NPC} not found")
            return False
        slot = None
        for _ in range(10):
            time.sleep(0.5)
            slot = self.ev(c, BUY_JS, list(item))
            if slot is not None:
                break
        ok = False
        if slot is not None and slot >= 0:
            for _ in range(10):
                time.sleep(0.5)
                if self.inv(c, item) > before:
                    ok = True
                    break
        self.ev(c, "() => { window.__store.closeNpcShop(); window.__store.inventoryEnabled = false; }")
        c.log(f"BC: cloak +{tier} {'bought' if ok else 'NOT bought (slot %s)' % slot}")
        return ok

    def scan(self, c, item=(0, 0, None), r=CLEAR_RADIUS):
        ours = R.friends([m.name for m in self.g.members])   # + our characters on other machines
        g, n, l = (item + (None,))[:3]
        return self.ev(c, SCAN_JS, [ours, r, g, n, l]) or {"players": [], "monsters": 0, "items": []}

    # ── timing ──
    def window_info(self):
        """(seconds until the entry window opens, seconds since it opened) — one of them is None."""
        t = self.ev(self.g.leader, BC_TIMER_JS)
        if not t:
            return None, None
        if t.get("open"):
            return None, 0
        secs = t["secs"]
        since = 3600 - secs
        if since <= BC_DOOR_CLOSE_SEC:   # timer already counts to the next hour: window is open
            return None, since
        return secs, None

    # ── main ──
    def tick(self):
        if not BC_ENABLED:
            return False
        for c in self.g.members:
            self.ev(c, BC_HOOK_JS)
        if self.active is None:
            return self.maybe_start()
        try:
            return self.run()
        except Exception as e:
            self.g_log(f"BC error: {e!r}")
            return True

    def g_log(self, msg):
        self.g.leader.log(msg)

    def maybe_start(self):
        g, lead = self.g, self.g.leader
        if g.reset_round or lead.shop_pending:
            return False
        tier = tier_of(lead.s.get("level"))
        if tier not in BC_TARGET_TIERS:
            return False
        until, since = self.window_info()
        now = time.time()
        if until is not None and until <= BC_PREP_SEC:
            open_at = now + until
        elif since is not None and since <= BC_DOOR_CLOSE_SEC - BC_ENTRY_NEEDS_SEC:
            open_at = now - since
        else:
            return False
        key = round(open_at / 600)
        if key == self.done_key:
            return False
        self.done_key = key
        parts = {c.name for c in g.members if tier_of(c.s.get("level")) == tier}
        self.active = {
            "tier": tier, "open_at": open_at, "parts": parts, "kicked": set(), "invited": {},
            "party_done": False, "end_at": None, "rush": False, "left": set(),
        }
        for c in g.members:
            self.ev(c, "() => { window.__bcState = null; }")
            c.bc = {"burst_until": 0, "tried": set(),
                    "box": False, "box_try": None, "talk_at": 0, "entered": False, "cloak": False}
        g.leader.log(f"BLOOD CASTLE {tier}: window opens in {max(0, int(open_at - now))}s, "
                     f"going in: {sorted(parts)}; waiting in Lorencia: "
                     f"{sorted(c.name for c in g.members if c.name not in parts) or '-'}")
        return True

    def finish(self, why):
        self.g_log(f"BLOOD CASTLE finished ({why}) -> back to leveling")
        for c in self.g.members:
            c.bc = None
            c.walk_target = None
            c.spot = None
        self.active = None

    def run(self):
        a, g, lead = self.active, self.g, self.g.leader
        now = time.time()
        since_open = now - a["open_at"]
        door = BC_DOOR_CLOSE_SEC
        lead_in = in_bc(lead.s.get("map"))
        st = self.ev(lead, BC_STATE_JS) if lead_in else None
        if st and st.get("t", 0) < a["open_at"] * 1000 - 60_000:
            st = None   # left over from a previous castle

        # leader did not make it in: give up
        if since_open > door + 20 and not lead.bc["entered"] and a["end_at"] is None:
            self.cleanup_party()
            self.finish("wizzardo could not enter")
            return False

        # castle over: BloodCastleState 2, or everyone thrown out after it started
        if a["end_at"] is None and ((st and st.get("st") == 2) or
                                    (lead.bc["entered"] and not lead_in and since_open > door)):
            a["end_at"] = now
            self.g_log("BC: castle finished -> picking up boxes")
        if a["end_at"] is not None:
            return self.end_phase(now - a["end_at"])

        if lead_in and since_open < door:
            self.party_management(since_open)

        # who has the Archangel's weapon? one of ours (checked in every bag, every tick) or a stranger
        holder = st.get("own") if st else 255
        ours_wpn = next((m for m in g.members
                         if in_bc(m.s.get("map")) and self.inv(m, WEAPON) > 0), None)
        a["holder"] = ours_wpn.name if ours_wpn else None
        if ours_wpn and a.get("ours_logged") != ours_wpn.name:
            a["ours_logged"] = ours_wpn.name
            self.g_log(f"BC: {ours_wpn.name} has the weapon -> delivering, everyone to the Archangel")
        if holder not in (None, 255) and not ours_wpn and not a["rush"]:
            a["rush"] = True
            self.g_log("BC: another player has the weapon -> EVERYONE back to the Archangel now")
        lead_has_weapon = ours_wpn is lead

        for c in g.members:
            if (c.s.get("hp") or 0) <= 0:
                continue
            if c.name not in a["parts"]:
                self.go(c, *self.bar_target())
                continue
            if in_bc(c.s.get("map")):
                if not c.bc["entered"]:
                    c.log(f"BC: inside the castle (map {c.s.get('map')})")
                c.bc["entered"] = True
                if c is lead:
                    self.leader_inside(c, st, lead_has_weapon)
                else:
                    self.alt_inside(c)
                self.accept_invites(c)
                continue
            if c.bc["entered"] or since_open > door:
                # thrown out / missed the door -> wait at the bar
                self.go(c, *self.bar_target())
                continue
            self.prepare_and_enter(c, since_open)
        return True

    def bar_target(self):
        m, x, y = BAR_SPOT
        return m, "Lorencia", x, y

    def prepare_and_enter(self, c, since_open):
        a, b = self.active, c.bc
        if not b["cloak"]:
            if not self.go(c, *self.bar_target()):
                return
            if self.buy_cloak(c, a["tier"]):
                b["cloak"] = True
            else:
                b["fails"] = b.get("fails", 0) + 1
                if b["fails"] >= 3:
                    c.log("BC: no cloak -> staying in Lorencia")
                    a["parts"].discard(c.name)
            return
        # never walk to / talk to the Spirit without the cloak (it is consumed on entry, so a
        # character thrown out of the castle has none left)
        have = self.cloak_count(c, a["tier"])
        if have == 0 and time.time() - b["talk_at"] < 10:
            return   # just talked: the cloak was likely consumed and the warp into the castle is loading
        if have == 0:
            left = BC_DOOR_CLOSE_SEC - since_open
            b["rebuys"] = b.get("rebuys", 0) + 1
            if left >= BC_REBUY_NEEDS_SEC and b["rebuys"] <= BC_MAX_REBUYS:
                c.log(f"BC: no cloak +{a['tier']} in the bag -> back to the bar for another "
                      f"({int(left)}s left before the door closes)")
                b["cloak"] = False
            else:
                c.log(f"BC: no cloak +{a['tier']} and no time / too many tries -> skipping this castle")
                a["parts"].discard(c.name)
            return
        m, x, y = SPIRIT_SPOT
        if not self.go(c, m, "Devias", x, y, radius=1):
            return
        if since_open < 0:
            return   # wait next to the Spirit until the window opens
        if time.time() - b["talk_at"] > 5:
            b["talk_at"] = time.time()
            btn = self.ev(c, ENTER_BUTTON_JS, a["tier"]) or {}
            if not btn.get("dialog"):
                c.close_windows()
                c.log("BC: talking to Spirit of Archangel")
                self.ev(c, TALK_NPC_JS, SPIRIT_NPC)
                for _ in range(8):       # wait for the Messenger of Archangel dialog
                    time.sleep(0.4)
                    btn = self.ev(c, ENTER_BUTTON_JS, a["tier"]) or {}
                    if btn.get("dialog"):
                        break
            if not btn.get("dialog"):
                c.log("BC: entry dialog did not open")
                return
            if "x" not in btn:
                c.log(f"BC: castle {a['tier']} button not found in dialog: {btn.get('text')!r}")
                return
            if btn.get("disabled"):
                c.log(f"BC: '{btn.get('label')}' is greyed out (level out of range) -> skipping this castle")
                c.close_windows()
                a["parts"].discard(c.name)
                return
            c.log(f"BC: clicking '{btn.get('label')}'")
            try:
                c.page.mouse.click(btn["x"], btn["y"])
            except Exception as e:
                c.log(f"BC: mouse click failed {e!r} -> clicking the row from JS")
                self.ev(c, CLICK_ROW_JS, a["tier"])
            # errors (cloak level wrong / not open / level too high) show as game notifications
            time.sleep(1.5)
            note = self.ev(c, "() => (window.__store.notifications || []).slice(-2).map(n => n.text || n.message || JSON.stringify(n))")
            if note:
                c.log(f"BC: game says {note}")

    def accept_invites(self, c):
        """Inside the castle: accept party invites from our own characters only.
        wizzardo must stay party leader (strangers are invited INTO his party), so a stranger's
        invite is declined - accepting one made him a member who can't invite/kick anymore."""
        if not c.s.get("partyRequest"):
            return
        text = c.s.get("partyRequestText", "")
        ours = [m.name for m in self.g.members if m is not c]
        if c is not self.g.leader and any(f'"{n}"' in text for n in ours):
            c.log(f"BC: accepting party invite {text[:60]}")
            self.ev(c, "() => window.__social.partyRespond(true)")
        else:
            c.log(f"BC: declining party invite {text[:60]}")
            self.ev(c, "() => window.__social.partyRespond(false)")

    def walk_in_castle(self, c, x, y, radius=2):
        """Walk inside the castle. Monsters block the corridor, so when there is no progress for
        STUCK_SEC the helper fights for HELPER_BURST seconds, then walking resumes.
        Returns True once within radius."""
        b, s = c.bc, c.s
        now = time.time()
        d = dist(s, x, y)
        if d <= radius:
            if s.get("helper") and now >= b["burst_until"]:
                c.set_helper(False)
            c.walk_target = None
            return True
        if now < b["burst_until"]:
            c.set_helper(True)                       # fighting whatever blocks the way
            return False
        if s.get("helper"):
            c.set_helper(False)                      # burst over: walk again
            c.walk_target = None
        if c.walk_target != (x, y):
            c.walk_target, c.walk_best, c.walk_progress_at = (x, y), d, now
        if d < c.walk_best - 0.5:
            c.walk_best, c.walk_progress_at = d, now
        elif now - c.walk_progress_at > STUCK_SEC:
            b["stuck_n"] = b.get("stuck_n", 0) + 1
            if b["stuck_n"] % 5 == 1:
                c.log(f"BC: stuck at ({s.get('x')},{s.get('y')}), {d:.0f} tiles from ({x},{y}) -> helper burst")
            b["burst_until"] = now + HELPER_BURST
            c.walk_target = None
            c.set_helper(True)
            return False
        if not self.ev(c, WALK_JS, [int(x), int(y)]):
            # target tile not walkable: use the nearest walkable tile around it
            for ox, oy in ((1, 0), (-1, 0), (0, -1), (1, -1), (-1, -1), (2, 0), (0, -2), (2, -2),
                           (0, 1), (1, 1), (-1, 1)):
                if self.ev(c, WALK_JS, [int(x) + ox, int(y) + oy]):
                    break
        return False

    def deliver(self, c):
        """c has the weapon: walk to the Archangel and talk to him."""
        b = c.bc
        if not self.walk_in_castle(c, *ARCHANGEL, radius=2):
            return
        if time.time() - b["talk_at"] > 3:
            b["talk_at"] = time.time()
            c.set_helper(False)
            c.log("BC: delivering the weapon to the Archangel")
            self.ev(c, TALK_NPC_JS, ARCHANGEL_NPC)

    def to_archangel_mode(self):
        """Everyone heads to the Archangel: a stranger has the weapon, or one of ours is delivering it."""
        a = self.active
        return a["rush"] or a.get("holder") is not None

    def party_management(self, since_open):
        a, lead = self.active, self.g.leader
        now = time.time()
        party = lead.s.get("party") or []
        ours_in = [c for c in self.g.members if c is not lead and c.name in a["parts"]
                   and in_bc(c.s.get("map"))]
        deadline = BC_DOOR_CLOSE_SEC - BC_INVITE_MARGIN
        if not a["party_done"] and since_open >= deadline:
            a["party_done"] = True
            self.g_log(f"BC: stranger invites over, party now {party}")
        strangers = self.scan(lead)["players"]
        if not a["party_done"]:
            total = len(set(strangers))
            if 1 <= total <= BC_MAX_STRANGERS:
                want = [n for n in strangers if n not in party]
                free = 5 - max(1, len(party))
                kickable = [c for c in reversed(ours_in) if c.name in party and c.name not in a["kicked"]]
                while free < len(want) and kickable:
                    c = kickable.pop(0)
                    self.g_log(f"BC: {total} other player(s) -> kicking {c.name} to make room")
                    self.ev(lead, PARTY_KICK_JS, c.name)
                    a["kicked"].add(c.name)
                    free += 1
                for n in want:
                    if len(party) < 5 and now - a["invited"].get(n, 0) > 10:
                        a["invited"][n] = now
                        res = self.ev(lead, INVITE_BY_NAME_JS, n)
                        self.g_log(f"BC: party invite {n}: {res}")
                return
        # our own characters: invite those not in a party (kicked ones only after the deadline)
        for c in ours_in:
            if c.name in party or c.s.get("party"):
                continue
            if c.name in a["kicked"] and not a["party_done"]:
                continue
            if len(party) < 5 and now - a["invited"].get(c.name, 0) > 8:
                a["invited"][c.name] = now
                res = self.ev(lead, INVITE_BY_NAME_JS, c.name)
                self.g_log(f"BC: party invite {c.name}: {res}")

    def leader_inside(self, c, st, has_weapon):
        b = c.bc
        now = time.time()
        if has_weapon:
            return self.deliver(c)
        if self.to_archangel_mode():
            # a stranger (or an alt) has the weapon: back to the Archangel immediately
            self.walk_in_castle(c, *ARCHANGEL, radius=2)
            return
        if not st or st.get("st") not in (1, 4):
            self.go(c, c.s.get("map"), "", *(self.ev(c, WAIT_SPOT_JS, [*BC_WAIT, *BC_START]) or BC_START), radius=2)   # waiting for the castle to start
            return
        weapon = self.scan(c, WEAPON, r=CLEAR_RADIUS)
        if weapon["items"]:
            if now - b["talk_at"] > 2:
                b["talk_at"] = now
                c.set_helper(False)
                c.log("BC: picking up the Archangel's weapon")
                self.ev(c, PICKUP_JS, weapon["items"][0]["id"])
            return
        # walk up the corridor (helper bursts when stuck); at the statue: helper on to destroy it
        tx, ty = STATUE
        if dist(c.s, tx, ty) <= STATUE_RADIUS:
            c.set_helper(True)
            return
        self.walk_in_castle(c, tx, ty, radius=STATUE_RADIUS)

    def alt_inside(self, c):
        """Alts follow wizzardo by POSITION (party or not, so nobody is left at the entrance);
        when the weapon is taken by anyone they go straight to the Archangel."""
        a, lead = self.active, self.g.leader
        if a.get("holder") == c.name:
            return self.deliver(c)
        if self.to_archangel_mode():
            self.walk_in_castle(c, *ARCHANGEL, radius=2)
            return
        if not in_bc(lead.s.get("map")) or (lead.s.get("hp") or 0) <= 0:
            c.set_helper(False)
            self.go(c, c.s.get("map"), "", *(self.ev(c, WAIT_SPOT_JS, [*BC_WAIT, *BC_START]) or BC_START), radius=2)    # wait in the castle
            return
        lx, ly = lead.s.get("x"), lead.s.get("y")
        if dist(c.s, lx, ly) > FOLLOW_DIST:
            self.walk_in_castle(c, lx, ly, radius=FOLLOW_DIST)
        elif c.s.get("helper") and time.time() >= c.bc["burst_until"]:
            c.set_helper(False)

    def end_phase(self, since_end):
        a, g = self.active, self.g
        busy = False
        for c in g.members:
            b = c.bc
            if not in_bc(c.s.get("map")) or b["box"]:
                continue
            if not self.go(c, c.s.get("map"), "", *ARCHANGEL, radius=4) and since_end < 20:
                busy = True
                continue
            now = time.time()
            if b["box_try"]:
                bid, t0, n0 = b["box_try"]
                if self.inv(c, BOX) > n0:
                    b["box"] = True
                    c.log("BC: got a Box of Kundun +5")
                    continue
                if now - t0 < BOX_TRY_SEC:
                    busy = True
                    continue
                b["tried"].add(bid)
                b["box_try"] = None
            boxes = [it for it in self.scan(c, BOX, r=30)["items"] if it["id"] not in b["tried"]]
            if boxes:
                b["box_try"] = (boxes[0]["id"], now, self.inv(c, BOX))
                self.ev(c, PICKUP_JS, boxes[0]["id"])
                busy = True
            elif since_end < 25:
                busy = True       # boxes may still be dropping
        if busy and since_end < END_WAIT_SEC:
            return True
        # leave: back to Lorencia, drop a stranger party
        if since_end < END_WAIT_SEC + 60:
            pending = False
            for c in g.members:
                if in_bc(c.s.get("map")):
                    pending = True
                    if time.time() - c.last_warp > 15:
                        c.chat("/move Lorencia")
                        c.last_warp = time.time()
            if pending:
                return True
        self.cleanup_party()
        if TRADE_BOXES and not a.get("traded"):
            if not self.trade_phase():
                return True
            a["traded"] = True
        got = [c.name for c in g.members if c.bc and c.bc["box"]]
        self.finish(f"boxes: {got or 'none'}")
        return False

    # ── give every Box of Kundun to wizzardo ──
    def trade_phase(self):
        """Gather at the Lorencia bar, then each alt trades its Kundun boxes to wizzardo.
        Returns True when finished."""
        a, lead = self.active, self.g.leader
        if "trade_started" not in a:
            a["trade_started"], a["trade_done"] = time.time(), set()
        if time.time() - a["trade_started"] > 300:
            self.g_log("BC: box trading took too long -> skipping the rest")
            return True
        m, x, y = TRADE_SPOT
        todo = [c for c in self.g.members if c is not lead and c.name not in a["trade_done"]]
        if not todo:
            return True
        lead_ready = self.go(lead, m, "Lorencia", x, y, radius=2)
        for c in todo:
            if (c.s.get("hp") or 0) <= 0:
                continue
            if not self.ev(c, KUNDUN_SLOTS_JS, KUNDUN_MIN_LVL):
                a["trade_done"].add(c.name)
                continue
            if not lead_ready:
                self.go(c, m, "Lorencia", x, y + 2, radius=2)
                continue
            if dist(c.s, lead.s.get("x"), lead.s.get("y")) > 3:
                self.go(c, m, "Lorencia", int(lead.s["x"]), int(lead.s["y"]) + 1, radius=2)
                continue
            ok = self.trade_boxes(c, lead)
            c.bc["trade_tries"] = c.bc.get("trade_tries", 0) + 1
            if ok or c.bc["trade_tries"] >= 3:
                a["trade_done"].add(c.name)
            return False   # one trade per tick
        return False

    def trade_boxes(self, c, lead):
        """Blocking: one trade window between an alt and wizzardo. True if the boxes moved."""
        slots = self.ev(c, KUNDUN_SLOTS_JS, KUNDUN_MIN_LVL) or []
        lead_before = len(self.ev(lead, KUNDUN_SLOTS_JS, KUNDUN_MIN_LVL) or [])
        c.log(f"BC: trading {len(slots)} Box(es) of Kundun to {lead.name}")
        for ch in (c, lead):
            ch.set_helper(False)
            ch.close_windows()
        state = lambda ch: self.ev(ch, TRADE_STATE_JS) or {}

        def wait(cond, secs):
            end = time.time() + secs
            while time.time() < end:
                if cond():
                    return True
                time.sleep(0.4)
            return False

        def abort(why):
            c.log(f"BC: trade failed ({why})")
            for ch in (c, lead):
                self.ev(ch, "() => { if (window.__eco.tradeOpen) window.__eco.cancelTrade(); "
                            "if (window.__eco.tradeRequest) window.__eco.answerTradeRequest(false); }")
            time.sleep(1)
            return False

        res = self.ev(c, TRADE_REQUEST_JS, lead.name)
        if res != "requested":
            return abort(f"request: {res}")
        if not wait(lambda: state(lead).get("req"), 8):
            return abort("wizzardo got no trade request")
        self.ev(lead, "() => window.__eco.answerTradeRequest(true)")
        if not wait(lambda: state(c).get("open") and state(lead).get("open"), 8):
            return abort("trade window did not open")
        for slot in slots:   # Inventory (0) -> my trade window (1)
            wait(lambda: not state(c).get("busy"), 3)
            self.ev(c, "(i) => window.__store.autoMoveItem(0, i, 1)", slot)
            time.sleep(0.6)
        if not wait(lambda: state(lead).get("yours", 0) >= len(slots), 10):
            return abort(f"only {state(lead).get('yours', 0)}/{len(slots)} boxes reached the trade window")
        time.sleep(1)
        self.ev(c, "() => window.__eco.setMyConfirm(true)")
        time.sleep(1)
        self.ev(lead, "() => window.__eco.setMyConfirm(true)")
        if not wait(lambda: not state(c).get("open") and not state(lead).get("open"), 12):
            return abort("trade did not complete")
        time.sleep(1)
        left = len(self.ev(c, KUNDUN_SLOTS_JS, KUNDUN_MIN_LVL) or [])
        lead_after = len(self.ev(lead, KUNDUN_SLOTS_JS, KUNDUN_MIN_LVL) or [])
        c.log(f"BC: trade done -> {c.name} has {left} box(es) left, {lead.name} {lead_before} -> {lead_after}")
        return left < len(slots)

    def cleanup_party(self):
        ours = {m.name for m in self.g.members}
        for c in self.g.members:
            party = c.s.get("party") or []
            if any(n not in ours for n in party):
                c.log(f"BC: leaving party {party}")
                self.ev(c, "() => window.__social.partyLeave()")

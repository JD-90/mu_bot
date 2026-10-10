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
ARCHANGEL     = (12, 12)            # stand here to deliver / wait for the boxes (Archangel ≈ 12,11)
ARCHANGEL_NPC = 232
WEAPON        = (13, 19)            # Archangel's weapon (staff / sword / crossbow)
BOX           = (14, 11, 12)        # Box of Kundun +5
STATUE        = (13, 93)            # walk towards the statue (weapon drops at ≈13,95)
STATUE_RADIUS = 3                   # this close -> helper on to destroy the statue
STUCK_SEC     = 2.0                 # no walking progress this long -> stuck
HELPER_BURST  = 3.0                 # helper on this long when stuck, then keep walking
CLEAR_RADIUS  = 7                   # scan radius for the weapon on the ground
FOLLOW_DIST   = 4
BOX_TRY_SEC   = 3.5
END_WAIT_SEC  = 90                  # after the castle ends: time to pick boxes before leaving
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
  return true;
}
"""

BC_STATE_JS = "() => window.__bcState"

INV_COUNT_JS = """
([g, n, l]) => (window.__store.playerData.items || []).filter((it, i) =>
    i >= 12 && it && it.group === g && it.num === n && (l === null || it.lvl === l)).length
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
        ours = [m.name for m in self.g.members]
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
            self.go(c, c.s.get("map"), "", *BC_START, radius=2)   # waiting for the castle to start
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
            self.go(c, c.s.get("map"), "", *BC_START, radius=2)    # wait in the castle
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

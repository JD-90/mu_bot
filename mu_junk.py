"""
Getting rid of junk (Juan 2026-10-15). Two parts, both per character, only while leveling:

1. DROP  every gear item in the bag that is neither excellent nor ancient is dropped on the ground
         at the character's feet: weapons, shields, helms, armors, pants, gloves, boots (item groups
         0-11) and rings / pendants (JUNK_JEWELRY). Never: anything equipped (slots 0-11), wings,
         jewels, boxes, potions, arrows / bolts, the Blood Castle quest weapon, special rings
         (Wizard, Transformation, event rings).
         One item every JUNK_DROP_GAP s; an item the server won't take is left alone for an hour.
         The loot filter's "High level items" is OFF now (mu_group_bot.GAME_OPTIONS), otherwise the
         MU Helper would pick a dropped +7 straight back up.

2. CRYSTALLIZE  after every Treasure Hunt, a character carrying excellent gear that is NOT end-game
         top gear (CRYST_KEEP = the server's "best gear of each class") takes it to the Alquimista
         (npcType 693, next to the Chaos Goblin in Noria) and crystallizes it ("Cristalizar", up to 8
         items and 500k zen per batch), then takes the Cristales back to the bag.
         Only weapons, shields and set parts (the Alquimista refuses wings, rings, pendants).
         Items at +CRYST_MAX_LVL+1 or higher are kept too (fusion material, see the guide).
         The trip waits while an event / trade / Blood Castle is on (mu_auto_bot.trade_quiet) and
         is forgotten CRYST_DUE_SEC after the hunt.

Client facts (bundle read 2026-10-15):
  * bag items: window.__store.playerData.items[12..75], each {group, num, lvl, isExcellent, isAncient}
  * drop: __store.pickInventoryItem(slot); __store.dropPickedItem(x, y)  (DropItemRequest C3 23,
    no confirm window: the confirm lives in the UI layer, not in the store)
  * Alquimista: talkToNpc(npcType 693) -> NpcWindowResponse ChaosMachine -> __eco.openMix("alchemist")
    (__eco.mixOpen, __eco.mixKind === "alchemist", __eco.mixItems = 8x4 tray). Bag -> tray:
    __store.autoMoveItem(0, slot, 3) (storages: Inventory 0, Trade 1, Vault 2, ChaosMachine 3).
    "Cristalizar" = __eco.mix(); success -> mixResult "success" and the tray holds the Cristales
    (item 14/31); refused -> mixRefusal / mixResult "failed". Tray -> bag: autoMoveItem(3, slot, 0).
    __eco.closeMix() refuses while something is on the tray.
"""
import time

import mu_bc as B
from mu_bc import dist

# ───────────────────────────── CONFIG ─────────────────────────────
JUNK_ENABLED     = True
JUNK_DROP        = True      # part 1 on/off
JUNK_DROP_EVERY  = 20        # seconds between bag checks while leveling
JUNK_DROP_GAP    = 1.5       # seconds between two drops
JUNK_DROP_RETRY  = 3600      # an item that didn't leave the bag after a drop: leave it alone this long
JUNK_GEAR_GROUPS = range(0, 12)          # swords, axes, maces, spears, bows, staffs, shields, helm..boots
JUNK_JEWELRY     = {(13, n) for n in (8, 9, 12, 13, 21, 22, 23, 24, 25, 26, 27, 28)}
                                         # Ring of Ice/Poison, Pendant of Lightning/Fire, Ring of Fire/Earth/
                                         # Wind/Magic, Pendant of Ice/Wind/Water/Ability. NOT: Wizard ring (13/20),
                                         # Transformation rings, event rings
JUNK_NEVER       = {(4, 7), (4, 15)} | set(B.WEAPON_CODES)   # bolts, arrows, Blood Castle quest weapon

CRYST_ENABLED    = True      # part 2 on/off
CRYST_SKIP_CHARS = {"wizzardo"}   # these characters never crystallize (Juan 2026-10-15: not the main)
CRYST_DUE_SEC    = 3600      # after a Treasure Hunt, try for this long (events / trades come first)
CRYST_RETRY_SEC  = 300       # failed trip -> try again after this long (while still due)
CRYST_TIMEOUT    = 300       # whole trip
CRYST_FEE        = 500_000   # zen per batch
CRYST_ZEN_KEEP   = 200_000   # don't go below this after paying
CRYST_BATCH      = 8         # items per batch (the Alquimista's limit)
CRYST_MAX_LVL    = 12        # +13 and up are kept (fusion material)
ALCHEMIST_NPC    = 693       # npcType of the Alquimista
ALCHEMIST_MAP    = (3, "Noria")
ALCHEMIST_NEAR   = (180, 102)    # the Chaos Goblin's Chaos Machine (guide); the Alquimista stands next to it
ALCHEMIST_TALK_R = 3         # walk this close to him before talking
CRYST_ITEM       = (14, 31)  # Cristal
# End-game top gear: never crystallized (Juan 2026-10-15: "the server's top gear" = what the Grand Reset
# shop sells as the best of each class)
CRYST_KEEP = (
    {(g, 21) for g in range(7, 12)}          # Great Dragon set (DK)
    | {(g, 22) for g in range(7, 12)}        # Dark Soul set (DW)
    | {(g, 24) for g in range(7, 12)}        # Red Spirit set (Elf)
    | {(g, 23) for g in range(8, 12)}        # Hurricane set (MG, no helm)
    | {(0, 20), (0, 21), (5, 11), (4, 18), (4, 19)}   # Knight Blade, Dark Reign Blade, Staff of Kundun,
                                                      # Divine Crossbow of Archangel, Great Reign Crossbow
    | {(6, 15), (6, 16)}                     # Grand Soul Shield, Elemental Shield
)
# ──────────────────────────────────────────────────────────────────

# bag slots by kind: junk to drop / excellent gear to crystallize
BAG_JS = """
([never, jewelry, keep, maxLvl, skip]) => {
  const s = window.__store; if (!s || !s.playerData) return null;
  const items = s.playerData.items || [], has = (l, it) => l.some(p => p[0] === it.group && p[1] === it.num);
  const junk = [], cryst = [];
  for (let i = 12; i < Math.min(items.length, 76); i++) {   // 76+ = personal shop
    const it = items[i]; if (!it || it.group === undefined) continue;
    const key = `${it.group}/${it.num}/${it.lvl || 0}/${it.isExcellent ? 1 : 0}`;
    const d = {slot: i, g: it.group, n: it.num, lvl: it.lvl || 0, key};
    if (has(never, it)) continue;
    const gear = it.group >= 0 && it.group <= 11, jew = has(jewelry, it);
    if (!gear && !jew) continue;
    if (!it.isExcellent && !it.isAncient) { if (!skip.includes(i + ':' + key)) junk.push(d); continue; }
    if (gear && it.isExcellent && !it.isAncient && !has(keep, it) && (it.lvl || 0) <= maxLvl) cryst.push(d);
  }
  return {junk, cryst, money: Number(s.playerData.money) || 0, picked: !!s.pickedItem, pending: !!s.pendingItemMove};
}
"""

DROP_JS = """
(slot) => {
  const s = window.__store, w = window.__world, h = w && w.playerEntity;
  if (!s || !h || s.pickedItem || s.pendingItemMove) return 'busy';
  if (!s.playerData.items[slot]) return 'gone';
  const x = Math.floor(h.transform.pos.x), y = Math.floor(h.transform.pos.z);
  s.pickInventoryItem(slot);
  if (!s.pickedItem) return 'pick failed';
  s.dropPickedItem(x, y);
  if (s.pickedItem && !s.pendingItemMove) { s.cancelPickedItem(); return 'not sent'; }
  return 'sent';
}
"""

MIX_STATE_JS = """
() => { const e = window.__eco, s = window.__store; if (!e || !s) return null;
  const tray = (e.mixItems || []).map((it, i) => it ? {slot: i, g: it.group, n: it.num} : null).filter(Boolean);
  return {open: !!e.mixOpen, kind: e.mixKind, tray, pending: !!e.mixPending, result: e.mixResult,
          refusal: e.mixRefusal, moving: !!s.pendingItemMove, picked: !!s.pickedItem,
          money: Number(s.playerData.money) || 0}; }
"""

# where the Alquimista stands (null = not in view)
NPC_POS_JS = """
(t) => { const w = window.__world, h = w && w.playerEntity; if (!h) return null;
  const e = w.netObjsQuery.entities.find(e => e.npcType === t && e.transform); if (!e) return null;
  const x = Math.round(e.transform.pos.x), y = Math.round(e.transform.pos.z);
  const hx = h.transform.pos.x, hy = h.transform.pos.z;
  // walkable tile 1-2 tiles from him, on my side (his own tile may not be walkable)
  let best = null;
  for (let dx = -2; dx <= 2; dx++) for (let dy = -2; dy <= 2; dy++) {
    if (!dx && !dy) continue;
    const tx = x + dx, ty = y + dy;
    if (w.isWalkable && !w.isWalkable(tx, ty)) continue;
    const d = Math.hypot(tx - hx, ty - hy);
    if (!best || d < best.d) best = {tx, ty, d};
  }
  return {x, y, tx: best ? best.tx : x, ty: best ? best.ty : y}; }
"""


def describe(items, limit=8):
    s = ", ".join(f"{i['g']}/{i['n']} +{i['lvl']}" for i in items[:limit])
    return s + (f" +{len(items) - limit} more" if len(items) > limit else "")


class Junk:
    def __init__(self, agent):
        self.a = agent
        self.drop_at = 0
        self.last_drop = None        # (slot, key, time) waiting to see the slot empty
        self.skip = {}               # "slot:key" -> until (didn't drop)
        self.dropped = 0
        self.due = None              # time of the Treasure Hunt that made a trip due
        self.next_try = 0
        self.trip = None

    def log(self, msg):
        self.a.log("JUNK: " + msg)

    @property
    def c(self):
        return self.a.c

    def ev(self, js, arg=None):
        return self.a.ev(js, arg)

    def bag(self):
        now = time.time()
        self.skip = {k: t for k, t in self.skip.items() if t > now}
        return self.ev(BAG_JS, [sorted(JUNK_NEVER), sorted(JUNK_JEWELRY), sorted(CRYST_KEEP),
                                CRYST_MAX_LVL, list(self.skip)])

    # called by mu_th when a Treasure Hunt ends
    def hunt_over(self):
        if JUNK_ENABLED and CRYST_ENABLED and self.a.name.lower() not in CRYST_SKIP_CHARS:
            self.due, self.next_try = time.time(), 0

    # ── part 1: drop junk (doesn't block leveling) ──
    def drop_tick(self):
        if not (JUNK_ENABLED and JUNK_DROP):
            return
        now = time.time()
        if self.last_drop:
            slot, key, t = self.last_drop
            if now - t < JUNK_DROP_GAP:
                return
            b = self.bag()
            if b is None:
                return
            if any(j["slot"] == slot and j["key"] == key for j in b["junk"]):
                self.skip[f"{slot}:{key}"] = now + JUNK_DROP_RETRY
                self.log(f"item {key} in slot {slot} did not leave the bag -> leaving it for {JUNK_DROP_RETRY // 60} min")
            else:
                self.dropped += 1
            self.last_drop = None
        elif now - self.drop_at < JUNK_DROP_EVERY:
            return
        else:
            b = self.bag()
        self.drop_at = now
        if not b or not b["junk"] or b["picked"] or b["pending"]:
            return
        j = b["junk"][0]
        r = self.ev(DROP_JS, j["slot"])
        if r == "sent":
            self.last_drop = (j["slot"], j["key"], now)
            self.drop_at = 0                         # next one right after the check
            self.log(f"dropped {j['g']}/{j['n']} +{j['lvl']} (not excellent / ancient) at "
                     f"({self.c.s.get('x')},{self.c.s.get('y')}); {len(b['junk']) - 1} more to go")
        elif r not in ("busy", None):
            self.skip[f"{j['slot']}:{j['key']}"] = now + JUNK_DROP_RETRY
            self.log(f"couldn't drop {j['key']} (slot {j['slot']}): {r}")

    # ── part 2: crystallize at the Alquimista ──
    def tick(self):
        """True while on the trip (leveling paused). Start conditions as the vault trip."""
        a, c, s = self.a, self.c, self.c.s
        now = time.time()
        t = self.trip
        if t is None:
            if not (JUNK_ENABLED and CRYST_ENABLED) or self.due is None or now < self.next_try \
                    or self.a.name.lower() in CRYST_SKIP_CHARS:
                return False
            if now - self.due > CRYST_DUE_SEC:
                self.log(f"crystallizing still not done {CRYST_DUE_SEC // 60} min after the Treasure Hunt -> "
                         f"forgetting it until the next one")
                self.due = None
                return False
            if a.bcs or a.my_trade or a.vault or (s.get("hp") or 0) <= 0 or c.shop_pending \
                    or (s.get("level") or 0) >= (a.hub.reset_level or 10 ** 6) or a.trade_quiet():
                return False
            ht = a.hub.trade
            if ht and a.name in (ht.get("alt"), ht.get("lead")):
                return False
            b = self.bag()
            if b is None:
                return False
            if not b["cryst"]:
                self.due = None
                return False
            if b["money"] < CRYST_FEE + CRYST_ZEN_KEEP:
                self.log(f"{len(b['cryst'])} excellent item(s) to crystallize but only {b['money']:,} zen "
                         f"(needs {CRYST_FEE + CRYST_ZEN_KEEP:,}) -> not going")
                self.due = None
                return False
            t = self.trip = {"since": now, "phase": "go", "phase_at": now, "tries": 0, "batches": 0,
                             "items": 0, "crystals": 0}
            self.log(f"{len(b['cryst'])} excellent item(s) to crystallize ({describe(b['cryst'])}) -> "
                     f"going to the Alquimista in Noria")
        a.mode = "crystal"
        if (s.get("hp") or 0) <= 0:
            return True
        if a.bcs or a.trade_quiet() and t["phase"] in ("go", "find", "open"):
            return self.end("an event is coming", ok=False)
        if now - t["since"] > CRYST_TIMEOUT:
            return self.end(f"took more than {CRYST_TIMEOUT}s (phase {t['phase']})", ok=False)
        try:
            return self.step(t, now)
        except Exception as e:
            self.log(f"error: {e!r}")
            return True

    def phase(self, t, p):
        t["phase"], t["phase_at"], t["tries"] = p, time.time(), 0

    def step(self, t, now):
        a, c, s = self.a, self.c, self.c.s
        if t["phase"] == "go":
            if a.go(ALCHEMIST_MAP[0], ALCHEMIST_MAP[1], *ALCHEMIST_NEAR, radius=6):
                self.phase(t, "find")
            return True
        if t["phase"] == "find":
            pos = self.ev(NPC_POS_JS, ALCHEMIST_NPC)
            if not pos:
                if now - t["phase_at"] > 15:
                    return self.end(f"no Alquimista (npcType {ALCHEMIST_NPC}) in view near "
                                    f"{ALCHEMIST_NEAR} in Noria", ok=False, retry=3600)
                return True
            if dist(s, pos["x"], pos["y"]) <= ALCHEMIST_TALK_R or \
                    a.go(ALCHEMIST_MAP[0], ALCHEMIST_MAP[1], pos["tx"], pos["ty"], radius=1):
                if not t.get("seen"):
                    t["seen"] = True
                    self.log(f"Alquimista at ({pos['x']},{pos['y']})")
                self.phase(t, "open")
            return True
        st = self.ev(MIX_STATE_JS) or {}
        if t["phase"] == "open":
            if st.get("open") and st.get("kind") == "alchemist":
                if st.get("tray"):
                    self.phase(t, "collect")         # something left on the table from before
                else:
                    self.phase(t, "load")
                return True
            if st.get("open"):                       # some other mix window
                self.ev("() => window.__eco.closeMix(false)")
                return True
            if now - t["phase_at"] < 4 and t["tries"]:
                return True
            if t["tries"] >= 3:
                return self.end("the Alquimista's window did not open", ok=False)
            c.set_helper(False)
            c.close_windows()
            if not self.ev(B.TALK_NPC_JS, ALCHEMIST_NPC):
                self.phase(t, "find")
                return True
            t["tries"] += 1
            t["phase_at"] = now
            return True
        if not (st.get("open") and st.get("kind") == "alchemist"):
            return self.end(f"the Alquimista's window closed (phase {t['phase']})", ok=t["batches"] > 0)
        if st.get("moving") or st.get("pending"):
            return True
        if t["phase"] == "load":
            b = self.bag() or {}
            on_tray = [x for x in st.get("tray") or [] if (x["g"], x["n"]) != CRYST_ITEM]
            todo = b.get("cryst") or []
            if todo and len(on_tray) < CRYST_BATCH and now - t["phase_at"] < 30:
                if t.get("last") == todo[0]["slot"] and now - t.get("move_at", 0) < 3:
                    return True                      # the server hasn't answered yet
                t["last"], t["move_at"] = todo[0]["slot"], now
                self.ev("(i) => window.__store.autoMoveItem(0, i, 3)", todo[0]["slot"])   # bag -> tray
                return True
            if not on_tray:
                return self.end("nothing went onto the table", ok=t["batches"] > 0)
            if (st.get("money") or 0) < CRYST_FEE:
                self.log(f"only {st.get('money'):,} zen for the {CRYST_FEE:,} fee")
                self.phase(t, "collect")
                t["abort"] = True
                return True
            # give the Alquimista a moment to price the table, then crystallize
            if now - t.get("move_at", 0) < 1.5:
                return True
            t["batch"] = len(on_tray)
            names = ", ".join(f"{x['g']}/{x['n']}" for x in on_tray)
            self.log(f"crystallizing {len(on_tray)} item(s) ({names})")
            self.ev("() => window.__eco.mix()")
            self.phase(t, "mix")
            return True
        if t["phase"] == "mix":
            if st.get("result") == "success":
                n = len(st.get("tray") or [])
                t["batches"] += 1
                t["items"] += t.get("batch", 0)
                self.log(f"batch {t['batches']} done: {t.get('batch')} item(s) -> {n} stack(s) of Cristales on the table")
                self.phase(t, "collect")
                return True
            if st.get("result") == "failed" or st.get("refusal"):
                self.log(f"crystallize refused: {st.get('refusal') or st.get('result')} -> taking the items back")
                t["abort"] = True
                self.phase(t, "collect")
                return True
            if now - t["phase_at"] > 10:
                self.log("no answer to Cristalizar -> taking the items back")
                t["abort"] = True
                self.phase(t, "collect")
            return True
        if t["phase"] == "collect":
            tray = st.get("tray") or []
            if tray:
                if now - t.get("move_at", 0) < 1:
                    return True
                if now - t["phase_at"] > 30:
                    return self.end("couldn't take everything off the table (bag full?)", ok=False, retry=3600)
                t["move_at"] = now
                self.ev("(i) => window.__store.autoMoveItem(3, i, 0)", tray[0]["slot"])   # tray -> bag
                return True
            if not t.get("abort") and (self.bag() or {}).get("cryst"):
                self.phase(t, "load")                # next batch
                return True
            self.ev("() => window.__eco.closeMix()")
            return self.end(f"{t['batches']} batch(es), {t['items']} item(s) crystallized", ok=not t.get("abort"))
        return True

    def end(self, why, ok, retry=None):
        self.log(f"Alquimista trip over: {why}")
        try:
            st = self.ev(MIX_STATE_JS) or {}
            if st.get("open") and not st.get("tray"):
                self.ev("() => window.__eco.closeMix()")
        except Exception:
            pass
        self.trip = None
        if ok:
            self.due = None
        else:
            self.next_try = time.time() + (retry or CRYST_RETRY_SEC)
        self.c.walk_target = None
        return False

    def busy(self):
        return self.trip is not None

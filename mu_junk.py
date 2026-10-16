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

2. CRYSTALLIZE  after every /reset (Juan 2026-10-15; was: after every Treasure Hunt), a character carrying excellent gear that is NOT end-game
         top gear (CRYST_KEEP = the server's "best gear of each class") takes it to the Alquimista
         (npcType 693, next to the Chaos Goblin in Noria) and crystallizes it ("Cristalizar", up to 8
         items and 500k zen per batch), then takes the Cristales back to the bag.
         Only weapons, shields and set parts (the Alquimista refuses wings, rings, pendants).
         Juan 2026-10-15 (cucaracha's recording): talk to him -> "Cristalizar" (menu) -> put items on the
         table until it holds 8 or the next one doesn't fit -> "Cristalizar" (button at the bottom) -> take
         the Cristales off the table (with Cristales on the table it won't crystallize) -> next items.
         (v1 put 8 at once without checking the room -> "There is no room for that item", bruto stuck.) At the end the Cristales are joined into stacks in the bag (they
         don't stack by themselves). Zen is no problem (no zen check).
         Items at +CRYST_MAX_LVL+1 or higher are kept too (fusion material, see the guide).
         Juan 2026-10-16: the end-game exception only holds from +CRYST_KEEP_FROM (7) up; excellent
         end-game gear below +7 is crystallized too. Excellent rings / pendants are always kept.
1b. JOIN  right when a character goes (back) to leveling, then every JOIN_EVERY s, small Cristal stacks in
         the bag are joined (picked up and dropped on each other, like a player does).
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
JUNK_CLOAKS      = True      # (Juan 2026-10-16) leftover Invisibility Cloaks (B.CLOAK) are dropped too, once the
                             # character is back to leveling with no Blood Castle planned (never while it holds
                             # one for the next castle)

CRYST_ENABLED    = True      # part 2 on/off
CRYST_SKIP_CHARS = {"wizzardo"}   # these characters never crystallize (Juan 2026-10-15: not the main)
CRYST_DUE_SEC    = 7200      # after a Treasure Hunt / bot start, try for this long (events / trades / resets come first;
                             # right after the :40 hunt come GI :50 and BC :00, so the trip usually starts ~:05)
CRYST_CHECK_EVERY = 10      # while due: look at the bag this often
CRYST_AFTER_RESET = True     # (Juan 2026-10-15) a trip is due after every /reset (and grand reset), if I carry any
CRYST_AFTER_TH   = False     # ... no longer after every Treasure Hunt (resets come far more often); True = both
CRYST_MIN_LEVEL  = 10        # right after a reset: wait until /move Noria is allowed (level 10)
CRYST_RETRY_SEC  = 300       # failed trip -> try again after this long (while still due)
CRYST_TIMEOUT    = 300       # whole trip
CRYST_ITEM_SEC   = 10        # extra trip time allowed per item to crystallize
CRYST_BATCH      = 8         # items per Cristalizar (the Alquimista's maxBatch)
CRYST_STACK_MAX  = 255       # Cristales per stack (the stack size is the item's durability byte)
CRYST_MAX_LVL    = 12        # +13 and up are kept (fusion material)
CRYST_KEEP_FROM  = 7         # (Juan 2026-10-16) CRYST_KEEP (end-game gear) is only kept from +7 up: an excellent
                             # below +7 ("-7") is crystallized even if it's top gear. Excellent rings / pendants
                             # are always kept (only gear groups 0-11 ever go to the Alquimista).
JOIN_ENABLED     = True      # (Juan 2026-10-16) join small Cristal stacks in the bag while leveling
JOIN_EVERY       = 300       # seconds between looks at the Cristal stacks (one join per tick when joining)
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
([never, jewelry, keep, maxLvl, skip, keepFrom, cloak]) => {
  const s = window.__store; if (!s || !s.playerData) return null;
  const items = s.playerData.items || [], has = (l, it) => l.some(p => p[0] === it.group && p[1] === it.num);
  const junk = [], cryst = [];
  for (let i = 12; i < Math.min(items.length, 76); i++) {   // 76+ = personal shop
    const it = items[i]; if (!it || it.group === undefined) continue;
    const key = `${it.group}/${it.num}/${it.lvl || 0}/${it.isExcellent ? 1 : 0}`;
    const d = {slot: i, g: it.group, n: it.num, lvl: it.lvl || 0, key};
    if (cloak && it.group === cloak[0] && it.num === cloak[1]) { if (!skip.includes(i + ':' + key)) junk.push({...d, cloak: true}); continue; }
    if (has(never, it)) continue;
    const gear = it.group >= 0 && it.group <= 11, jew = has(jewelry, it);
    if (!gear && !jew) continue;
    if (!it.isExcellent && !it.isAncient) { if (!skip.includes(i + ':' + key)) junk.push(d); continue; }
    if (gear && it.isExcellent && !it.isAncient && !(has(keep, it) && (it.lvl || 0) >= keepFrom)
        && (it.lvl || 0) <= maxLvl) cryst.push(d);
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
  const tray = (e.mixItems || []).map((it, i) => it ? {slot: i, g: it.group, n: it.num, d: it.durability} : null).filter(Boolean);
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


# click a control of the Alquimista's window by its text / tooltip ("menu" = an entry with that text,
# "button" = the image button whose tooltip is that text). "clicked" / "not found"
CLICK_TEXT_JS = """
([kind, text]) => {
  const win = document.querySelector('.v097d-alchemist') || document.getElementById('alchemist');
  if (!win) return 'no window';
  const norm = v => (v || '').replace(/\\s+/g, ' ').trim().toLowerCase(), want = text.toLowerCase();
  const els = [...win.querySelectorAll('*')].filter(e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; });
  // cucaracha's recording 2026-10-15: menu entry = div.v097d-quest-proceed[role=button] > span "Cristalizar";
  // bottom button = div.v097d-button (text "Cristalizar"); Close = div.v097d-button "Close"
  const sel = kind === 'button' ? '.v097d-button' : '.v097d-quest-proceed';
  let el = els.find(e => e.matches(sel) && norm(e.textContent) === want);
  if (!el && kind === 'menu')
    el = els.find(e => e.childElementCount === 0 && norm(e.textContent) === want && !/band|title/.test(String(e.className)));
  if (!el) return 'not found';
  for (const t of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click'])
    el.dispatchEvent(new MouseEvent(t, {bubbles: true, cancelable: true, view: window}));
  return 'clicked';
}
"""

# bag slots of excellent gear that fit on the table right now (the client's own free-square search)
FITS_JS = """
([cg, cn]) => { const s = window.__store; if (!s || !s.findFreeSquare) return null;
  const items = s.playerData.items || [], fits = [];
  for (let i = 12; i < Math.min(items.length, 76); i++) { const it = items[i];
    if (it && it.isExcellent && !(it.group === cg && it.num === cn)) { try { if (s.findFreeSquare(3, it) >= 0) fits.push(i); } catch (e) {} } }
  return {fits}; }
"""

# Cristales in the bag: [{slot, n}] (n = stack size)
CRYSTALS_JS = """
([g, n]) => { const s = window.__store; if (!s) return null;
  const items = s.playerData.items || [], out = [];
  for (let i = 12; i < Math.min(items.length, 76); i++) { const it = items[i];
    if (it && it.group === g && it.num === n) out.push({slot: i, n: Number(it.durability ?? 1) || 1}); }
  return {stacks: out, pending: !!s.pendingItemMove, picked: !!s.pickedItem}; }
"""

# move `n` pieces of the stack in bag slot `from` onto the same item in slot `to`: the server's
# "/dividir <from> <to> <n>" chat command (client: __store.sendSplitCommand, used when laying pieces)
SPLIT_JS = """
([from, to, n]) => { const s = window.__store;
  if (!s || s.pickedItem || s.pendingItemMove) return 'busy';
  if (typeof s.sendSplitCommand === 'function') { s.sendSplitCommand(from, to, n); return 'sent'; }
  if (window.__social && window.__social.sendChat) { window.__social.sendChat(`/dividir ${from} ${to} ${n}`); return 'sent (chat)'; }
  return 'no way to send';
}
"""


# join two Cristal stacks the way a player does: pick one stack up and drop it on the other
# (server: "dropping a stack on an identical one joins them"). "/dividir" only splits into an EMPTY cell --
# onto an occupied one the server ignores it (every join attempt 2026-10-15/16 showed "no change").
MERGE_JS = """
([from, to]) => { const s = window.__store;
  if (!s || s.pickedItem || s.pendingItemMove) return 'busy';
  const items = s.playerData.items || [];
  if (!items[from] || !items[to]) return 'slot empty';
  s.pickInventoryItem(from);
  if (!s.pickedItem) return 'could not pick it up';
  s.placePickedItem(to, 0);
  return s.pendingItemMove ? 'sent' : (s.pickedItem ? 'still in hand' : 'done');
}
"""

CANCEL_PICK_JS = "() => { const s = window.__store; if (s && s.pickedItem && !s.pendingItemMove) { s.cancelPickedItem(); return true; } return false; }"


def join_pair(stacks):
    """(src, dst) for the next join, or None. Prefer a pair that fits in one stack (smallest onto the biggest
    open stack it fits on); otherwise the smallest onto the biggest open one (the server fills up to 255)."""
    open_ = sorted([x for x in stacks if x["n"] < CRYST_STACK_MAX], key=lambda x: (-x["n"], x["slot"]))
    if len(open_) < 2:
        return None
    src = open_[-1]
    for dst in open_[:-1]:
        if dst["n"] + src["n"] <= CRYST_STACK_MAX:
            return src, dst
    return src, open_[0]


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
        self.due = time.time()       # a trip is due since: bot start, and after every Treasure Hunt
        self.th_seen = None          # (2026-10-15) every Treasure Hunt makes it due, also for characters
                                     # that skipped it (low level / busy): they may carry excellents too
        self.wait_why = None         # last "waiting because" logged
        self.check_at = 0
        self.resets_seen = None      # reset counter last seen (a change = a reset happened)
        self.next_try = 0
        self.trip = None
        self.join_at = 0             # Cristal stack joining while leveling
        self.join = None             # {"sig", "at", "stuck", "n"} while joining
        self.hand_since = None       # a stack picked up for joining is still in hand since

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
        cloak = list(B.CLOAK) if JUNK_CLOAKS and getattr(self.a, "bcs", None) is None else None
        return self.ev(BAG_JS, [sorted(JUNK_NEVER), sorted(JUNK_JEWELRY), sorted(CRYST_KEEP),
                                CRYST_MAX_LVL, list(self.skip), CRYST_KEEP_FROM, cloak])

    # called by mu_th when a Treasure Hunt ends
    def hunt_over(self):
        if CRYST_AFTER_TH:
            self.make_due()

    def make_due(self):
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
            self.log(f"dropped {j['g']}/{j['n']} +{j['lvl']} "
                     + ("(leftover Blood Castle cloak)" if j.get("cloak") else "(not excellent / ancient)") + " at "
                     f"({self.c.s.get('x')},{self.c.s.get('y')}); {len(b['junk']) - 1} more to go")
        elif r not in ("busy", None):
            self.skip[f"{j['slot']}:{j['key']}"] = now + JUNK_DROP_RETRY
            self.log(f"couldn't drop {j['key']} (slot {j['slot']}): {r}")

    # ── part 1b: join small Cristal stacks while leveling (doesn't block leveling) ──
    def join_tick(self):
        """Juan 2026-10-16: characters carry several small Cristal stacks (trades, crystallizing) that waste
        bag cells. Every JOIN_EVERY s: while 2+ stacks aren't full, one /dividir per tick (biggest open
        stack filled from the smallest), waiting for the server between moves."""
        if not (JUNK_ENABLED and JOIN_ENABLED):
            return
        now = time.time()
        j = self.join
        if j is None and now - self.join_at < JOIN_EVERY:
            return
        if self.last_drop:
            return                                   # a drop is waiting for its answer
        b = self.ev(CRYSTALS_JS, list(CRYST_ITEM)) or {}
        if self.hand_stuck(b, now):
            return
        stacks = b.get("stacks") or []
        sig = tuple(sorted((x["slot"], x["n"]) for x in stacks))
        if j is not None:
            if j["sig"] == sig and now - j["at"] < 3:
                return                               # the server hasn't answered the last move yet
            if j["sig"] == sig:
                j["stuck"] += 1
        pair = join_pair(stacks)
        if pair is None or (j and (j["stuck"] >= 3 or j["n"] >= 40)):
            if j is not None:
                full = sum(1 for x in stacks if x["n"] >= CRYST_STACK_MAX)
                self.log(f"Cristales joined: {sum(x['n'] for x in stacks)} in {len(stacks)} stack(s), {full} full"
                         + (" (stopped: no change from the server)" if j["stuck"] >= 3 else ""))
            self.join, self.join_at = None, now
            return
        if j is None:
            j = self.join = {"sig": None, "at": 0, "stuck": 0, "n": 0}
            open_ = [x["n"] for x in stacks if x["n"] < CRYST_STACK_MAX]
            self.log(f"joining Cristales: {len(open_)} stacks not full ({', '.join(map(str, sorted(open_, reverse=True)))})")
        src, dst = pair
        r = self.ev(MERGE_JS, [src["slot"], dst["slot"]])
        if j["n"] < 10:
            self.log(f"Cristales: stack of {src['n']} (slot {src['slot']}) -> onto {dst['n']} (slot {dst['slot']}): {r}")
        j["sig"], j["at"], j["n"] = sig, now, j["n"] + 1

    def hand_stuck(self, b, now):
        """True while an item move is pending / a stack is in hand. A stack left in hand for more than
        5 s (the server refused the drop) is put back."""
        if not (b.get("pending") or b.get("picked")):
            self.hand_since = None
            return False
        if b.get("picked") and not b.get("pending"):
            self.hand_since = self.hand_since or now
            if now - self.hand_since > 5:
                self.ev(CANCEL_PICK_JS)
                self.log("Cristales: a stack stayed in hand -> put back")
                self.hand_since = None
        return True

    # ── part 2: crystallize at the Alquimista ──
    def tick(self):
        """True while on the trip (leveling paused). Start conditions as the vault trip."""
        a, c, s = self.a, self.c, self.c.s
        now = time.time()
        t = self.trip
        if t is None:
            r = s.get("resets")
            if r is not None and r != self.resets_seen:
                if self.resets_seen is not None and CRYST_AFTER_RESET:
                    self.make_due()                  # after every reset: excellents picked up meanwhile -> Alquimista
                self.resets_seen = r
            th = getattr(a, "th", None)
            if th is not None and not th.active() and th.handled != self.th_seen:
                self.th_seen = th.handled            # a Treasure Hunt went by (hunted or skipped)
                if th.handled is not None:
                    self.hunt_over()
            if not (JUNK_ENABLED and CRYST_ENABLED) or self.due is None or now < self.next_try \
                    or self.a.name.lower() in CRYST_SKIP_CHARS:
                return False
            if now - self.due > CRYST_DUE_SEC:
                self.log(f"crystallizing still not done {CRYST_DUE_SEC // 60} min after it became due -> "
                         f"forgetting it until the next Treasure Hunt")
                self.due = None
                return False
            if now - self.check_at < CRYST_CHECK_EVERY:
                return False
            self.check_at = now
            b = self.bag()
            if b is None:
                return False
            if not b["cryst"]:
                self.due, self.wait_why = None, None
                return False
            ht = a.hub.trade
            why = ("Blood Castle" if a.bcs else "box trade" if a.my_trade or (ht and a.name in (ht.get("alt"), ht.get("lead")))
                   else "vault trip" if a.vault else "dead" if (s.get("hp") or 0) <= 0 else "buying Zen Chests" if c.shop_pending
                   else "at the reset level" if (s.get("level") or 0) >= (a.hub.reset_level or 10 ** 6)
                   else f"level {s.get('level')} (Noria needs {CRYST_MIN_LEVEL})" if (s.get("level") or 0) < CRYST_MIN_LEVEL
                        and s.get("map") != ALCHEMIST_MAP[0]
                   else "an event is on / coming" if a.trade_quiet() else None)
            if why:
                if why != self.wait_why:
                    self.wait_why = why
                    self.log(f"{len(b['cryst'])} excellent item(s) to crystallize, waiting: {why}")
                return False
            self.wait_why = None
            t = self.trip = {"since": now, "phase": "go", "phase_at": now, "tries": 0, "batches": 0,
                             "items": 0, "skip": set()}
            self.log(f"{len(b['cryst'])} excellent item(s) to crystallize ({describe(b['cryst'])}) -> "
                     f"going to the Alquimista in Noria")
        a.mode = "crystal"
        if (s.get("hp") or 0) <= 0:
            return True
        if a.bcs or a.trade_quiet() and t["phase"] in ("go", "find", "open"):
            return self.end("an event is coming", ok=False)
        if now - t["since"] > CRYST_TIMEOUT + CRYST_ITEM_SEC * t.get("todo", 0):
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
                r = self.ev(CLICK_TEXT_JS, ["menu", "Cristalizar"])      # 1. "Cristalizar" in his menu
                self.log(f"Alquimista window open -> 'Cristalizar' menu: {r}")
                self.phase(t, "clear")
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
        if t["phase"] == "stack":
            return self.stack_step(t, now)
        if not (st.get("open") and st.get("kind") == "alchemist"):
            return self.end(f"the Alquimista's window closed (phase {t['phase']})", ok=t["items"] > 0)
        if st.get("moving") or st.get("pending") or st.get("picked"):
            return True
        tray = st.get("tray") or []
        if t["phase"] == "clear":                    # take EVERYTHING off the table (Cristales block the next mix)
            if tray:
                if now - t.get("move_at", 0) < 1:
                    return True
                if now - t["phase_at"] > 20:
                    return self.end(f"couldn't take {len(tray)} thing(s) off the table (bag full?)", ok=False, retry=3600)
                t["move_at"] = now
                self.ev("(i) => window.__store.autoMoveItem(3, i, 0)", tray[0]["slot"])   # table -> bag
                return True
            if t.get("stop_after_clear"):
                self.ev("() => window.__eco.closeMix()")
                self.phase(t, "stack")
                return True
            self.phase(t, "load")
            t["batch"] = []
            return True
        if t["phase"] == "load":
            # Juan 2026-10-15 (cucaracha's recording): put items on the table until it holds CRYST_BATCH or the
            # next one doesn't fit, then Cristalizar, take the Cristales off, carry on with the rest
            b = self.ev(FITS_JS, [CRYST_ITEM[0], CRYST_ITEM[1]]) or {}
            on = [x for x in tray if (x["g"], x["n"]) != CRYST_ITEM]
            if any((x["g"], x["n"]) == CRYST_ITEM for x in tray):
                self.phase(t, "clear")
                return True
            last = t.get("cur")
            if last and now - t.get("move_at", 0) < 3 and any(x["slot"] == last["slot"] and x["key"] == last["key"]
                                                               for x in (self.bag() or {}).get("cryst") or []):
                return True                          # the last one hasn't left the bag yet
            if last and any(x["slot"] == last["slot"] and x["key"] == last["key"] for x in (self.bag() or {}).get("cryst") or []):
                t["skip"].add(f"{last['slot']}:{last['key']}")
                self.log(f"{last['g']}/{last['n']} +{last['lvl']} (slot {last['slot']}) didn't go onto the table -> skipping it")
            t["cur"] = None
            todo = [x for x in (self.bag() or {}).get("cryst") or [] if f"{x['slot']}:{x['key']}" not in t["skip"]]
            t["todo"] = len(todo)
            fits = set(b.get("fits") or [])
            nxt = next((x for x in todo if x["slot"] in fits), None)
            if nxt and len(on) < CRYST_BATCH:
                t["cur"], t["move_at"] = nxt, now
                t["batch"].append(f"{nxt['g']}/{nxt['n']} +{nxt['lvl']}")
                self.ev("(i) => window.__store.autoMoveItem(0, i, 3)", nxt["slot"])   # bag -> table
                return True
            if not on:
                if todo:                             # nothing fits an EMPTY table?
                    for x in todo:
                        t["skip"].add(f"{x['slot']}:{x['key']}")
                    self.log(f"{len(todo)} item(s) don't fit the empty table ({describe(todo)}) -> skipping them")
                self.ev("() => window.__eco.closeMix()")
                self.phase(t, "stack")
                return True
            if now - t.get("move_at", 0) < 1.5:
                return True                          # let him price the table
            r = self.ev(CLICK_TEXT_JS, ["button", "Cristalizar"])    # "Cristalizar" at the bottom
            if r != "clicked":
                self.ev("() => window.__eco.mix()")                  # what that button calls (c1 05 86 01 00)
            t["mix_how"], t["batch_n"] = r, len(on)
            self.phase(t, "mix")
            return True
        if t["phase"] == "mix":
            if st.get("result") == "success":
                t["items"] += t.get("batch_n", 0)
                t["batches"] += 1
                cr = sum(y.get("d") or 0 for y in tray if (y["g"], y["n"]) == CRYST_ITEM)
                self.log(f"batch {t['batches']}: crystallized {t.get('batch_n')} item(s) ({', '.join(t.get('batch') or [])}) "
                         f"-> {cr} Cristales ({t['items']} done, {max(t.get('todo', 0), 0)} left; button: {t.get('mix_how')})")
                self.phase(t, "clear")               # Cristales off the table, next batch
                return True
            if st.get("result") == "failed" or st.get("refusal") or now - t["phase_at"] > 10:
                why = st.get("refusal") or st.get("result") or "no answer"
                for y in tray:
                    if (y["g"], y["n"]) != CRYST_ITEM:
                        t["skip"].add(f"tray:{y['g']}/{y['n']}")
                self.log(f"Cristalizar refused ({why}) for {', '.join(t.get('batch') or [])} -> taking them back")
                t["refused"] = t.get("refused", 0) + 1
                if t["refused"] >= 2:
                    self.phase(t, "clear")
                    t["stop_after_clear"] = True
                else:
                    self.phase(t, "clear")
            return True
        return True

    def stack_step(self, t, now):
        """5. Join the Cristales in the bag: the server's "/dividir <from slot> <to slot> <count>" (what the
        client sends when you lay pieces of a stack on another) moves `count` pieces onto the other stack.
        Fill the biggest stack that isn't full (255 = the count is one byte) from the smallest one."""
        b = self.ev(CRYSTALS_JS, list(CRYST_ITEM)) or {}
        if self.hand_stuck(b, now):
            return True
        stacks = b.get("stacks") or []
        sig = tuple(sorted((x["slot"], x["n"]) for x in stacks))
        if t.get("join_sig") == sig and now - t.get("move_at", 0) < 3:
            return True                              # wait for the server to answer the last move
        if t.get("join_sig") == sig and t.get("join_sent"):
            t["join_stuck"] = t.get("join_stuck", 0) + 1
        t["join_sent"] = False
        pair = join_pair(stacks)
        if pair is None or t.get("join_stuck", 0) >= 3 or now - t["phase_at"] > 60:
            full = sum(1 for x in stacks if x["n"] >= CRYST_STACK_MAX)
            extra = " (joining stopped: no change from the server)" if t.get("join_stuck", 0) >= 3 else ""
            return self.end(f"{t['items']} item(s) crystallized; Cristales in the bag: {sum(x['n'] for x in stacks)} "
                            f"in {len(stacks)} stack(s), {full} full{extra}", ok=True)
        src, dst = pair
        r = self.ev(MERGE_JS, [src["slot"], dst["slot"]])
        t["join_sig"], t["move_at"], t["join_sent"] = sig, now, True
        t["joins"] = t.get("joins", 0) + 1
        if t["joins"] <= 10:
            self.log(f"Cristales: stack of {src['n']} (slot {src['slot']}) -> onto {dst['n']} (slot {dst['slot']}): {r}")
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

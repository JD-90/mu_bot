"""
Recorder for manual play: while a character is PAUSED (you are playing it), this writes down what
happens in its game window so the bot can later learn new things from it (e.g. Golden Invasion).

Used by mu_auto_bot.py and mu_group_bot.py: start() on pause, tick() every second while paused,
stop() on resume. Output goes to mu_record.log next to the scripts (one line per thing seen,
tagged with time and character). It only READS the game; it never clicks or sends anything.

What it records (only changes, so the file stays readable):
  * chat / system messages, and any new text that pops up on screen (announcements like
    "The Golden Dragon is invading!", dialogs)
  * the event panel (Blood Castle / Devil Square / Chaos Castle / Golden Invasion rows)
  * every game packet/event name the client fires (first time seen + a count per minute), with a
    short payload dump for the interesting-looking ones
  * monsters coming into / leaving view (name, type id, position, whether it died), and the fields
    of each new monster type once
  * items dropping on the ground near you, your bag gaining / losing items, zen changes
  * your map, position (every few seconds while moving), HP, deaths, MU Helper on/off
  * on stop: a summary (monsters seen by name, events by count, items gained / lost)
"""
import threading
import time
from datetime import datetime
from pathlib import Path

RECORD_WHEN_PAUSED = True
RECORD_FILE        = Path(__file__).with_name("mu_record.log")
RECORD_EVERY       = 1.0     # seconds between samples
RECORD_RADIUS      = 30      # tiles: monsters / ground items further than this are ignored
RECORD_POS_EVERY   = 5       # seconds between position lines while moving

_lock = threading.Lock()

REC_JS = r"""
([cmd, R_RADIUS, POS_EVERY]) => {
  const out = [];
  const W = window, s = W.__store, w = W.__world, soc = W.__social, bus = W.__eventBus;
  if (!s || !w) return ['(game not loaded)'];
  const now = Date.now();
  const short = (v, n) => { let t; try { t = typeof v === 'string' ? v : JSON.stringify(v); } catch (e) { t = String(v); }
                            t = (t || '').replace(/\s+/g, ' '); return t.length > n ? t.slice(0, n) + '…' : t; };
  const hex = (u, n) => Array.from(u.slice(0, n)).map(b => b.toString(16).padStart(2, '0')).join(' ');
  const payload = d => {
    try {
      if (d instanceof Uint8Array || (d && d.buffer instanceof ArrayBuffer && d.byteLength !== undefined)) {
        const u = new Uint8Array(d.buffer || d, d.byteOffset || 0, d.byteLength);
        return `[${u.length}b] ${hex(u, 48)}`;
      }
      return short(d, 220);
    } catch (e) { return '?'; }
  };
  const hero = w.playerEntity, p = s.playerData || {};
  const pos = hero ? {x: Math.round(hero.transform.pos.x), y: Math.round(hero.transform.pos.z)} : {x: p.x, y: p.y};
  const near = e => hero ? Math.hypot(e.transform.pos.x - hero.transform.pos.x, e.transform.pos.z - hero.transform.pos.z) : 0;
  const ents = (w.netObjsQuery && w.netObjsQuery.entities) || [];
  const prim = (o, n) => { const r = {}; if (!o) return r;
    for (const k of Object.keys(o)) { const v = o[k]; const t = typeof v;
      if (t === 'number' || t === 'string' || t === 'boolean') r[k] = v;
      else if (v && t === 'object' && !Array.isArray(v) && Object.keys(r).length < n) {
        const sub = {}; let c = 0;
        for (const kk of Object.keys(v)) { const vv = v[kk];
          if (['number', 'string', 'boolean'].includes(typeof vv) && c++ < 8) sub[kk] = vv; }
        if (c) r[k] = sub;
      } }
    return r; };
  const bag = () => { const m = {};
    (p.items || []).forEach((it, i) => { if (i < 12 || !it) return;
      const k = `${it.name || it.itemName || (it.group + '/' + it.num)}${it.lvl ? ' +' + it.lvl : ''}`;
      m[k] = (m[k] || 0) + 1; });
    return m; };
  const evRows = () => Array.from(document.querySelectorAll('.event-row-label'))
      .map(e => ((e.parentElement || e).textContent || '').replace(/\s+/g, ' ').trim()).filter(Boolean);
  const chatTxt = l => typeof l === 'string' ? l :
      ([l.name, l.sender, l.from].find(Boolean) ? [l.name, l.sender, l.from].find(Boolean) + ': ' : '') +
      ([l.text, l.message, l.msg, l.content].filter(Boolean).join(' ') || short(l, 200));

  // ── (re)arm ──
  let R = W.__rec;
  if (cmd === 'start' || !R) {
    const rearm = !!R && cmd !== 'start';
    R = W.__rec = {on: true, t0: now, chatSeen: new WeakSet(), chatStrLen: 0, mobs: new Map(), types: new Set(),
                   drops: new Map(), bag: bag(), money: p.money, map: w.mapIndex, lastPos: pos, lastPosAt: 0,
                   hp: p.currentHP, helper: !!(s.muHelper && s.muHelper.active), rows: {}, rowsAt: now,
                   evCount: {}, evMin: {}, evMinAt: now, evLines: {}, dom: [], mobTotal: {}, kills: {},
                   bag0: bag(), money0: p.money};
    W.__recOn = true;
    out.push(rearm ? '(page was reloaded: recorder re-armed)' :
      `START map ${w.mapIndex} pos (${pos.x},${pos.y}) lvl ${p.level} hp ${p.currentHP}/${p.maxHP} zen ${p.money} helper ${R.helper}`);
    const lines = (soc && soc.chatLines) || [];
    lines.forEach(l => { if (l && typeof l === 'object') R.chatSeen.add(l); });
    R.chatStrLen = lines.length;
    lines.slice(-8).forEach(l => out.push('recent chat: ' + short(chatTxt(l), 200)));
    evRows().forEach(r => { out.push('event panel: ' + r);
      R.rows[r.replace(/[^A-Za-z ]+.*$/, '').trim() || r] = r.replace(/\d+:\d+(:\d+)?/g, '#'); });
    out.push('bag: ' + short(R.bag, 600));
    if (hero && !W.__recHeroDumped) { W.__recHeroDumped = true; out.push('hero fields: ' + short(prim(hero, 40), 1500)); }
    out.push('store keys: ' + Object.keys(s).filter(k => typeof s[k] !== 'function').join(','));
    if (soc) out.push('social keys: ' + Object.keys(soc).filter(k => typeof soc[k] !== 'function').join(','));

    // every client event / packet: wrap the bus's emit-like method once per page
    if (bus && !W.__recBus) {
      W.__recBus = 'none';
      const proto = Object.getPrototypeOf(bus) || {};
      const fns = [...new Set([...Object.keys(bus), ...Object.getOwnPropertyNames(proto)])]
        .filter(k => typeof bus[k] === 'function');
      const name = ['emit', 'dispatch', 'fire', 'trigger', 'publish'].find(f => fns.includes(f));
      if (name) {
        const orig = bus[name].bind(bus);
        bus[name] = function (ev, ...args) {
          try {
            const r = W.__rec;
            if (W.__recOn && r && typeof ev === 'string') {
              const first = !(ev in r.evCount);
              r.evCount[ev] = (r.evCount[ev] || 0) + 1;
              r.evMin[ev] = (r.evMin[ev] || 0) + 1;
              const loud = /gold|invas|event|drop|item|kill|die|dead|death|notice|announce|message|chat|server|reward|exp|box|zen|money|monster/i.test(ev);
              if (first || (loud && (r.evLines[ev] = (r.evLines[ev] || 0) + 1) <= 30))
                r.dom.push(`event ${first ? 'NEW ' : ''}${ev}: ${args.length ? payload(args[0]) : ''}`);
            }
          } catch (e) {}
          return orig(ev, ...args);
        };
        W.__recBus = name;
      }
      out.push(`event bus: hooked '${W.__recBus}' (methods: ${fns.join(',')})`);
    }
    // new text appearing on screen (announcements, dialogs, notices)
    if (!W.__recObs) {
      W.__recObs = new MutationObserver(muts => {
        const r = W.__rec; if (!W.__recOn || !r) return;
        for (const m of muts) for (const n of m.addedNodes) {
          const el = n.nodeType === 1 ? n : n.parentElement; if (!el) continue;
          if (el.closest && el.closest('.event-row-label')) continue;
          const t = (n.textContent || '').replace(/\s+/g, ' ').trim();
          if (!t || t.length > 200 || /^\d+([:.,]\d+)*$/.test(t)) continue;
          if (r.lastDom === t && Date.now() - r.lastDomAt < 3000) continue;
          r.lastDom = t; r.lastDomAt = Date.now();
          if (r.dom.length < 200) r.dom.push('screen: ' + t);
        }
      });
      W.__recObs.observe(document.body, {childList: true, subtree: true, characterData: false});
    }
    if (cmd === 'start') return out;
  }

  if (cmd === 'stop') {
    W.__recOn = false;
    const g = bag(), gained = {}, lost = {};
    for (const k of new Set([...Object.keys(g), ...Object.keys(R.bag0)])) {
      const d = (g[k] || 0) - (R.bag0[k] || 0);
      if (d > 0) gained[k] = d; else if (d < 0) lost[k] = -d; }
    const mins = ((now - R.t0) / 60000).toFixed(1);
    out.push(`STOP after ${mins} min | map ${w.mapIndex} pos (${pos.x},${pos.y}) lvl ${p.level} | zen ${(p.money || 0) - (R.money0 || 0) >= 0 ? '+' : ''}${(p.money || 0) - (R.money0 || 0)}`);
    out.push('summary monsters seen: ' + short(R.mobTotal, 1500));
    out.push('summary monsters died: ' + short(R.kills, 1500));
    out.push('summary items gained: ' + short(gained, 800) + ' | lost: ' + short(lost, 800));
    out.push('summary events: ' + short(Object.entries(R.evCount).sort((a, b) => b[1] - a[1]), 3000));
    W.__rec = null;
    return out;
  }

  // ── tick ──
  out.push(...R.dom.splice(0));
  // chat
  const lines = (soc && soc.chatLines) || [];
  if (lines.length && typeof lines[lines.length - 1] === 'object') {
    for (const l of lines) if (l && typeof l === 'object' && !R.chatSeen.has(l)) { R.chatSeen.add(l); out.push('chat: ' + short(chatTxt(l), 300)); }
  } else if (lines.length !== R.chatStrLen) {
    lines.slice(Math.max(0, Math.min(R.chatStrLen, lines.length - 1))).forEach(l => out.push('chat: ' + short(chatTxt(l), 300)));
    R.chatStrLen = lines.length;
  }
  // event panel: a row is logged when its text changes (ignoring the running clocks)
  for (const r of evRows()) {
    const key = r.replace(/[^A-Za-z ]+.*$/, '').trim() || r;
    const norm = r.replace(/\d+:\d+(:\d+)?/g, '#');
    if (R.rows[key] !== norm) { R.rows[key] = norm; out.push('event panel: ' + r); }
  }
  if (now - R.rowsAt > 60000) { R.rowsAt = now; const rows = evRows(); if (rows.length) out.push('event panel (1 min): ' + rows.join(' | ')); }
  // per-minute event counts
  if (now - R.evMinAt > 60000) {
    R.evMinAt = now;
    const e = Object.entries(R.evMin).sort((a, b) => b[1] - a[1]);
    if (e.length) out.push('events last minute: ' + short(e, 1500));
    R.evMin = {};
  }
  // me
  if (w.mapIndex !== R.map) { out.push(`MAP ${R.map} -> ${w.mapIndex} at (${pos.x},${pos.y})`); R.map = w.mapIndex; R.mobs.clear(); R.drops.clear(); }
  if ((pos.x !== R.lastPos.x || pos.y !== R.lastPos.y) && now - R.lastPosAt >= POS_EVERY * 1000) {
    out.push(`pos (${pos.x},${pos.y})`); R.lastPos = pos; R.lastPosAt = now; }
  const hp = p.currentHP;
  if (hp <= 0 && R.hp > 0) out.push(`DIED at (${pos.x},${pos.y})`);
  else if (hp > 0 && R.hp <= 0) out.push(`alive again at (${pos.x},${pos.y})`);
  R.hp = hp;
  const helper = !!(s.muHelper && s.muHelper.active);
  if (helper !== R.helper) { out.push('MU Helper ' + (helper ? 'ON' : 'OFF')); R.helper = helper; }
  if (p.money !== R.money) { const d = (p.money || 0) - (R.money || 0); out.push(`zen ${d >= 0 ? '+' : ''}${d} -> ${p.money}`); R.money = p.money; }
  // bag
  const b = bag();
  for (const k of new Set([...Object.keys(b), ...Object.keys(R.bag)])) {
    const d = (b[k] || 0) - (R.bag[k] || 0);
    if (d) out.push(`bag ${d > 0 ? '+' : ''}${d} ${k}`);
  }
  R.bag = b;
  // monsters and ground items in range
  const seen = new Set(), seenDrops = new Set();
  for (const e of ents) {
    if (e.localPlayer) continue;
    const d = near(e);
    if (d > R_RADIUS) continue;
    const x = Math.round(e.transform.pos.x), y = Math.round(e.transform.pos.z);
    if (e.droppedItem) {
      seenDrops.add(e.netId);
      if (!R.drops.has(e.netId)) {
        const it = e.droppedItem;
        R.drops.set(e.netId, 1);
        out.push(`ground + ${it.isMoney ? 'zen' : (e.objectNameInWorld || '?')} ${it.isMoney ? '' : `[${it.group}/${it.num}${it.item && it.item.lvl ? ' +' + it.item.lvl : ''}]`} at (${x},${y}) ${Math.round(d)} tiles`);
      }
      continue;
    }
    if (e.npcType === undefined || !e.monsterAnimation) continue;
    seen.add(e.netId);
    const name = e.objectNameInWorld || ('npc ' + e.npcType);
    const m = R.mobs.get(e.netId);
    if (!m) {
      R.mobs.set(e.netId, {name, x, y, dying: !!e.dying});
      R.mobTotal[name] = (R.mobTotal[name] || 0) + 1;
      out.push(`mob + ${name} [type ${e.npcType} #${e.netId}] at (${x},${y}) ${Math.round(d)} tiles`);
      if (e.dying) { R.kills[name] = (R.kills[name] || 0) + 1; out.push(`mob DIED ${name} #${e.netId} at (${x},${y})`); }
      if (!R.types.has(e.npcType)) { R.types.add(e.npcType); out.push(`mob type ${e.npcType} (${name}) fields: ` + short(prim(e, 30), 1500)); }
    } else {
      m.x = x; m.y = y;
      if (e.dying && !m.dying) { m.dying = true; R.kills[name] = (R.kills[name] || 0) + 1; out.push(`mob DIED ${name} #${e.netId} at (${x},${y})`); }
    }
  }
  for (const [id, m] of R.mobs) if (!seen.has(id)) {
    R.mobs.delete(id);
    if (!m.dying) out.push(`mob - ${m.name} #${id} left view at (${m.x},${m.y})`);
  }
  for (const id of [...R.drops.keys()]) if (!seenDrops.has(id)) R.drops.delete(id);
  return out;
}
"""


class Recorder:
    """Records one game window while you play it. Never raises: errors are written to the record."""

    def __init__(self, page, who, main_log=None):
        self.page, self.who, self.main_log = page, who, main_log
        self.on = False
        self.last = 0
        self.lines = 0

    def _write(self, lines):
        if not lines:
            return
        stamp = f"{datetime.now():%Y-%m-%d %H:%M:%S}"
        with _lock:
            with RECORD_FILE.open("a", encoding="utf-8") as f:
                for ln in lines:
                    f.write(f"[{stamp}] [{self.who}] {ln}\n")
        self.lines += len(lines)

    def _run(self, cmd):
        try:
            return self.page.evaluate(REC_JS, [cmd, RECORD_RADIUS, RECORD_POS_EVERY]) or []
        except Exception as e:
            return [f"(recorder error: {str(e)[:200]})"]

    def start(self):
        if not RECORD_WHEN_PAUSED or self.on:
            return
        self.on, self.last, self.lines = True, time.time(), 0
        self._write(self._run("start"))
        if self.main_log:
            self.main_log(f"recording your play to {RECORD_FILE.name}")

    def tick(self):
        if not self.on or time.time() - self.last < RECORD_EVERY:
            return
        self.last = time.time()
        try:
            if self.page.is_closed():
                return
        except Exception:
            return
        self._write(self._run("tick"))

    def stop(self):
        if not self.on:
            return
        self.on = False
        self._write(self._run("stop"))
        if self.main_log:
            self.main_log(f"recording stopped ({self.lines} lines in {RECORD_FILE.name})")

# mu_bot

Bots for the MU Online browser client at https://mu.mormon.garden (Playwright + Chromium).

- `mu_auto_bot.py` / `run_auto.bat` - **main bot**: every character levels, resets and runs Blood Castle on its own; alts trade their Boxes of Kundun to the main; leveling pairs for grand-reset characters (see below)
- `mu_group_bot.py` / `run_group.bat` - party bot that levels several characters together
- `mu_bc.py` - Blood Castle logic (used by both bots)
- `mu_gi.py` - Golden Invasion hunter (used by the auto bot): every hour at :50 wizzardo tops up potions in Noria, hunts golden mobs for 3 minutes (players first at a Golden Dragon), then `/pkclear`
- `mu_recorder.py` - records what you do while a character is paused (`mu_record.log`)
- `mu_bot.py` / `run.bat` - old single-character reset bot (not maintained)

Only one bot can run at a time.

## Setup on Windows
1. Copy `accounts.example.json` to `accounts.json` and fill in your accounts (first entry = main character / party leader).
2. Double-click `run_auto.bat` (or `run_group.bat`). The first run installs Python, Playwright and Chromium.

`accounts.json`, browser profiles and logs are git-ignored.

## Setup on macOS
1. Install Python 3.10 or newer from [python.org](https://www.python.org/downloads/macos/) or with Homebrew (`brew install python`).
2. In Terminal, go to this project folder and run `chmod +x run_auto.sh run_group.sh run.sh restart_bot.sh` once.
3. Copy `accounts.example.json` to `accounts.json` and fill in your accounts.
4. Start the main bot with `./run_auto.sh`, or the party bot with `./run_group.sh`. The first run installs Playwright and its Chromium browser.

Use `./restart_bot.sh` to restart the supervised auto bot, or create a `RESTART` file in this folder. `STOP_ALL` stops the supervisor. `PAUSE` pauses all characters in either bot; delete it to resume. The auto bot also accepts `P`, `B`, `R`, and `1`-`5` in the Terminal window; the group bot accepts `P`.

## Controls (auto bot)
- `P` in the console pauses / resumes everyone, `1`-`5` pause / resume one character (order of `accounts.json`) so you can play it.
- `B` in the console switches Blood Castle on / off. It starts as `BC_AUTO` near the top of `mu_auto_bot.py` (currently off). Switching off drops anyone who hasn't entered a castle yet; anyone already inside finishes it.
- Restart to load code changes, but not between :45 and :10 (Blood Castle time).

## Idle wiggle
While a character only waits (Lorencia bar before Blood Castle or while its leveler is away, by the Spirit in Devias, by the bridge inside the castle, at the Golden Invasion waiting spot) it takes a random 1-2 tile step every 10-25 s, staying within 2 tiles of the spot. In Devias it stops 15 s before the door opens and stands on the spot for the click. Settings: `IDLE_WIGGLE*` near the top of `mu_auto_bot.py` (`IDLE_WIGGLE = False` turns it off).

## Leveling pairs (grand resets)
Grand resets are done by hand. To have another character carry a grand-reset character, edit `PAIRS` near the top of `mu_auto_bot.py`:

```python
PAIRS = {
    "levelee": "leveler",
}
```

Until the levelee has `ASSIST_UNTIL_RESETS` (5) resets:
- both farm together in their own party, at the zone for the lower of their two levels, and reset together;
- the levelee skips Blood Castle and waits at the Lorencia bar while the leveler is in Blood Castle, paused or offline;
- the levelee puts its free points into VIT (`/add vit <amount>`) up to `LEVELEE_VIT_MAX` (1500).

At 5 resets the pair splits by itself; the next grand reset (detected when the reset counter drops to 0) starts it again.

## Several machines (roster.json)

Each machine's `accounts.json` lists only the logins that machine runs. `roster.json` (committed, the same on every machine) lists **all** our characters and which machine runs each. Everything that asks "is this player one of ours?" uses it: Golden Invasion never fights them, Blood Castle doesn't treat them as strangers, scouting doesn't count them as crowd, chat replies ignore them. Add a new character to `roster.json` before running it. The startup log prints `roster: ...` and warns about characters missing from it. (Phase 0: the machines don't share levels or positions yet — see the multi-machine plan.)

### Shared hub (hub.json)

With a `hub.json` (git-ignored, template `hub.example.json`) the bots also share live state: the PC bot runs a small server (`mu_hub.py`, port 47300), every bot sends its characters' level/map/position/mode every 3 s, and Blood Castle castles are **claimed** at the hub so the PC and the Mac never send two characters to the same castle (another machine's claim makes the planner pick the next castle). Hub unreachable -> each machine plans only among its own characters. No `hub.json` -> the machine runs alone, as before.

- PC: `"serve": true`, url `http://127.0.0.1:47300`. On first start Windows Firewall asks about Python: allow **Private networks**. The startup log prints the address the Mac must use.
- Mac: `"serve": false`, `"machine": "mac"`, url `http://<PC LAN IP>:47300`, same `key`. Check with `python3 mu_hub.py test` (the VPN must allow local-network access).
- Status page: `http://<PC LAN IP>:47300/?key=<key>` in any browser on the network. The 5-minute summary in the log adds a `hub:` line with the other machine's characters.

## Restarting without the console (supervisor)

`run_auto.bat` on Windows or `./run_auto.sh` on macOS starts `mu_supervisor.py`, which runs `mu_auto_bot.py` in the same window (P / B / 1-5 still work) and logs to `supervisor.log`.

- `restart_bot.bat` (Windows) or `./restart_bot.sh` (macOS), or `R` in the console: restart right away. If the bot isn't running, the helper starts it.
- Empty file named `RESTART` in this folder: restart at the next safe time (only between :10 and :45). `RESTART` containing `now`: right away.
- File named `STOP_ALL`: stop the bot and the supervisor.
- On its own: every `.py` is compiled before a (re)start (broken code -> the running bot is left alone), a crash restarts it after 30 s (4 crashes in 15 min -> it waits for a `RESTART` file), and a bot whose log is silent for 10 min is killed and restarted.

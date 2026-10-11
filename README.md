# mu_bot

Bots for the MU Online browser client at https://mu.mormon.garden (Playwright + Chromium).

- `mu_auto_bot.py` / `run_auto.bat` - **main bot**: every character levels, resets and runs Blood Castle on its own; alts trade their Boxes of Kundun to the main; leveling pairs for grand-reset characters (see below)
- `mu_group_bot.py` / `run_group.bat` - party bot that levels several characters together
- `mu_bc.py` - Blood Castle logic (used by both bots)
- `mu_gi.py` - Golden Invasion hunter (used by the auto bot): every hour at :50 wizzardo tops up potions in Noria, hunts golden mobs for 3 minutes (players first at a Golden Dragon), then `/pkclear`
- `mu_recorder.py` - records what you do while a character is paused (`mu_record.log`)
- `mu_bot.py` / `run.bat` - old single-character reset bot (not maintained)

Only one bot can run at a time.

## Setup
1. Copy `accounts.example.json` to `accounts.json` and fill in your accounts (first entry = main character / party leader).
2. Double-click `run_auto.bat` (or `run_group.bat`). The first run installs Python, Playwright and Chromium.

`accounts.json`, browser profiles and logs are git-ignored.

## Controls (auto bot)
- `P` in the console pauses / resumes everyone, `1`-`5` pause / resume one character (order of `accounts.json`) so you can play it.
- `B` in the console switches Blood Castle on / off. It starts as `BC_AUTO` near the top of `mu_auto_bot.py` (currently off). Switching off drops anyone who hasn't entered a castle yet; anyone already inside finishes it.
- Restart to load code changes, but not between :45 and :10 (Blood Castle time).

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

## Restarting without the console (supervisor)

`run_auto.bat` starts `mu_supervisor.py`, which runs `mu_auto_bot.py` in the same window (P / B / 1-5 still work) and logs to `supervisor.log`.

- `restart_bot.bat` (double-click) or `R` in the console: restart right away. If the bot isn't running, `restart_bot.bat` starts it.
- Empty file named `RESTART` in this folder: restart at the next safe time (only between :10 and :45). `RESTART` containing `now`: right away.
- File named `STOP_ALL`: stop the bot and the supervisor.
- On its own: every `.py` is compiled before a (re)start (broken code -> the running bot is left alone), a crash restarts it after 30 s (4 crashes in 15 min -> it waits for a `RESTART` file), and a bot whose log is silent for 10 min is killed and restarted.

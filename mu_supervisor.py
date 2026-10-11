"""
Keeps the MU bot running and lets it be restarted WITHOUT touching its console
(so Claude, or you from another program, can restart it by creating a file).

run_auto.bat starts this script; it starts mu_auto_bot.py (or the script given as argument)
in the same console window, so the bot's keys (P, B, 1-5) keep working as before.

Control files (create them in the bot folder; their content is optional):
  RESTART           restart the bot at the next safe moment: never between :45 and :10
                    (Blood Castle, Golden Invasion, post-reset Zen Chest buys)
  RESTART  "now"    restart right away (file content = the word now)
  STOP_ALL          stop the bot AND this supervisor (window closes)
  restart_bot.bat   does "RESTART now" for you (double-click); starts the bot if it isn't running

On its own it also:
  * restarts the bot after a crash (after CRASH_MAX crashes in CRASH_WINDOW it waits for a RESTART file)
  * restarts a frozen bot (its log silent for HANG_MIN minutes)
  * compiles every .py first: broken code -> the running bot is left alone / it waits for a fix,
    and the error goes to supervisor.log
Stopping a bot: STOP_BOT file -> the bot closes its browsers and exits; after GRACE_SEC it is
killed together with its browsers (taskkill /T).
Everything is logged to supervisor.log.
"""
import os
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent
BOT = sys.argv[1] if len(sys.argv) > 1 else "mu_auto_bot.py"
BOT_LOG = BASE / (Path(BOT).stem + ".log")
LOG = BASE / "supervisor.log"
RESTART_FILE = BASE / "RESTART"
STOP_ALL_FILE = BASE / "STOP_ALL"
STOP_BOT_FILE = BASE / "STOP_BOT"     # read by the bot: close cleanly and exit

LOCK_PORT = 47292                     # one supervisor at a time (the bots use 47291)
SAFE_MINUTES = (10, 45)               # a normal RESTART only happens when :10 <= minute < :45
GRACE_SEC = 90                        # time the bot gets to close itself before it is killed
CRASH_WAIT = 30                       # seconds before restarting after a crash
CRASH_MAX, CRASH_WINDOW = 4, 900      # this many crashes in this many seconds -> stop restarting
HANG_MIN = 10                         # bot log silent this long -> frozen -> restart
ANOTHER_BOT_RC = 2                    # the bot's exit code when another bot holds its lock


def log(msg):
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] [supervisor] {msg}"
    print(line, flush=True)
    try:
        with LOG.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def take(path):
    """Read and delete a control file. None if it doesn't exist."""
    try:
        text = path.read_text(encoding="utf-8", errors="ignore").strip().lower()
    except OSError:
        return None
    try:
        path.unlink()
    except OSError:
        pass
    return text


def compile_all():
    """None if every .py in the folder compiles, else the first error (nothing is written to disk)."""
    for f in sorted(BASE.glob("*.py")):
        try:
            compile(f.read_bytes(), str(f), "exec")
        except (SyntaxError, ValueError) as e:
            return f"{f.name}: {e}"
    return None


def safe_now():
    return SAFE_MINUTES[0] <= datetime.now().minute < SAFE_MINUTES[1]


def start_bot():
    try:
        STOP_BOT_FILE.unlink()
    except OSError:
        pass
    env = dict(os.environ, MU_SUPERVISED="1")
    p = subprocess.Popen([sys.executable, BOT], cwd=str(BASE), env=env)
    log(f"started {BOT} (pid {p.pid})")
    return p


def kill_tree(p):
    """Kill the bot and everything it started (Playwright driver + Chromium windows)."""
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True)
    else:
        p.kill()
    try:
        p.wait(timeout=15)
    except subprocess.TimeoutExpired:
        pass


def log_age():
    try:
        return time.time() - BOT_LOG.stat().st_mtime
    except OSError:
        return None


def main():
    lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        lock.bind(("127.0.0.1", LOCK_PORT))
    except OSError:
        print("The supervisor is already running in another window. Create a RESTART file to restart the bot.")
        time.sleep(8)
        return
    log(f"=== supervisor starting ({BOT}) | RESTART file = restart at a safe time, "
        f"'now' inside = right away, STOP_ALL = stop everything ===")
    proc, started = None, 0.0
    pending = None            # restart asked: "now" / "safe"
    stopping = None           # (since, why, then): bot asked to close; then = "restart" / "exit"
    crashes, held = [], False # held: too many crashes / broken code -> wait for a RESTART file
    said = None               # last "waiting" message (logged once)

    def say_once(msg):
        nonlocal said
        if msg != said:
            said = msg
            log(msg)

    def ask_stop(why, then):
        nonlocal stopping
        log(f"stopping the bot ({why})")
        STOP_BOT_FILE.write_text("stop", encoding="utf-8")
        stopping = (time.time(), why, then)

    try:
        while True:
            # ── no bot running ──
            if proc is None:
                if take(STOP_ALL_FILE) is not None:
                    log("STOP_ALL file -> supervisor stops")
                    return
                if held:
                    if take(RESTART_FILE) is None:
                        time.sleep(2)
                        continue
                    held = False
                    crashes.clear()
                    log("RESTART file -> trying again")
                else:
                    take(RESTART_FILE)          # the bot is about to start anyway
                err = compile_all()
                if err:
                    say_once(f"NOT starting: the code does not compile -> {err} "
                             "(fix it, then it starts by itself)")
                    time.sleep(10)
                    continue
                said = None
                proc, started, pending, stopping = start_bot(), time.time(), None, None
                continue

            # ── the bot ended ──
            rc = proc.poll()
            if rc is not None:
                proc = None
                if stopping:
                    if stopping[2] == "exit":
                        log("bot closed -> supervisor stops")
                        return
                    log(f"bot closed (exit {rc}) -> starting it again")
                    continue
                if rc == 0:
                    log("bot stopped by you (Ctrl+C) -> supervisor stops too")
                    return
                if rc == ANOTHER_BOT_RC:
                    log("another MU bot is already running (not started by me) -> trying again in 60 s")
                    time.sleep(60)
                    continue
                now = time.time()
                crashes = [t for t in crashes if now - t < CRASH_WINDOW] + [now]
                if len(crashes) >= CRASH_MAX:
                    log(f"bot crashed (exit {rc}) {len(crashes)}x in {CRASH_WINDOW // 60} min -> NOT restarting; "
                        "create a RESTART file to try again")
                    held = True
                else:
                    log(f"bot crashed (exit {rc}) -> restarting in {CRASH_WAIT} s")
                    time.sleep(CRASH_WAIT)
                continue

            # ── the bot is running ──
            if stopping:
                if time.time() - stopping[0] > GRACE_SEC:
                    log(f"bot did not close within {GRACE_SEC} s -> killing it and its browsers")
                    kill_tree(proc)
                    stopping = (time.time(), stopping[1], stopping[2])
                time.sleep(1)
                continue
            if take(STOP_ALL_FILE) is not None:
                ask_stop("STOP_ALL file", "exit")
                continue
            req = take(RESTART_FILE)
            if req is not None:
                pending = "now" if "now" in req else "safe"
                log(f"RESTART file found ({'right away' if pending == 'now' else 'at the next safe time'})")
            if pending:
                if pending != "now" and not safe_now():
                    say_once(f"restart waiting: no restarts between :{SAFE_MINUTES[1]} and :{SAFE_MINUTES[0]:02d}")
                else:
                    err = compile_all()
                    if err:
                        log(f"NOT restarting: the new code does not compile -> {err} (the running bot keeps going)")
                    else:
                        said = None
                        ask_stop("restart requested", "restart")
                    pending = None
                    continue
            age = log_age()
            if time.time() - started > HANG_MIN * 60 and (age is None or age > HANG_MIN * 60):
                log(f"bot log silent for {HANG_MIN}+ min -> frozen, killing and restarting it")
                kill_tree(proc)
                stopping = (time.time(), "frozen", "restart")
                continue
            time.sleep(2)
    except KeyboardInterrupt:
        log("Ctrl+C -> supervisor stops")
        if proc and proc.poll() is None:
            try:
                proc.wait(timeout=30)       # the bot got the same Ctrl+C and closes itself
            except subprocess.TimeoutExpired:
                kill_tree(proc)


if __name__ == "__main__":
    main()

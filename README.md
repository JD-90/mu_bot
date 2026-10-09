# mu_bot

Reset bots for the MU Online browser client at https://mu.mormon.garden (Playwright + Chromium).

- `mu_bot.py` / `run.bat` - single-character reset bot
- `mu_group_bot.py` / `run_group.bat` - party bot that levels several characters together

## Setup
1. Copy `accounts.example.json` to `accounts.json` and fill in your accounts (first entry = party leader / single bot account).
2. Double-click `run.bat` or `run_group.bat`. The first run installs Python, Playwright and Chromium.

`accounts.json`, browser profiles and logs are git-ignored.

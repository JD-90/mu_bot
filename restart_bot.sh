#!/bin/sh
set -eu
cd "$(dirname "$0")"
if [ -f supervisor.log ] && lsof -nP -iTCP:47292 -sTCP:LISTEN >/dev/null 2>&1; then
  printf 'now\n' > RESTART
  echo "Restart requested; see supervisor.log."
else
  echo "The supervisor is not running; starting the auto bot in the background."
  nohup ./run_auto.sh >/dev/null 2>&1 </dev/null &
fi

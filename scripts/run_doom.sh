#!/bin/sh
# run_doom.sh - pause the POS app, run Doom for a limited time, resume the app.
#
# The USB shell lives inside the ISO process, so pausing ISO also freezes the
# shell. This script therefore runs detached from the shell, writes all output
# to a log file, and always resumes ISO at the end, which brings the shell back.
#
# Start it from the USB shell (detached):
#   setsid sh /data/doom/run_doom.sh 60 </dev/null >/dev/null 2>&1 &
# If BusyBox has no setsid:
#   nohup sh /data/doom/run_doom.sh 60 </dev/null >/dev/null 2>&1 &
#
# Argument: how many seconds Doom may run (default 30). Needed until the
# keypad works, because there is no way to quit Doom from the terminal yet.
# Afterwards, read the log:  cat /data/doom/doom.log

RUN_SECONDS=${1:-30}

DOOM_DIR=/data/doom
DOOM_BIN=./doomgeneric_pos
DOOM_ARGS="-iwad doom1.wad"
LOG=$DOOM_DIR/doom.log

# Processes to pause while Doom runs (full path as shown in /proc/<pid>/cmdline).
# Add /vendor/bin/pos-manage here only if the screen still gets overwritten.
# Leave pos-master and pedserver (PIN pad security server) running.
PAUSE_APPS="/home/user0/ISO/ISO"

GOV=/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor

cd "$DOOM_DIR" || exit 1

# All output to the log: if Doom printed to the shell's terminal while ISO is
# paused, nobody would read it and Doom would block once the buffer filled.
exec >"$LOG" 2>&1 </dev/null

echo "run_doom: start $(date), limit ${RUN_SECONDS}s"

# Find PIDs whose command line starts with the given path.
find_pids() {
    for p in /proc/[0-9]*; do
        cmd=$(tr '\0' ' ' < "$p/cmdline" 2>/dev/null)
        case "$cmd" in
            "$1"|"$1 "*) echo "${p#/proc/}" ;;
        esac
    done
}

PAUSED=""
OLD_GOV=$(cat "$GOV" 2>/dev/null)

resume() {
    for pid in $PAUSED; do
        kill -CONT "$pid" 2>/dev/null && echo "run_doom: resumed $pid"
    done
    PAUSED=""
    [ -n "$OLD_GOV" ] && echo "$OLD_GOV" > "$GOV" 2>/dev/null
    echo "run_doom: end $(date)"
}
trap resume EXIT
trap 'exit 1' INT TERM HUP

# Give the shell a moment to show its prompt before ISO freezes.
sleep 1

for app in $PAUSE_APPS; do
    for pid in $(find_pids "$app"); do
        kill -STOP "$pid" && PAUSED="$PAUSED $pid" && echo "run_doom: paused $pid ($app)"
    done
done

echo performance > "$GOV" 2>/dev/null

$DOOM_BIN $DOOM_ARGS &
DOOM_PID=$!
echo "run_doom: doom pid $DOOM_PID"

# Wait until Doom exits or the time limit is reached.
i=0
while kill -0 "$DOOM_PID" 2>/dev/null && [ "$i" -lt "$RUN_SECONDS" ]; do
    sleep 1
    i=$((i + 1))
done

if kill -0 "$DOOM_PID" 2>/dev/null; then
    echo "run_doom: time limit reached, stopping doom"
    kill -TERM "$DOOM_PID" 2>/dev/null
    sleep 2
    kill -KILL "$DOOM_PID" 2>/dev/null
fi
wait "$DOOM_PID" 2>/dev/null
echo "run_doom: doom exited with status $?"
# resume() runs on exit and brings ISO (and the USB shell) back
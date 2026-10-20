#!/bin/bash
# Start detached with: umask 077; nohup ./run.sh >/dev/null 2>&1 &
set -u
# Logs and supervisor state must be readable only by their owner.
umask 077
cd "$(dirname "$0")" || exit 1
exec >> relay.log 2>&1
parent=$(ps -o ppid= -p $$ 2>/dev/null) || parent=
parent=${parent//[[:space:]]/}
case "$parent" in
  ''|*[!0-9]*)
    echo "$(date '+%F %T') ps -o ppid= -p is not supported here (BusyBox ps?); install procps"
    exit 1
    ;;
esac
load_env() {
  local file writable exports rc
  file="${RELAY_ENV_FILE:-.env}"
  case "$file" in /*) ;; *) file="./$file" ;; esac
  if [ ! -e "$file" ]; then
    if [ -n "${RELAY_ENV_FILE:-}" ]; then
      echo "$(date '+%F %T') env file RELAY_ENV_FILE not found, exiting"
      exit 1
    fi
    return 0
  fi
  # Sourcing runs the file as shell, so only its owner may be able to change it.
  if [ ! -O "$file" ]; then
    echo "$(date '+%F %T') env file is not owned by you, refusing to source it, exiting"
    exit 1
  fi
  if ! writable=$(find "$file" -prune \( -perm -0020 -o -perm -0002 \) 2>/dev/null) || [ -n "$writable" ]; then
    echo "$(date '+%F %T') env file is group or world writable, refusing to source it (chmod 600), exiting"
    exit 1
  fi
  exports=$(
    exec 2>/dev/null # An error message can quote file content.
    set -e
    before=$(compgen -e)
    set -a
    # The env file is selected at runtime and checked above.
    # shellcheck disable=SC1090
    . "$file" >/dev/null
    set +a
    for name in $(compgen -e); do
      if [[ $'\n'"$before"$'\n' != *$'\n'"$name"$'\n'* ]]; then
        printf 'export %s=%q\n' "$name" "${!name}"
      fi
    done
  )
  rc=$?
  if [ "$rc" -ne 0 ]; then
    echo "$(date '+%F %T') env file failed to load, exiting"
    exit 1
  fi
  eval "$exports"
}
load_env
max=${HEARTBEAT_MAX_AGE-180}
case "$max" in
  ''|*[!0-9]*) echo "HEARTBEAT_MAX_AGE must be a positive integer"; exit 1 ;;
esac
max=$((10#$max))
[ "$max" -gt 0 ] || { echo "HEARTBEAT_MAX_AGE must be a positive integer"; exit 1; }
interval=$((max / 6)); [ "$interval" -ge 1 ] || interval=1
if ! state=$(python3 relay.py --state-dir); then
  echo "$(date '+%F %T') cannot resolve relay state directory, exiting"
  exit 1
fi
heartbeat="$state/heartbeat"
pidfile="$state/run.pid"
if ! ( set -C; echo $$ > "$pidfile" ) 2>/dev/null; then
  old_pid=$(cat "$pidfile" 2>/dev/null) || old_pid=
  command=
  case "$old_pid" in
    ''|*[!0-9]*) ;;
    *)
      if [ "$old_pid" != "$$" ] && [ "$old_pid" -gt 0 ] && kill -0 "$old_pid" 2>/dev/null; then
        command=$(ps -p "$old_pid" -o command= 2>/dev/null) || command=
      fi
      ;;
  esac
  case "$command" in
    *run.sh*)
      echo "$(date '+%F %T') run.sh already running (pid $old_pid), exiting"
      exit 1
      ;;
  esac
  echo "$(date '+%F %T') removing stale pidfile (pid $old_pid)"
  if ! rm -f "$pidfile" 2>/dev/null || ! ( set -C; echo $$ > "$pidfile" ) 2>/dev/null; then
    echo "$(date '+%F %T') cannot create pidfile run.pid, exiting"
    exit 1
  fi
fi
cleanup_pidfile() {
  # A replacement owner must keep its lock when this supervisor exits.
  if [ "$(cat "$pidfile" 2>/dev/null)" = "$$" ]; then
    rm -f "$pidfile" 2>/dev/null
  fi
}
trap cleanup_pidfile EXIT
pid=
sleep_pid=
interruptible_sleep() {
  # Bash can run signal traps promptly while waiting on a background job.
  sleep "$1" &
  sleep_pid=$!
  wait "$sleep_pid"
  sleep_pid=
}
stop_sleep() {
  [ -n "$sleep_pid" ] || return 0
  kill -TERM "$sleep_pid" 2>/dev/null || :
  wait "$sleep_pid" 2>/dev/null || :
  sleep_pid=
}
alive() {
  local parent
  # Bash may reap a child before wait, allowing its pid to belong to someone else.
  parent=$(ps -o ppid= -p "$pid" 2>/dev/null) || return 1
  parent=${parent//[[:space:]]/}
  [ "$parent" = "$$" ]
}
stop_child() {
  [ -n "$pid" ] || return 0
  alive || return 0
  kill -TERM "$pid" 2>/dev/null || :
  for ((n=0; n<5; n++)); do
    alive || break
    sleep 1
  done
  if alive; then
    kill -KILL "$pid" 2>/dev/null || :
  fi
}
trap 'stop_sleep; stop_child; [ -z "$pid" ] || wait "$pid"; exit 0' TERM INT
rotate_log() {
  if [ -f relay.log ] && [ "$(wc -c < relay.log)" -gt 5242880 ]; then
    # Append writers keep using this inode. Lines between copy and truncate can be lost.
    cp relay.log relay.log.1 && : > relay.log
  fi
}
delay=5
while true; do
  rotate_log || echo "$(date '+%F %T') log rotation failed, continuing"
  started=$(date +%s)
  # A previous run must not shorten this child's startup grace.
  # Group redirection errors too, so bash cannot expose the state directory.
  if ! { echo 0 > "$heartbeat"; } 2>/dev/null; then
    delay=$((delay * 2)); [ "$delay" -le 300 ] || delay=300
    echo "$(date '+%F %T') cannot write heartbeat, retrying in ${delay}s"
    interruptible_sleep "$delay"
    continue
  fi
  last=0
  idle=0
  python3 relay.py >> relay.log 2>&1 &
  pid=$!
  reason=
  while alive; do
    interruptible_sleep "$interval"
    rotate_log || echo "$(date '+%F %T') log rotation failed, continuing"
    alive || break
    beat=$(cat "$heartbeat" 2>/dev/null) || beat=
    # Sleep counts keep clock changes from hiding stalls or killing healthy children.
    # Wall-clock time is used only in logs and the coarse restart backoff heuristic.
    if [ "$beat" != "$last" ]; then
      last=$beat
      idle=0
    else
      idle=$((idle + interval))
    fi
    if [ "$idle" -gt "$max" ]; then
      echo "$(date '+%F %T') relay heartbeat stale (no progress for ${idle}s > ${max}s), killing pid $pid"
      reason='killed (heartbeat stale)'
      stop_child
      break
    fi
  done
  rc=0; wait "$pid" || rc=$?
  pid=
  if [ "$rc" -eq 3 ]; then
    echo "$(date '+%F %T') relay.lock is held by another relay, probably an orphan left by a killed supervisor; stop that process to let this supervisor take over"
  fi
  # A clock jump can only change the backoff length.
  if [ "$(($(date +%s) - started))" -lt 60 ]; then
    delay=$((delay * 2)); [ "$delay" -le 300 ] || delay=300
  else
    delay=5
  fi
  echo "$(date '+%F %T') relay ${reason:-exited (rc=$rc)}, restarting in ${delay}s"
  interruptible_sleep "$delay"
done

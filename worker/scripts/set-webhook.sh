#!/usr/bin/env bash
# Set or delete a Telegram webhook using exported configuration, without exposing credentials in argv.
set -euo pipefail
umask 077
fail() { printf '%s\n' "$1" >&2; exit 2; }
required() { [[ -n "${!1:-}" ]] || fail "$1"; }
mode="${1:-}"
[[ $# == 1 && ( "$mode" == set || "$mode" == delete ) ]] || fail 'usage: set-webhook.sh set|delete'
required TELEGRAM_BOT_TOKEN
# Reject curl-config metacharacters so an environment value cannot inject config lines.
[[ "$TELEGRAM_BOT_TOKEN" =~ ^[0-9]+:[A-Za-z0-9_-]+$ ]] || fail TELEGRAM_BOT_TOKEN
if [[ "$mode" == set ]]; then
  for name in WORKER_URL WEBHOOK_PATH_SECRET TELEGRAM_WEBHOOK_SECRET; do required "$name"; done
  [[ "$WORKER_URL" == https://* ]] || fail WORKER_URL
  python3 -c 'import os,sys,urllib.parse
u=urllib.parse.urlsplit(os.environ["WORKER_URL"])
sys.exit(0 if u.scheme=="https" and u.hostname and not u.username and not u.password and not u.path and not u.query and not u.fragment else 2)' || fail WORKER_URL
  # macOS Bash regex repetition bounds stop at 255, so check the 256-character limit separately.
  [[ "$WEBHOOK_PATH_SECRET" =~ ^[A-Za-z0-9_-]+$ && ${#WEBHOOK_PATH_SECRET} -ge 16 && ${#WEBHOOK_PATH_SECRET} -le 256 ]] || fail WEBHOOK_PATH_SECRET
  [[ "$TELEGRAM_WEBHOOK_SECRET" =~ ^[A-Za-z0-9_-]+$ && ${#TELEGRAM_WEBHOOK_SECRET} -le 256 ]] || fail TELEGRAM_WEBHOOK_SECRET
  [[ "$WEBHOOK_PATH_SECRET" != "$TELEGRAM_WEBHOOK_SECRET" ]] || fail WEBHOOK_PATH_SECRET
  [[ "${FORWARD_EDITS:-0}" == 0 || "${FORWARD_EDITS:-0}" == 1 ]] || fail FORWARD_EDITS
fi
body=$(mktemp)
reply=''
trap 'rm -f -- "$body" ${reply:+"$reply"}' EXIT
reply=$(mktemp)
if [[ "$mode" == set ]]; then
  method=setWebhook
  python3 -c 'import json,os
print(json.dumps({"url":os.environ["WORKER_URL"]+"/telegram/"+os.environ["WEBHOOK_PATH_SECRET"],"secret_token":os.environ["TELEGRAM_WEBHOOK_SECRET"],"allowed_updates":["message"]+(["edited_message"] if os.environ.get("FORWARD_EDITS","0")=="1" else []),"max_connections":1}))' >"$body"
else
  method=deleteWebhook
  printf '{}\n' >"$body"
fi
# stdin config keeps the token off argv; the private temporary file does the same for the body.
# Suppress curl stderr because its error text can expose the bot token in the request URL.
rc=0
curl -sS --max-time 30 --config - --header 'Content-Type: application/json' --data-binary "@$body" --output "$reply" 2>/dev/null <<CURL_CONFIG || rc=$?
url = "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/${method}"
CURL_CONFIG
if [[ $rc != 0 ]]; then
  printf 'Telegram request failed (curl exit %s)\n' "$rc" >&2
  exit 1
fi
python3 -c 'import json,sys
try:
    with open(sys.argv[1]) as f: result=json.load(f)
    if not isinstance(result,dict): raise ValueError()
except (ValueError,OSError):
    print("Invalid Telegram response",file=sys.stderr);sys.exit(1)
if result.get("ok") is not True:
    print(result.get("description","Telegram request failed"),file=sys.stderr);sys.exit(1)' "$reply"
if [[ "$mode" == set ]]; then
  printf 'webhook set; getUpdates is now disabled for this bot, stop the polling relay\n'
else
  printf 'webhook deleted; the polling relay can run again\n'
fi

# Telegram bot relay Worker

Telegram calls this Worker as soon as a message arrives. An agent whose machine
sleeps between tasks can still receive updates immediately through its HTTPS
inbox. Telegram cannot add the bearer header that inbox needs, so the Worker
adds it. The Worker never holds the bot token.

[![Deploy to Cloudflare](https://deploy.workers.cloudflare.com/button)](https://deploy.workers.cloudflare.com/?url=https://github.com/oscarsterling/telegram-bot-relay/tree/main/worker)

The button creates a copy of this folder in your own GitHub account and deploys it.
It works only with public GitHub or GitLab repositories. This worker/ folder is self-contained:
Cloudflare treats the subdirectory as the root of the new repository. See the
[Deploy button docs](https://developers.cloudflare.com/workers/platform/deploy-buttons/).

## Deploy by hand

From this folder, run:

```sh
npm install
npx wrangler login
```

Set ALLOWED_CHAT_ID and OWNER_USER_ID as secrets using the commands below.
Values must be integers. A repository created by the Deploy button, or a fork,
may be public, and anything in wrangler.toml is committed. Secrets also survive
later deploys, while [vars] are overwritten by each deploy. Keep the generated
repository private anyway. Optional ALLOWED_SENDER_IDS is a comma-separated list
of integers, empty allows all. Set it later with
`npx wrangler secret put ALLOWED_SENDER_IDS`; it is omitted from .dev.vars.example
so the Deploy form does not demand a value.
Optional vars: REPLY_CAP (10, at least 1), HISTORY_SIZE (20, at least 0), HISTORY_MAX_CHARS
(1000, at least 1), FORWARD_EDITS (0 or 1, default 0), FORWARD_MEDIA (0 or 1,
default 1). Values are trimmed. Floats and exponential notation are rejected.
The owner always passes the sender filter and may also use private chats.

Generate each random secret separately with `openssl rand -hex 32`. Keep them
different. The path secret appears in request URLs; the header secret must not.

```sh
npx wrangler secret put TELEGRAM_WEBHOOK_SECRET
npx wrangler secret put WEBHOOK_PATH_SECRET
npx wrangler secret put AGENT_WEBHOOK_KEY
npx wrangler secret put SENT_API_KEY
npx wrangler secret put ALLOWED_CHAT_ID
npx wrangler secret put OWNER_USER_ID
npx wrangler secret put TELEGRAM_WEBHOOK_URL
npx wrangler deploy
```

TELEGRAM_WEBHOOK_SECRET is Telegram's secret_token (1-256 letters, digits,
underscores or hyphens). WEBHOOK_PATH_SECRET is a separate random value of
16-256 such characters. SENT_API_KEY is a separate required random key of
16-256 such characters, used only for POST /sent. Of the Worker's keys, give the agent only SENT_API_KEY.
AGENT_WEBHOOK_KEY belongs to the inbox platform and the Worker, never in the
agent model's context or tools. SENT_API_KEY must differ from AGENT_WEBHOOK_KEY
and both webhook secrets. AGENT_WEBHOOK_KEY must also differ from both webhook
secrets. TELEGRAM_WEBHOOK_URL is the agent inbox URL, HTTPS with a
hostname. Missing or invalid config logs its name and returns 500.
For local development copy .dev.vars.example to .dev.vars and fill it privately.

## Switch Telegram

Stop the polling relay first. Export TELEGRAM_BOT_TOKEN, WORKER_URL (the HTTPS
Worker base URL without a trailing slash or path), WEBHOOK_PATH_SECRET and
TELEGRAM_WEBHOOK_SECRET into the shell, then run:

```sh
scripts/set-webhook.sh set
```

Set FORWARD_EDITS=1 as well if edits should be delivered. Once a webhook is
set, getUpdates stops working, so the polling relay must not run. To switch
back, export TELEGRAM_BOT_TOKEN and run `scripts/set-webhook.sh delete`.
The helper keeps credentials and the JSON body off process command lines.

## Record the agent's replies

After sendMessage succeeds, the agent should POST its returned message id and
date with ONLY SENT_API_KEY as its bearer key. Example, with the key in an environment
variable and WORKER_HOST as a placeholder:

```sh
curl -X POST https://WORKER_HOST/sent \
  -H "Authorization: Bearer $SENT_API_KEY" \
  -H 'Content-Type: application/json' \
  --data '{"chat_id":-123,"message_id":42,"date":1792454400,"text":"Hello"}'
```

The three numeric fields must be integers and text must be a string. Only the
allowed group and owner's private chat are accepted. A repeated message_id
replaces the previous sent entry. Sent replies merge into forward history as
`"self": true`; an existing received entry wins an id clash. They are never
stored in received history.

### Routine prompt changes for the Worker

The top-level ROUTINE_PROMPT.md applies, except replace its SENT_LOG append
paragraph with this paragraph to paste into the routine prompt:

> After sendMessage returns ok, POST {"chat_id": result.chat.id, "message_id": result.message_id, "date": result.date, "text": result.text} to https://WORKER_HOST/sent with Authorization: Bearer SENT_API_KEY. Never print SENT_API_KEY, and treat it like the bot token.

## Behaviour and limits

The relay's allow rule, cap, quotes, media ids and history apply. Capped and
non-text messages enter history. A new owner text, caption or enabled media
resets the cap; an edit does not. Only successful forwards use a cap slot.
History defaults to 20 messages, with 1000 characters per text plus `...` on
truncation. HISTORY_SIZE must be at most 100, HISTORY_MAX_CHARS at most 4000,
and their product at most 100000. All state shares one storage value. Three
texts per entry (received, quote and sent) at 4 bytes per code point use at most
1.2 MB, plus about 300 KB of overhead at 100 entries. This stays below the
[2 MB key-and-value limit](https://developers.cloudflare.com/durable-objects/platform/limits/).
HISTORY_SIZE=0 omits both history fields. Media is never downloaded.

Telegram owns retries: it redelivers non-2xx responses for "a reasonable amount
of attempts" and then gives up. The attempt count and timing are undocumented,
so loss remains possible. FORWARD_RETRIES, Retry-After and the polling stall
watchdog do not apply here. A 429, 5xx, network error or timeout returns 500;
other failures, including all redirects, are logged and deliberately skipped.
A timeout after acceptance can redeliver an update, so the agent must still
de-duplicate on update_id. The Worker keeps the last 200 update_ids per chat.
Telegram's max_connections=1 plus a per-chat promise queue serialize deliveries.

State lives in Durable Object storage. Each chat keeps the last HISTORY_SIZE
sent entries (one entry when history is disabled). Sent text is truncated on
receipt and again at merge time. There is no corrupt-file handling because
there are no files. The Worker's own console lines omit text, URLs and
credentials. Cloudflare's request records (wrangler tail, Workers Logs when
observability is on, and Logpush) include the request URL, which contains
WEBHOOK_PATH_SECRET. Treat those logs as sensitive. If shared, rotate
WEBHOOK_PATH_SECRET and re-run `scripts/set-webhook.sh set`.

[Cloudflare's Free plan](https://developers.cloudflare.com/durable-objects/platform/pricing/)
includes Durable Objects with SQLite storage only: 100,000 requests/day,
13,000 GB-s/day, 5 million rows read/day, 100,000 rows written/day and 5 GB total
storage. Each forwarded update makes two requests, Worker plus DO. It updates
one state row, but this implementation puts it twice on success (before and
after delivery), so budget about two row writes per forwarded update. /sent also
writes that row. Figures checked against the linked pricing page on 2026-10-20;
check it again before you rely on them.

Every request to the Worker counts against the free-tier daily request limit,
including unauthenticated 401 or 404 hits. Once exhausted, Telegram deliveries
fail for the rest of the day and are dropped after Telegram's retries. Choose
a non-guessable Worker name in the Deploy form, or change `name` in wrangler.toml
for a hand deploy, instead of the default "telegram-bot-relay". For more
protection, use a custom domain with a Cloudflare rate-limiting rule and turn
off the workers.dev and preview URLs.

## Tests

Tests need Node 20 or newer. Deploying with wrangler 4.151.1 needs Node 22 or
newer (its engines requirement is >=22.0.0). No install is needed for tests:

```sh
npm test
```

The test script names the file explicitly because Node 24 does not accept
`node --test test/` as a directory test target. All outbound calls and Durable
Object storage are mocked; tests do not contact Telegram or Cloudflare.

## Security

The main repository README's SECURITY section also applies here. Only use
trusted groups. ALLOWED_SENDER_IDS filters triggers, not quoted or historical
content. The receiving platform must enforce scoped tools, private credentials,
fixed permitted destinations and owner approval outside the model. A prompt is
not a security boundary. Keep .dev.vars private and out of git. Do not share
logs without checking them. Report vulnerabilities privately.

Keep the webhook secrets, AGENT_WEBHOOK_KEY and SENT_API_KEY different.
To rotate a webhook secret, change its Worker secret,
update the helper's exported value, then re-run `scripts/set-webhook.sh set`.
Rotate a leaked bot token through BotFather. Rotate AGENT_WEBHOOK_KEY on both
the inbox platform and Worker. Rotate SENT_API_KEY on the Worker and in the
agent's tools, without exposing AGENT_WEBHOOK_KEY to the model. To switch back to polling, run `scripts/set-webhook.sh delete`,
then start the polling relay. The tests never contact Telegram or Cloudflare.

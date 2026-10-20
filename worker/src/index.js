// Log only event words, ids, statuses, counts and error names, never message text or credentials.
const PLAIN_NOTE = "Earlier messages in this chat, oldest first, excluding the current one; the receiving bot's own messages appear only when someone quoted them in a reply.";
const SENT_NOTE = 'Earlier messages in this chat, oldest first, excluding the current one; this bot\'s own messages come from its sent log and are marked "self": true.';
const MEDIA = ['photo', 'document', 'voice', 'audio', 'video'];
const TYPES = ['photo', 'sticker', 'document', 'voice', 'video', 'audio', 'animation', 'location', 'poll', 'contact'];
const empty = (status = 200) => new Response(null, { status });
const object = x => x !== null && typeof x === 'object' && !Array.isArray(x);
const order = (a, b) => ((a.date || 0) - (b.date || 0)) || ((a.message_id || 0) - (b.message_id || 0));

export async function safeEqual(a, b) {
  // Fixed-size hashes avoid leaking the input lengths during comparison.
  const hash = s => crypto.subtle.digest('SHA-256', new TextEncoder().encode(s));
  const [aHash, bHash] = await Promise.all([hash(a), hash(b)]);
  const left = new Uint8Array(aHash), right = new Uint8Array(bHash);
  let acc = 0;
  for (let i = 0; i < 32; i++) acc |= left[i] ^ right[i];
  return acc === 0;
}

function invalid(name) { console.error('config invalid', name); throw new Error('ConfigInvalid'); }
function value(env, name, fallback) {
  const raw = env[name] ?? fallback;
  if (typeof raw !== 'string') invalid(name);
  return raw.trim();
}
function sentApiKey(env) {
  const key = value(env, 'SENT_API_KEY');
  if (!/^[A-Za-z0-9_-]{16,256}$/.test(key)) invalid('SENT_API_KEY');
  return key;
}
function authConfig(env) {
  const out = {};
  for (const name of ['TELEGRAM_WEBHOOK_SECRET', 'WEBHOOK_PATH_SECRET']) {
    const v = value(env, name);
    if (!/^[A-Za-z0-9_-]{1,256}$/.test(v) || (name === 'WEBHOOK_PATH_SECRET' && v.length < 16)) invalid(name);
    out[name] = v;
  }
  if (out.WEBHOOK_PATH_SECRET === out.TELEGRAM_WEBHOOK_SECRET) invalid('WEBHOOK_PATH_SECRET');
  return out;
}
export function config(env, auth = authConfig(env)) {
  const out = { ...auth };
  for (const name of ['AGENT_WEBHOOK_KEY', 'SENT_API_KEY', 'TELEGRAM_WEBHOOK_URL']) {
    out[name] = name === 'SENT_API_KEY' ? sentApiKey(env) : value(env, name);
    if (!out[name]) invalid(name);
  }
  // A leaked /sent key must only be able to add "self" history entries.
  if ([out.AGENT_WEBHOOK_KEY, out.TELEGRAM_WEBHOOK_SECRET, out.WEBHOOK_PATH_SECRET].includes(out.SENT_API_KEY)) invalid('SENT_API_KEY');
  if ([out.TELEGRAM_WEBHOOK_SECRET, out.WEBHOOK_PATH_SECRET].includes(out.AGENT_WEBHOOK_KEY)) invalid('AGENT_WEBHOOK_KEY');
  try {
    const url = new URL(out.TELEGRAM_WEBHOOK_URL);
    if (url.protocol !== 'https:' || !url.hostname) invalid('TELEGRAM_WEBHOOK_URL');
  } catch (err) {
    if (err.message === 'ConfigInvalid') throw err;
    invalid('TELEGRAM_WEBHOOK_URL');
  }
  for (const [name, fallback, min] of [
    ['ALLOWED_CHAT_ID', undefined, null], ['OWNER_USER_ID', undefined, null],
    ['REPLY_CAP', '10', 1], ['HISTORY_SIZE', '20', 0], ['HISTORY_MAX_CHARS', '1000', 1],
    ['FORWARD_EDITS', '0', 0], ['FORWARD_MEDIA', '1', 0],
  ]) {
    const raw = value(env, name, fallback), n = Number(raw);
    if (!/^-?\d+$/.test(raw) || !Number.isSafeInteger(n) || (min !== null && n < min) || (name.startsWith('FORWARD_') && n > 1)) invalid(name);
    out[name] = n;
  }
  // 3 * 100000 * 4 bytes of text + about 300 KB overhead stays under 2 MB.
  // https://developers.cloudflare.com/durable-objects/platform/limits/
  if (out.HISTORY_SIZE > 100) invalid('HISTORY_SIZE');
  if (out.HISTORY_MAX_CHARS > 4000 || out.HISTORY_SIZE * out.HISTORY_MAX_CHARS > 100000) invalid('HISTORY_MAX_CHARS');
  const senders = value(env, 'ALLOWED_SENDER_IDS', '');
  out.ALLOWED_SENDER_IDS = new Set();
  if (senders) for (const item of senders.split(',')) {
    const s = item.trim(), n = Number(s);
    if (!/^-?\d+$/.test(s) || !Number.isSafeInteger(n)) invalid('ALLOWED_SENDER_IDS');
    out.ALLOWED_SENDER_IDS.add(n);
  }
  return out;
}
function messageText(message, limit) {
  const text = message.text || message.caption || `[${TYPES.find(k => k in message) || 'message'}]`;
  const chars = Array.from(text);
  return chars.length > limit ? chars.slice(0, limit).join('') + '...' : text;
}
function summary(message, limit) {
  const sender = message.from || {};
  return { message_id: message.message_id ?? null, from_id: sender.id ?? null,
    from_name: [sender.first_name, sender.last_name].filter(Boolean).join(' ') || sender.username || '',
    is_bot: sender.is_bot ?? false, text: messageText(message, limit) };
}
function mediaFileId(message) {
  for (const kind of MEDIA) if (kind in message) {
    let item = message[kind];
    if (kind === 'photo') item = Array.isArray(item) ? item.at(-1) : null;
    return typeof item?.file_id === 'string' && item.file_id ? item.file_id : null;
  }
  return null;
}
function historyEntry(message, cfg) {
  const entry = { ...summary(message, cfg.HISTORY_MAX_CHARS), date: message.date ?? null,
    reply_to_message_id: message.reply_to_message?.message_id ?? null };
  if (cfg.FORWARD_MEDIA) {
    const id = mediaFileId(message);
    if (id !== null) entry.file_id = id;
  }
  if (message.reply_to_message) entry.reply_to = summary(message.reply_to_message, cfg.HISTORY_MAX_CHARS);
  return entry;
}
function validSent(item) {
  return object(item) && ['chat_id', 'message_id', 'date'].every(k => Number.isInteger(item[k])) && typeof item.text === 'string';
}
async function json(request) {
  try { return await request.json(); } catch { return null; }
}
function chatStub(env, chatId) { return env.CHAT.get(env.CHAT.idFromName(String(chatId))); }
function internal(path, body) {
  return new Request('https://do' + path, { method: 'POST', body: JSON.stringify(body) });
}
export default {
  async fetch(request, env) {
    const path = new URL(request.url).pathname;
    if (path.startsWith('/telegram/')) {
      let auth;
      try { auth = authConfig(env); } catch { return empty(500); }
      const pathOK = await safeEqual(path.slice('/telegram/'.length), auth.WEBHOOK_PATH_SECRET);
      const headerOK = await safeEqual(request.headers.get('X-Telegram-Bot-Api-Secret-Token') || '', auth.TELEGRAM_WEBHOOK_SECRET);
      if (request.method !== 'POST' || !pathOK || !headerOK) return empty(401);
      let cfg;
      try { cfg = config(env, auth); } catch { return empty(500); }
      const update = await json(request);
      if (!object(update) || !Number.isInteger(update.update_id)) { console.error('malformed update'); return empty(400); }
      const edited = !('message' in update) && 'edited_message' in update;
      if (edited && !cfg.FORWARD_EDITS) { console.log('skipped', update.update_id); return empty(); }
      const message = update.message || update.edited_message || {};
      const chat = message.chat || {}, sender = message.from || {};
      const allowed = (chat.id === cfg.ALLOWED_CHAT_ID && (!cfg.ALLOWED_SENDER_IDS.size || cfg.ALLOWED_SENDER_IDS.has(sender.id) || sender.id === cfg.OWNER_USER_ID)) || (chat.type === 'private' && sender.id === cfg.OWNER_USER_ID);
      if (!allowed) { console.log('skipped', update.update_id); return empty(); }
      const response = await chatStub(env, chat.id).fetch(internal('/update', { update, edited }));
      return empty(response.status);
    }
    if (path === '/sent') {
      if (request.method !== 'POST') return empty(401);
      let key;
      try { key = sentApiKey(env); } catch { return empty(500); }
      if (!await safeEqual(request.headers.get('Authorization') || '', 'Bearer ' + key)) return empty(401);
      let cfg;
      try { cfg = config(env); } catch { return empty(500); }
      const item = await json(request);
      if (!validSent(item)) return empty(400);
      if (item.chat_id !== cfg.ALLOWED_CHAT_ID && item.chat_id !== cfg.OWNER_USER_ID) return empty(403);
      const response = await chatStub(env, item.chat_id).fetch(internal('/sent', item));
      return empty(response.status);
    }
    return empty(404);
  },
};

export class ChatRelay {
  constructor(ctx, env) { this.ctx = ctx; this.env = env; this.queue = Promise.resolve(); }
  fetch(request) {
    // Cloudflare docs: outbound fetch() opens the input gate, so serialize across the whole forward.
    const run = async () => {
      try { return await this.handle(request); }
      catch (err) { console.error('request error', err.name); return empty(500); }
    };
    this.queue = this.queue.then(run, run);
    return this.queue;
  }
  async handle(request) {
    const cfg = config(this.env);
    const state = await this.ctx.storage.get('state') ?? { count: 0, history: [], sent: [], seen: [] };
    const save = () => this.ctx.storage.put('state', state);
    const path = new URL(request.url).pathname;
    const item = await json(request);
    if (request.method !== 'POST') return empty(404);
    if (path === '/sent') {
      if (!validSent(item)) return empty(400);
      const entry = { message_id: item.message_id, date: item.date, from_id: null, from_name: 'self',
        is_bot: true, self: true, text: messageText(item, cfg.HISTORY_MAX_CHARS), reply_to_message_id: null };
      state.sent = state.sent.filter(e => e.message_id !== item.message_id);
      state.sent.push(entry);
      state.sent.sort(order);
      state.sent = state.sent.slice(-Math.max(cfg.HISTORY_SIZE, 1));
      await save();
      return empty();
    }
    if (path !== '/update') return empty(404);
    if (!object(item?.update) || !Number.isInteger(item.update.update_id)) return empty(400);
    const { update, edited } = item, uid = update.update_id;
    if (state.seen.includes(uid)) { console.log('duplicate', uid); return empty(); }
    const body = structuredClone(update), message = update.message || update.edited_message || {};
    const mid = message.message_id ?? null;
    if (cfg.HISTORY_SIZE > 0) {
      body.history = structuredClone(state.history.filter(e => e.message_id !== mid));
      const ids = new Set(body.history.map(e => e.message_id));
      body.history.push(...state.sent.filter(e => e.message_id !== mid && !ids.has(e.message_id)).map(e => ({ ...e, text: messageText(e, cfg.HISTORY_MAX_CHARS) })));
      body.history.sort(order);
      body.history = body.history.slice(-cfg.HISTORY_SIZE);
      body.history_note = state.sent.length ? SENT_NOTE : PLAIN_NOTE;
      const entry = historyEntry(message, cfg), index = state.history.findIndex(e => e.message_id === mid);
      if (index < 0) state.history.push(entry); else state.history[index] = entry;
      state.history = state.history.slice(-cfg.HISTORY_SIZE);
    }
    const mark = async () => {
      // Bound dedupe to the last 200 ids, covering realistic Telegram redelivery windows.
      state.seen = [...state.seen, uid].slice(-200);
      await save();
    };
    if (!(message.text || message.caption || (cfg.FORWARD_MEDIA && MEDIA.some(k => k in message)))) {
      console.log('skipped', uid); await mark(); return empty();
    }
    if (message.from?.id === cfg.OWNER_USER_ID && !edited) state.count = 0;
    if (state.count >= cfg.REPLY_CAP) { console.log('capped', uid, 'count', state.count); await mark(); return empty(); }
    await save();
    let status;
    try {
      const response = await fetch(cfg.TELEGRAM_WEBHOOK_URL, { method: 'POST', redirect: 'manual',
        headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + cfg.AGENT_WEBHOOK_KEY },
        body: JSON.stringify(body), signal: AbortSignal.timeout(30000) });
      status = response.status;
      if (response.body) await response.body.cancel();
    } catch (err) { console.log('forward error', uid, err.name); return empty(500); }
    if (status >= 200 && status < 300) {
      state.count += 1; await mark(); console.log('forwarded', uid, status); return empty();
    }
    if (status === 429 || (status >= 500 && status < 600)) { console.log('forward failed', uid, status); return empty(500); }
    console.log('forward failed', uid, status, 'not retried' + ([401, 403].includes(status) ? ' (check AGENT_WEBHOOK_KEY)' : ''));
    await mark();
    return empty();
  }
}

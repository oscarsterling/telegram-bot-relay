import { test, beforeEach, afterEach } from 'node:test';
import assert from 'node:assert/strict';
import worker, { ChatRelay } from '../src/index.js';
let env, objects, calls, logs, replies, saved;
const header = 'fixture-header-secret', path = 'fixture-path-secret-distinct', key = 'fixture-agent-key', sentKey = 'fixture-sent-key-distinct';
const u = (id = 1, changes = {}) => ({ update_id: id, message: { message_id: id, date: id, chat: { id: -100, type: 'group' }, from: { id: 2, first_name: 'First', last_name: 'Last', is_bot: true }, text: 'private-message-fixture', ...changes } });
const state = (id = '-100') => objects.get(id)?.data.get('state');
const hasLog = line => assert.ok(logs.includes(line), 'event reason: ' + line);
function send(body, route = '/telegram/' + path, headers = { 'X-Telegram-Bot-Api-Secret-Token': header }, method = 'POST') {
  return worker.fetch(new Request('https://worker.example' + route, { method, headers, ...(method === 'GET' ? {} : { body: JSON.stringify(body) }) }), env);
}
const sent = body => send(body, '/sent', { Authorization: 'Bearer ' + sentKey });
beforeEach(() => {
  objects = new Map(); calls = []; logs = []; replies = [];
  env = { TELEGRAM_WEBHOOK_SECRET: header, WEBHOOK_PATH_SECRET: path, AGENT_WEBHOOK_KEY: key, SENT_API_KEY: sentKey, TELEGRAM_WEBHOOK_URL: 'https://agent.example/inbox', ALLOWED_CHAT_ID: '-100', OWNER_USER_ID: '1', ALLOWED_SENDER_IDS: '', REPLY_CAP: '10', HISTORY_SIZE: '20', HISTORY_MAX_CHARS: '1000', FORWARD_EDITS: '0', FORWARD_MEDIA: '1', CHAT: {
    idFromName: name => name, get(id) {
      if (!objects.has(id)) {
        const data = new Map();
        const storage = { async get(k) { return structuredClone(data.get(k)); }, async put(k, v) { data.set(k, structuredClone(v)); } };
        const obj = new ChatRelay({ storage }, env);
        objects.set(id, { data, fetch: req => obj.fetch(req) });
      }
      return objects.get(id);
    },
  } };
  saved = [fetch, console.log, console.error];
  console.log = console.error = (...args) => logs.push(args.join(' '));
  globalThis.fetch = async (url, init) => {
    calls.push({ url, init, body: JSON.parse(init.body) });
    const next = replies.shift();
    if (next instanceof Error) throw next;
    return typeof next === 'function' ? next() : next || new Response('ok');
  };
});
afterEach(() => { [globalThis.fetch, console.log, console.error] = saved; });
for (const [name, route, headers, method] of [
  ['missing header', undefined, {}, 'POST'], ['wrong header', undefined, { 'X-Telegram-Bot-Api-Secret-Token': 'wrong' }, 'POST'],
  ['wrong path', '/telegram/wrong', undefined, 'POST'], ['GET', undefined, undefined, 'GET'],
]) test('auth rejects ' + name, async () => {
  const r = await send(u(), route, headers, method);
  assert.equal(r.status, 401); assert.equal(await r.text(), ''); assert.equal(calls.length, 0); assert.equal(objects.size, 0);
});
for (const [name, bad] of [['TELEGRAM_WEBHOOK_SECRET', ''], ['TELEGRAM_WEBHOOK_SECRET', '!'], ['WEBHOOK_PATH_SECRET', 'short'], ['AGENT_WEBHOOK_KEY', ''], ['TELEGRAM_WEBHOOK_URL', 'http://agent.example'], ['ALLOWED_CHAT_ID', ''], ['OWNER_USER_ID', '1e3'], ['REPLY_CAP', '0'], ['HISTORY_SIZE', '-1'], ['HISTORY_MAX_CHARS', '1.5'], ['FORWARD_EDITS', '2'], ['FORWARD_MEDIA', 'yes'], ['ALLOWED_SENDER_IDS', '2,']]) test('invalid config ' + name + JSON.stringify(bad), async () => {
  env[name] = bad; const r = await send(u()); assert.equal(r.status, 500); assert.equal(await r.text(), ''); assert.deepEqual(logs, ['config invalid ' + name]); assert.equal(calls.length, 0);
});
test('missing value logs name only; unauthenticated config stays private', async () => {
  delete env.OWNER_USER_ID;
  assert.equal((await send(u(), undefined, {})).status, 401); assert.deepEqual(logs, []);
  assert.equal((await send(u())).status, 500); assert.deepEqual(logs, ['config invalid OWNER_USER_ID']);
});
test('trims configuration values', async () => {
  env.ALLOWED_CHAT_ID = ' -100 '; env.ALLOWED_SENDER_IDS = ' 2, 3 '; env.REPLY_CAP = ' 1 ';
  await send(u()); assert.equal(calls.length, 1);
});
test('malformed updates rejected by shape and id', async () => {
  for (const body of [null, [], {}, { update_id: true }, { update_id: 1.5 }]) assert.equal((await send(body)).status, 400);
  assert.equal((await worker.fetch(new Request('https://worker.example/telegram/' + path, { method: 'POST', headers: { 'X-Telegram-Bot-Api-Secret-Token': header }, body: '{' }), env)).status, 400);
  assert.deepEqual(logs, Array(6).fill('malformed update'));
});
test('allow rule avoids state for other chats and non-owner private chats', async () => {
  await send(u(1, { chat: { id: -999, type: 'group' } })); await send(u(2, { chat: { id: 2, type: 'private' } }));
  assert.equal(objects.size, 0); assert.deepEqual(logs, ['skipped 1', 'skipped 2']);
  await send(u(3, { chat: { id: 1, type: 'private' }, from: { id: 1 } })); assert.equal(calls.length, 1); assert.ok(objects.has('1'));
});
test('sender allowlist always permits owner', async () => {
  env.ALLOWED_SENDER_IDS = '3'; await send(u()); assert.equal(objects.size, 0); hasLog('skipped 1');
  await send(u(2, { from: { id: 3 } })); await send(u(3, { from: { id: 1 } })); assert.equal(calls.length, 2);
});
test('cap stores capped messages; owner resets but edit does not', async () => {
  env.REPLY_CAP = '1'; env.FORWARD_EDITS = '1';
  await send(u()); await send(u(2)); assert.equal(calls.length, 1); hasLog('capped 2 count 1'); assert.deepEqual(state().history.map(e => e.message_id), [1, 2]);
  await send(u(3, { from: { id: 1 } })); assert.equal(calls.length, 2);
  await send({ update_id: 4, edited_message: u(4, { from: { id: 1 } }).message }); assert.equal(calls.length, 2); hasLog('capped 4 count 1');
});
test('history shape quotes Unicode truncation and size', async () => {
  env.HISTORY_SIZE = '2'; env.HISTORY_MAX_CHARS = '3';
  await send(u(1, { text: '😀abcd', reply_to_message: { message_id: 99, from: { id: 9, username: 'quoted' }, text: 'abc' } }));
  assert.deepEqual(state().history[0], { message_id: 1, date: 1, from_id: 2, from_name: 'First Last', is_bot: true, text: '😀ab...', reply_to_message_id: 99, reply_to: { message_id: 99, from_id: 9, from_name: 'quoted', is_bot: false, text: 'abc' } });
  await send(u(2)); assert.deepEqual(calls[1].body.history, [state().history[0]]); assert.ok(calls[1].body.history_note.includes('appear only when someone quoted'));
  await send(u(3)); assert.deepEqual(state().history.map(e => e.message_id), [2, 3]); assert.deepEqual(calls[2].body.history.map(e => e.message_id), [1, 2]);
});
test('history zero omits both fields', async () => {
  env.HISTORY_SIZE = '0'; await send(u()); assert.deepEqual(calls[0].body, u()); assert.deepEqual(state().history, []);
});
test('default edit skips without DO; enabled edit replaces in place and excludes current', async () => {
  const edit = { update_id: 3, edited_message: u(1, { text: 'changed' }).message };
  await send(edit); assert.equal(objects.size, 0); hasLog('skipped 3');
  env.FORWARD_EDITS = '1'; await send(u()); await send(u(2)); await send(edit);
  assert.deepEqual(state().history.map(e => e.message_id), [1, 2]); assert.equal(state().history[0].text, 'changed'); assert.deepEqual(calls[2].body.history.map(e => e.message_id), [2]);
});
for (const kind of ['photo', 'document', 'voice', 'audio', 'video']) test('media raw forward ' + kind, async () => {
  const media = kind === 'photo' ? [{ file_id: 'small' }, { file_id: 'large' }] : { file_id: 'large' };
  await send(u(1, { text: undefined, [kind]: media })); assert.deepEqual(calls[0].body.message[kind], media); assert.equal(state().history[0].file_id, 'large'); assert.equal(state().history[0].text, '[' + kind + ']');
});
test('sticker and disabled media skip, captions still forward', async () => {
  await send(u(1, { text: undefined, sticker: {} })); assert.equal(calls.length, 0); hasLog('skipped 1');
  env.FORWARD_MEDIA = '0'; await send(u(2, { text: undefined, photo: [{ file_id: 'image' }] })); assert.equal(calls.length, 0); assert.equal(state().history[1].file_id, undefined);
  await send(u(3, { text: undefined, caption: 'caption', photo: [{ file_id: 'image' }] })); assert.equal(calls.length, 1);
});
test('forward HTTPS POST bearer raw JSON manual redirects and deadline', async () => {
  const raw = { ...u(), extra: { raw: true } }; await send(raw);
  const { url, init, body } = calls[0]; assert.equal(url, env.TELEGRAM_WEBHOOK_URL); assert.equal(init.method, 'POST'); assert.equal(init.redirect, 'manual');
  assert.deepEqual(init.headers, { 'Content-Type': 'application/json', Authorization: 'Bearer ' + key }); assert.ok(init.signal instanceof AbortSignal);
  assert.deepEqual(body, { ...raw, history: [], history_note: "Earlier messages in this chat, oldest first, excluding the current one; the receiving bot's own messages appear only when someone quoted them in a reply." });
});
for (const status of [302, 0, 401, 403, 400]) test('terminal forward ' + status, async () => {
  replies.push(status === 0 ? { status: 0, type: 'opaqueredirect', body: null } : new Response('no', { status }));
  assert.equal((await send(u())).status, 200); assert.equal(state().count, 0); assert.deepEqual(state().seen, [1]);
  hasLog('forward failed 1 ' + status + ' not retried' + ([401, 403].includes(status) ? ' (check AGENT_WEBHOOK_KEY)' : ''));
});
test('successful duplicate leaves state unchanged', async () => {
  await send(u()); const prior = structuredClone(state()); await send(u()); assert.equal(calls.length, 1); assert.deepEqual(state(), prior); hasLog('duplicate 1');
});
for (const error of [500, 429, new TypeError('private error')]) test('transient retry ' + (error.name || error), async () => {
  env.REPLY_CAP = '1'; replies.push(error instanceof Error ? error : new Response('retry', { status: error }));
  assert.equal((await send(u())).status, 500); assert.equal(state().count, 0); assert.deepEqual(state().seen, []); assert.equal(state().history.length, 1);
  hasLog(error instanceof Error ? 'forward error 1 TypeError' : 'forward failed 1 ' + error);
  assert.equal((await send(u())).status, 200); assert.equal(calls.length, 2); assert.equal(state().count, 1); assert.deepEqual(state().seen, [1]);
});
test('owner reset persists before a failed forward', async () => {
  env.REPLY_CAP = '1'; await send(u()); replies.push(new Response(null, { status: 500 })); await send(u(2, { from: { id: 1 } })); assert.equal(state().count, 0); assert.equal(state().history[1].message_id, 2);
});
test('concurrent duplicates forward once while outbound fetch yields', async () => {
  replies.push(async () => { await new Promise(resolve => setTimeout(resolve, 20)); return new Response('ok'); });
  const results = await Promise.all([send(u()), send(u())]); assert.deepEqual(results.map(r => r.status), [200, 200]); assert.equal(calls.length, 1); hasLog('duplicate 1');
});
test('/sent auth body and chat validation', async () => {
  for (const auth of ['', 'Bearer wrong']) assert.equal((await send({}, '/sent', { Authorization: auth })).status, 401);
  assert.equal((await send({}, '/sent', {}, 'GET')).status, 401);
  for (const body of [{}, { chat_id: true, message_id: 1, date: 1, text: '' }, { chat_id: -100, message_id: 1.5, date: 1, text: '' }, { chat_id: -100, message_id: 1, date: 1, text: 4 }]) assert.equal((await sent(body)).status, 400);
  assert.equal((await sent({ chat_id: 88, message_id: 1, date: 1, text: 'no' })).status, 403); assert.equal(objects.size, 0);
});
test('/sent last write wins, merges self, stored history wins clash', async () => {
  const entry = { chat_id: -100, message_id: 10, date: 1, text: 'first' };
  assert.equal((await sent(entry)).status, 200); await sent({ ...entry, text: 'last' }); assert.deepEqual(state().history, []); assert.equal(state().sent.length, 1);
  await send(u(20)); assert.deepEqual(calls[0].body.history[0], { message_id: 10, date: 1, from_id: null, from_name: 'self', is_bot: true, self: true, text: 'last', reply_to_message_id: null });
  assert.ok(calls[0].body.history_note.includes('marked "self": true'));
  await sent({ ...entry, message_id: 20, text: 'clash' }); await send(u(21)); assert.equal(calls[1].body.history.find(e => e.message_id === 20).text, 'private-message-fixture'); assert.ok(state().history.every(e => !e.self));
});
test('/sent retention ordering and merge-time truncation', async () => {
  env.HISTORY_SIZE = '2'; for (const id of [3, 1, 2]) await sent({ chat_id: -100, message_id: id, date: id, text: 'abcdef' });
  assert.deepEqual(state().sent.map(e => e.message_id), [2, 3]); env.HISTORY_MAX_CHARS = '3'; await send(u(4)); assert.deepEqual(calls[0].body.history.map(e => e.text), ['abc...', 'abc...']);
  env.HISTORY_SIZE = '0'; await sent({ chat_id: 1, message_id: 1, date: 1, text: '' }); assert.equal(state('1').sent.length, 1); assert.equal(state('1').sent[0].text, '[me...');
});
test('dedupe keeps last 200 ids', async () => {
  env.REPLY_CAP = '1'; env.HISTORY_SIZE = '0'; for (let id = 1; id <= 202; id++) await send(u(id)); assert.equal(state().seen.length, 200); assert.equal(state().seen[0], 3); assert.equal(state().seen.at(-1), 202);
});
test('unknown route empty 404', async () => { const r = await send({}, '/other'); assert.equal(r.status, 404); assert.equal(await r.text(), ''); });
test('logs never include credentials URLs or message text', async () => {
  await sent({ chat_id: -100, message_id: 99, date: 0, text: 'private-message-fixture' });
  await send(u()); await send(u()); replies.push(new TypeError('private-message-fixture')); await send(u(2)); env.REPLY_CAP = '1'; await send(u(3));
  const output = logs.join('\n'); hasLog('forwarded 1 200'); hasLog('forward error 2 TypeError');
  for (const value of [header, path, key, sentKey, 'private-message-fixture', env.TELEGRAM_WEBHOOK_URL]) assert.equal(output.includes(value), false);
});
test('secrets must differ and meet their length bounds', async () => {
  env.WEBHOOK_PATH_SECRET = header;
  assert.equal((await send(u())).status, 500); assert.deepEqual(logs, ['config invalid WEBHOOK_PATH_SECRET']);
  env.WEBHOOK_PATH_SECRET = path; env.TELEGRAM_WEBHOOK_SECRET = 'a'.repeat(257); logs.length = 0;
  assert.equal((await send(u())).status, 500); assert.deepEqual(logs, ['config invalid TELEGRAM_WEBHOOK_SECRET']);
  delete env.TELEGRAM_WEBHOOK_SECRET; logs.length = 0;
  assert.equal((await send(u())).status, 500); assert.deepEqual(logs, ['config invalid TELEGRAM_WEBHOOK_SECRET']);
});
test('every response body is cancelled before status handling', async () => {
  let cancelled = 0;
  for (const status of [200, 302, 429, 500]) {
    replies.push(new Response(new ReadableStream({ cancel() { cancelled++; } }), { status }));
    await send(u(status));
  }
  assert.equal(cancelled, 4);
});
test('timeout remains retryable', async () => {
  replies.push(new DOMException('private timeout text', 'TimeoutError'));
  assert.equal((await send(u())).status, 500); hasLog('forward error 1 TimeoutError'); assert.deepEqual(state().seen, []);
  assert.equal((await send(u())).status, 200); assert.equal(state().count, 1);
});
test('two distinct concurrent messages respect the cap', async () => {
  env.REPLY_CAP = '1';
  replies.push(async () => { await new Promise(resolve => setTimeout(resolve, 20)); return new Response('ok'); });
  await Promise.all([send(u(1)), send(u(2))]); assert.equal(calls.length, 1); assert.equal(state().count, 1);
  const capped = logs.filter(line => /^capped (1|2) count 1$/.test(line));
  assert.equal(capped.length, 1);
  assert.notEqual(Number(capped[0].split(' ')[1]), calls[0].body.update_id);
});

test('/sent accepts only its separate key', async () => {
  const entry = { chat_id: -100, message_id: 1, date: 1, text: 'self' };
  assert.equal((await send(entry, '/sent', { Authorization: 'Bearer ' + key })).status, 401);
  assert.equal(objects.size, 0);
  assert.equal((await sent(entry)).status, 200); assert.equal(state().sent[0].text, 'self');
});
for (const [name, other] of [['SENT_API_KEY', 'AGENT_WEBHOOK_KEY'], ['SENT_API_KEY', 'TELEGRAM_WEBHOOK_SECRET'], ['SENT_API_KEY', 'WEBHOOK_PATH_SECRET'], ['AGENT_WEBHOOK_KEY', 'TELEGRAM_WEBHOOK_SECRET'], ['AGENT_WEBHOOK_KEY', 'WEBHOOK_PATH_SECRET']]) test('key separation ' + name + ' from ' + other, async () => {
  env[name] = env[other];
  for (const route of ['/telegram/' + path, '/sent']) {
    logs.length = 0;
    const headers = route === '/sent' ? { Authorization: 'Bearer ' + env.SENT_API_KEY } : undefined;
    assert.equal((await send(u(), route, headers)).status, 500); assert.deepEqual(logs, ['config invalid ' + name]);
  }
  assert.equal(objects.size, 0);
});
for (const bad of [undefined, '', 'short', 'invalid key value', 'a'.repeat(257)]) test('invalid sent key ' + String(bad?.length), async () => {
  if (bad === undefined) delete env.SENT_API_KEY; else env.SENT_API_KEY = bad;
  for (const route of ['/telegram/' + path, '/sent']) {
    logs.length = 0;
    const r = await send(u(), route); assert.equal(r.status, 500); assert.equal(await r.text(), '');
    assert.deepEqual(logs, ['config invalid SENT_API_KEY']);
  }
});
test('sent key is trimmed', async () => {
  env.SENT_API_KEY = ' ' + sentKey + ' ';
  assert.equal((await sent({ chat_id: -100, message_id: 1, date: 1, text: 'self' })).status, 200);
});
for (const [size, chars, status, reason] of [[101, 1000, 500, 'HISTORY_SIZE'], [100, 1000, 200], [20, 4001, 500, 'HISTORY_MAX_CHARS'], [20, 4000, 200], [100, 1001, 500, 'HISTORY_MAX_CHARS'], [25, 4000, 200]]) test('storage bounds ' + size + ' x ' + chars, async () => {
  env.HISTORY_SIZE = String(size); env.HISTORY_MAX_CHARS = String(chars);
  assert.equal((await send(u())).status, status);
  if (reason) assert.deepEqual(logs, ['config invalid ' + reason]); else assert.equal(state().history.length, 1);
});
test('worst-case bounded history and sent state fits storage', async () => {
  env.HISTORY_SIZE = '100'; env.HISTORY_MAX_CHARS = '1000';
  const text = '😀'.repeat(1000);
  for (let id = 1; id <= 100; id++) {
    assert.equal((await send(u(id, { text, reply_to_message: u(1000 + id, { text }).message }))).status, 200);
    assert.equal((await sent({ chat_id: -100, message_id: 2000 + id, date: id, text })).status, 200);
  }
  const stored = state(); assert.equal(stored.history.length, 100); assert.equal(stored.sent.length, 100);
  assert.equal(stored.history[0].text, text); assert.equal(stored.history[0].reply_to.text, text); assert.equal(stored.sent[0].text, text);
  assert.ok(new TextEncoder().encode(JSON.stringify(stored)).length < 2000000);
});

test('/sent authenticates before parsing any body', async () => {
  const request = new Request('https://worker.example/sent', { method: 'POST', headers: { Authorization: 'Bearer ' + key }, body: '{' });
  request.json = () => { assert.fail('unauthenticated body parsed'); };
  assert.equal((await worker.fetch(request, env)).status, 401); assert.equal(objects.size, 0);
});

test('/sent keeps other invalid config private until authenticated', async () => {
  env.ALLOWED_CHAT_ID = '';
  const request = new Request('https://worker.example/sent', { method: 'POST', body: '{' });
  request.json = () => { assert.fail('unauthenticated body parsed'); };
  const unauthorized = await worker.fetch(request, env);
  assert.equal(unauthorized.status, 401); assert.equal(await unauthorized.text(), ''); assert.deepEqual(logs, []);
  const authenticated = await sent({});
  assert.equal(authenticated.status, 500); assert.equal(await authenticated.text(), '');
  assert.deepEqual(logs, ['config invalid ALLOWED_CHAT_ID']); assert.equal(objects.size, 0); assert.equal(calls.length, 0);
});

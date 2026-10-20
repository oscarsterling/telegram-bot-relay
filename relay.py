"""Long-poll Telegram getUpdates and forward allowed messages to an HTTPS webhook.
A per-chat REPLY_CAP loop guard resets when the owner posts again.
All configuration comes from environment variables, with no secrets in code.
Each POST also carries recent chat history.
"""
from __future__ import annotations

import copy
import http.client
import json
import os
import socket
import sys
import signal
import threading
from contextlib import contextmanager
import tempfile
import time
import urllib.error
import urllib.request
import urllib.parse
import unicodedata
from pathlib import Path


SOCKET_TIMEOUT = 40
API_TIMEOUT = 40
LONG_POLL = 25
POST_TIMEOUT = 30
API_DEADLINE = 45
POST_DEADLINE = 50
CONNECT_TIMEOUT = 8
LOCK_HELD_EXIT = 3
POLL_STALL_EXIT = 4
STATS_EVERY = 100
SENT_LOG_TAIL_BYTES = 1_000_000
MAX_WAIT = 300
_heartbeat_counter = 0


class Hang(Exception):
    pass


@contextmanager
def deadline(seconds):
    if not hasattr(signal, "SIGALRM") or threading.current_thread() is not threading.main_thread():
        yield
        return

    # Socket timeouts bound individual operations, not slow-drip responses or a
    # whole TLS handshake. DNS has no socket timeout and may evade alarms too;
    # run.sh's heartbeat watchdog must be the backstop for those hangs.
    armed = True

    def alarm(signum, frame):
        if armed:
            raise Hang()

    previous = signal.signal(signal.SIGALRM, alarm)
    try:
        signal.setitimer(signal.ITIMER_REAL, seconds)
        yield
        armed = False
    finally:
        armed = False  # A cleanup alarm must not cause a completed call to be retried.
        try:
            signal.setitimer(signal.ITIMER_REAL, 0)
        finally:
            signal.signal(signal.SIGALRM, previous)


HISTORY_NOTE = ("Earlier messages in this chat, oldest first, excluding the current one; "
                "the receiving bot's own messages appear only when someone quoted them in a reply.")
SENT_HISTORY_NOTE = ("Earlier messages in this chat, oldest first, excluding the current one; "
                     'this bot\'s own messages come from its sent log and are marked "self": true.')
FORWARDED_MEDIA = ("photo", "document", "voice", "audio", "video")
MESSAGE_TYPES = ("photo", "sticker", "document", "voice", "video", "audio",
                 "animation", "location", "poll", "contact")


def env(name, default=None, parse=str):
    value = os.environ.get(name, default)
    if value is None or not value.strip() and default is None:
        raise SystemExit(name + " is required")
    try:
        return parse(value.strip())
    except (ValueError, TypeError):
        raise SystemExit(name + " is invalid") from None



def state_dir():
    # State holds chat text, so keep the default outside the git checkout.
    value = os.environ.get("RELAY_STATE_DIR", "").strip()
    path = Path(value or "~/.local/state/telegram-bot-relay").expanduser().resolve()
    os.makedirs(path, mode=0o700, exist_ok=True)
    info = path.stat()
    if hasattr(os, "getuid") and (info.st_uid != os.getuid() or info.st_mode & 0o077):
        raise SystemExit("RELAY_STATE_DIR must be owned by you and not accessible to group or others (chmod 700)")
    return path


def acquire_lock(state_dir):
    try:
        import fcntl
    except ImportError:
        log("no single-instance lock on this platform")
        return None
    # A supervisor pidfile check can race, and SIGKILL can leave an orphan
    # relay. The process that polls must hold the lock itself.
    path = Path(state_dir) / "relay.lock"
    lock = os.fdopen(os.open(path, os.O_CREAT | os.O_RDWR, 0o600), "r+")
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        try:
            pid = lock.read().strip()
        except (OSError, UnicodeError):
            pid = ""
        finally:
            lock.close()
        pid = pid if pid.isascii() and pid.isdecimal() else "unknown"
        log(f"another relay (pid {pid}) holds {path.name}, exiting")
        raise SystemExit(LOCK_HELD_EXIT) from None
    except BaseException:
        lock.close()
        raise
    try:
        lock.seek(0)
        lock.truncate()
        lock.write(str(os.getpid()) + "\n")
        lock.flush()
    except BaseException:
        lock.close()
        raise
    return lock

def config():
    cfg = {name: env(name) for name in (
        "TELEGRAM_BOT_TOKEN", "TELEGRAM_WEBHOOK_URL", "TELEGRAM_WEBHOOK_KEY")}
    if any(c.isspace() or unicodedata.category(c) == "Cc"
           for c in cfg["TELEGRAM_BOT_TOKEN"]):
        raise SystemExit("TELEGRAM_BOT_TOKEN is invalid")
    try:
        url = urllib.parse.urlsplit(cfg["TELEGRAM_WEBHOOK_URL"])
        valid = url.scheme.lower() == "https" and bool(url.hostname)
        url.port
    except ValueError:
        valid = False
    if not valid:
        raise SystemExit("TELEGRAM_WEBHOOK_URL is invalid")
    for name, default in (("ALLOWED_CHAT_ID", None), ("OWNER_USER_ID", None),
                          ("REPLY_CAP", "10"), ("HISTORY_SIZE", "20"),
                          ("HISTORY_MAX_CHARS", "1000")):
        cfg[name] = env(name, default, int)
        if default is not None and cfg[name] < 0:
            raise SystemExit(name + " must be non-negative")
    for name in ("REPLY_CAP", "HISTORY_MAX_CHARS"):
        if cfg[name] < 1:
            raise SystemExit(name + " must be at least 1")
    cfg["FORWARD_RETRIES"] = env("FORWARD_RETRIES", "5", int)
    if cfg["FORWARD_RETRIES"] < 1:
        raise SystemExit("FORWARD_RETRIES must be at least 1")
    cfg["ALLOWED_SENDER_IDS"] = env(
        "ALLOWED_SENDER_IDS", "", lambda value: {int(x) for x in value.split(",")} if value else set())
    cfg["FORWARD_EDITS"] = env("FORWARD_EDITS", "0")
    if cfg["FORWARD_EDITS"] not in ("0", "1"):
        raise SystemExit("FORWARD_EDITS must be 0 or 1")
    cfg["FORWARD_EDITS"] = cfg["FORWARD_EDITS"] == "1"
    cfg["POLL_STALL_MAX"] = env("POLL_STALL_MAX", "180", int)
    if cfg["POLL_STALL_MAX"] < 60:
        raise SystemExit("POLL_STALL_MAX must be at least 60")
    cfg["FORWARD_MEDIA"] = env("FORWARD_MEDIA", "1")
    if cfg["FORWARD_MEDIA"] not in ("0", "1"):
        raise SystemExit("FORWARD_MEDIA must be 0 or 1")
    cfg["FORWARD_MEDIA"] = cfg["FORWARD_MEDIA"] == "1"
    cfg["RELAY_STATE_DIR"] = state_dir()
    sent_log = Path(env("SENT_LOG", "sent.jsonl"))
    cfg["SENT_LOG"] = (sent_log if sent_log.is_absolute() else
                       cfg["RELAY_STATE_DIR"] / sent_log).resolve()
    return cfg


def connect(address, timeout, source_address=None):
    # Each resolved address gets a short attempt, leaving time for the response.
    # Let the poll loop distinguish connect timeouts from read timeouts.
    try:
        sock = socket.create_connection(address, CONNECT_TIMEOUT, source_address)
    except Exception as exc:
        exc.relay_phase = "connect"
        raise
    sock.settimeout(timeout)
    return sock


class HTTPConnection(http.client.HTTPConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = connect


class HTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = connect


class HTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(HTTPConnection, req)


class HTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(HTTPSConnection, req, context=self._context)


def make_opener(*handlers):
    return urllib.request.build_opener(HTTPHandler(), HTTPSHandler(), *handlers)


def make_post(url, key):
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    opener = make_opener(NoRedirect())

    def post(body):
        req = urllib.request.Request(
            url, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
        with deadline(POST_DEADLINE):
            with opener.open(req, timeout=POST_TIMEOUT) as response:
                return response.status

    return post


def log(*args):
    # Message text and credentials must never reach logs.
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *args, flush=True)


def atomic_write(path, text):
    path = Path(path)
    temp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=path.name + ".tmp-", delete=False) as out:
            temp = out.name
            out.write(text)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp, path)
        if os.name == "posix":
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if temp is not None and os.path.exists(temp):
            os.unlink(temp)


def heartbeat(path):
    # A sequence lets the supervisor detect stalls without trusting the system clock.
    global _heartbeat_counter
    _heartbeat_counter += 1
    atomic_write(path, str(_heartbeat_counter))


def pause(seconds, beat):
    while seconds > 0:
        heartbeat(beat)
        part = min(seconds, 30)
        time.sleep(part)
        seconds -= part


class Retry:
    def __init__(self, after=None):
        self.after = after


def retry_after_from_body(raw_bytes):
    try:
        body = json.loads(raw_bytes)
        value = body.get("parameters", {}).get("retry_after")
        return value if type(value) is int and value > 0 else None
    except Exception:
        return None


def retry_after_header(headers):
    try:
        value = headers.get("Retry-After")
        if isinstance(value, str) and value and all("0" <= c <= "9" for c in value):
            return int(value)
    except Exception:
        return None
    return None


def poll_wait(exc):
    if isinstance(exc, urllib.error.HTTPError):
        code = exc.code
        if code in (401, 404):
            return (f"poll failed {code}: Telegram rejected the bot token "
                    "(check TELEGRAM_BOT_TOKEN)", 60)
        if code == 409:
            return ("poll failed 409: a webhook is set for this bot or another poller is using it "
                    "(call deleteWebhook, or stop the other copy)", 60)
        if code == 429:
            wait = min(MAX_WAIT, getattr(exc, "retry_after", None) or 5)
            return f"poll failed 429: rate limited, waiting {wait}s", wait
        return f"poll failed {code}", 5
    if isinstance(exc, urllib.error.URLError):
        return f"poll error URLError ({type(exc.reason).__name__})", 5
    message = "poll error " + type(exc).__name__
    if isinstance(exc, OSError) and exc.errno is not None:
        message += f" errno={exc.errno}"
    return message, 5


def poll_phase(exc):
    cause = exc.reason if isinstance(exc, urllib.error.URLError) and not isinstance(
        exc, urllib.error.HTTPError) else exc
    if isinstance(cause, (socket.timeout, TimeoutError, Hang)):
        return getattr(cause, "relay_phase", "read")
    return None


def poll_retryable(exc):
    if isinstance(exc, urllib.error.HTTPError):
        return False
    cause = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    return isinstance(cause, (socket.timeout, TimeoutError, Hang, ConnectionError,
                              socket.gaierror)) or getattr(cause, "relay_phase", None) == "connect"


def log_stats(stats):
    log("stats", *(f"{k}={v}" for k, v in stats.items()))


def load_json(path, closed=None):
    try:
        with path.open(encoding="utf-8") as source:
            value = json.load(source)
        if not isinstance(value, dict):
            raise ValueError("state must be an object")
        if closed is not None and any(type(count) is not int for count in value.values()):
            raise ValueError("counts must be integers")
        if path.name == "history.json" and any(
                not isinstance(entries, list) or any(
                    not isinstance(entry, dict) or type(entry.get("message_id")) is not int
                    or (entry.get("date") is not None and type(entry["date"]) is not int)
                    for entry in entries) for entries in value.values()):
            raise ValueError("history must contain lists of messages")
        return value
    except FileNotFoundError:
        return {}
    except (ValueError, UnicodeError) as exc:
        log("CORRUPT", path.name, type(exc).__name__,
            "moving aside, all chats capped" if closed is not None else "moving aside")
        backup = path.with_name(path.name + ".corrupt-" + str(int(time.time())))
        os.replace(path, backup)
        if closed is not None:
            atomic_write(path, json.dumps(closed))
            return closed
        return {}


def load_state(cfg):
    root = Path(cfg["RELAY_STATE_DIR"])
    return {"counts": load_json(root / "replycount.json",
                           {str(cfg[name]): cfg["REPLY_CAP"]
                            for name in ("ALLOWED_CHAT_ID", "OWNER_USER_ID")}),
            "history": load_json(root / "history.json") if cfg["HISTORY_SIZE"] > 0 else {}}


def message_text(message, limit):
    text = message.get("text") or message.get("caption")
    if not text:
        text = next(("[" + kind + "]" for kind in MESSAGE_TYPES if kind in message), "[message]")
    return text[:limit] + "..." if len(text) > limit else text


def summary(message, limit):
    sender = message.get("from", {})
    name = " ".join(filter(None, (sender.get("first_name"), sender.get("last_name"))))
    return {"message_id": message.get("message_id"), "from_id": sender.get("id"),
            "from_name": name or sender.get("username", ""),
            "is_bot": sender.get("is_bot", False), "text": message_text(message, limit)}


def media_file_id(message):
    for kind in FORWARDED_MEDIA:
        if kind in message:
            item = message[kind]
            if kind == "photo":
                item = item[-1] if isinstance(item, list) and item else None
            value = item.get("file_id") if isinstance(item, dict) else None
            return value if isinstance(value, str) and value else None
    return None


def history_entry(message, limit, media=False):
    entry = summary(message, limit)
    if media:
        file_id = media_file_id(message)
        if file_id is not None:
            entry["file_id"] = file_id
    entry.update(date=message.get("date"),
                 reply_to_message_id=message.get("reply_to_message", {}).get("message_id"))
    if message.get("reply_to_message"):
        # getUpdates never delivers this bot's own messages, so a quote is their only
        # source without a sent log; keep it even when the sent log also has the original.
        entry["reply_to"] = summary(message["reply_to_message"], limit)
    return entry


def sent_entries(path, chat_id, limit):
    entries, errors, bad = {}, set(), 0
    try:
        with path.open("rb") as source:
            start = max(0, source.seek(0, os.SEEK_END) - SENT_LOG_TAIL_BYTES)
            source.seek(start)
            if start:
                source.readline()
            for line in source:
                if not line.strip():
                    continue
                try:
                    item = json.loads(line.decode("utf-8"))
                    if (not isinstance(item, dict) or
                            any(type(item.get(key)) is not int
                                for key in ("chat_id", "message_id", "date")) or
                            not isinstance(item.get("text"), str)):
                        raise ValueError("invalid sent entry")
                    if item["chat_id"] == chat_id:
                        entries[item["message_id"]] = dict(
                            message_id=item["message_id"], date=item["date"],
                            from_id=None, from_name="self", is_bot=True, self=True,
                            text=message_text(item, limit),
                            reply_to_message_id=None)
                except (ValueError, UnicodeError, RecursionError) as exc:
                    bad += 1
                    errors.add(type(exc).__name__)
    except FileNotFoundError:
        return [], False
    except Exception as exc:
        log("sent log unreadable", type(exc).__name__)
        return [], True
    if bad:
        log("sent log skipped", bad, "bad line(s)", ",".join(sorted(errors)))
    return list(entries.values()), True


def process_update(u, cfg, state, post):
    edited = "message" not in u and "edited_message" in u
    if edited and not cfg.get("FORWARD_EDITS", False):
        log("skipped", u["update_id"])
        return True
    message = u.get("message") or u.get("edited_message") or {}
    chat, sender = message.get("chat", {}), message.get("from", {})
    senders = cfg.get("ALLOWED_SENDER_IDS", set())
    allowed = (chat.get("id") == cfg["ALLOWED_CHAT_ID"] and (
        not senders or sender.get("id") in senders or sender.get("id") == cfg["OWNER_USER_ID"])) or (
        chat.get("type") == "private" and sender.get("id") == cfg["OWNER_USER_ID"])
    root = Path(cfg["RELAY_STATE_DIR"])
    body = copy.deepcopy(u)
    if allowed:
        key = str(chat.get("id"))
        size = cfg["HISTORY_SIZE"]
        if size > 0:
            entries = state["history"].setdefault(key, [])
            mid = message.get("message_id")
            body["history"] = copy.deepcopy([entry for entry in entries if entry["message_id"] != mid])
            own, exists = sent_entries(cfg["SENT_LOG"], chat.get("id"), cfg["HISTORY_MAX_CHARS"])
            seen = {entry["message_id"] for entry in body["history"]}
            body["history"].extend(entry for entry in own
                                   if entry["message_id"] != mid and entry["message_id"] not in seen)
            body["history"] = sorted(body["history"],
                                     key=lambda entry: (entry.get("date") or 0, entry.get("message_id") or 0))[-size:]
            body["history_note"] = SENT_HISTORY_NOTE if exists else HISTORY_NOTE
            current = history_entry(message, cfg["HISTORY_MAX_CHARS"],
                                    media=cfg.get("FORWARD_MEDIA", True))
            for index, entry in enumerate(entries):
                if entry["message_id"] == mid:
                    entries[index] = current
                    break
            else:
                entries.append(current)
            entries[:] = entries[-size:]
            atomic_write(root / "history.json", json.dumps(state["history"]))
    if allowed and (message.get("text") or message.get("caption") or (
            cfg.get("FORWARD_MEDIA", True) and any(kind in message for kind in FORWARDED_MEDIA))):
        counts = state["counts"]
        if sender.get("id") == cfg["OWNER_USER_ID"] and not edited:
            counts[key] = 0
            atomic_write(root / "replycount.json", json.dumps(counts))
        if counts.get(key, 0) >= cfg["REPLY_CAP"]:
            log("capped", u["update_id"], "count", counts.get(key, 0))
            return True
        after = None
        try:
            status = post(body)
        except urllib.error.HTTPError as exc:
            status = exc.code
            try:
                if status in (429, 503):
                    after = retry_after_header(exc.headers)
            finally:
                exc.close()
        except Exception as exc:
            log("forward error", u["update_id"], type(exc).__name__)
            return Retry()
        if 200 <= status < 300:
            counts[key] = counts.get(key, 0) + 1
            atomic_write(root / "replycount.json", json.dumps(counts))
            log("forwarded", u["update_id"], status)
        elif status == 429 or 500 <= status < 600:
            log("forward failed", u["update_id"], status)
            return Retry(after)
        else:
            hint = " (check TELEGRAM_WEBHOOK_KEY)" if status in (401, 403) else ""
            log("forward failed", u["update_id"], status, "not retried" + hint)
    else:
        log("skipped", u["update_id"])
    return True


def run():
    # Chat text and state files must be readable only by their owner.
    os.umask(0o077)
    socket.setdefaulttimeout(SOCKET_TIMEOUT)
    cfg = config()
    lock = acquire_lock(cfg["RELAY_STATE_DIR"])
    stats = dict(polls=0, timeouts_connect=0, timeouts_read=0, poll_errors=0,
                 forward_retries=0, stall_exits=0)
    previous_term = None
    term_installed = False
    try:
        if threading.current_thread() is threading.main_thread() and hasattr(signal, "SIGTERM"):
            def terminate(signum, frame):
                raise SystemExit(128 + signum)
            previous_term = signal.signal(signal.SIGTERM, terminate)
            term_installed = True
        state = load_state(cfg)
        off = Path(cfg["RELAY_STATE_DIR"]) / "offset"
        beat = off.with_name("heartbeat")
        heartbeat(beat)

        opener = make_opener()

        def tg(method, **params):
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{cfg['TELEGRAM_BOT_TOKEN']}/{method}",
                data=json.dumps(params).encode(), headers={"Content-Type": "application/json"})
            with deadline(API_DEADLINE):
                try:
                    with opener.open(req, timeout=API_TIMEOUT) as response:
                        return json.load(response)
                except urllib.error.HTTPError as exc:
                    try:
                        exc.retry_after = retry_after_from_body(exc.read())
                    except Exception:
                        exc.retry_after = None
                    finally:
                        exc.close()
                    raise

        post = make_post(cfg["TELEGRAM_WEBHOOK_URL"], cfg["TELEGRAM_WEBHOOK_KEY"])
        allowed_updates = ["message"] + (["edited_message"] if cfg.get("FORWARD_EDITS", False) else [])
        offset = None
        if off.exists():
            try:
                offset = int(off.read_text())
            except (ValueError, UnicodeError) as exc:
                log("CORRUPT", off.name, type(exc).__name__, "moving aside")
                os.replace(off, off.with_name("offset.corrupt-" + str(int(time.time()))))
        last_ok = time.monotonic()
        limit = cfg.get("POLL_STALL_MAX", 180)

        def poll(**params):
            nonlocal last_ok
            stats["polls"] += 1
            try:
                result = tg("getUpdates", **params)
                updates = result["result"]
                last_ok = time.monotonic()
                return updates
            except Exception as exc:
                phase = poll_phase(exc)
                key = "timeouts_" + phase if phase in ("connect", "read") else "poll_errors"
                stats[key] += 1
                raise
            finally:
                if stats["polls"] % STATS_EVERY == 0:
                    log_stats(stats)

        def poll_pause(exc, seconds):
            nonlocal last_ok
            elapsed = time.monotonic() - last_ok
            if elapsed >= limit:
                log(f"no successful poll for {int(elapsed)}s, exiting for restart")
                stats["stall_exits"] += 1
                raise SystemExit(POLL_STALL_EXIT)
            after = getattr(exc, "retry_after", None)
            exempt = (isinstance(exc, urllib.error.HTTPError) and exc.code == 429
                      and type(after) is int and after > 0)
            started = time.monotonic()
            pause(seconds, beat)
            if exempt:
                last_ok += time.monotonic() - started

        while offset is None:
            try:
                result = poll(offset=-1, timeout=0, allowed_updates=allowed_updates)
                offset = result[-1]["update_id"] + 1 if result else 0
                atomic_write(off, str(offset))
            except Exception as exc:
                message, seconds = poll_wait(exc)
                log(message)
                poll_pause(exc, seconds)
            else:
                heartbeat(beat)
        log("relay started, offset", offset)
        attempts = {}
        immediate_retry = False
        while True:
            try:
                updates = poll(offset=offset, timeout=LONG_POLL,
                               allowed_updates=allowed_updates)
            except Exception as exc:
                message, seconds = poll_wait(exc)
                phase = poll_phase(exc)
                if phase is not None:
                    message += f" ({phase} phase timeout)"
                if poll_retryable(exc) and not immediate_retry:
                    log(message + ", retrying now")
                    immediate_retry = True
                    heartbeat(beat)
                    continue
                log(message)
                poll_pause(exc, seconds)
                continue
            immediate_retry = False
            heartbeat(beat)
            for update in updates:
                uid = update["update_id"]
                finished = process_update(update, cfg, state, post)
                heartbeat(beat)
                if finished is not True:
                    n = attempts.get(uid, 0) + 1
                    attempts[uid] = n
                    if n < cfg["FORWARD_RETRIES"]:
                        backoff = min(5 * 2 ** min(n - 1, 4), 60)
                        delay = min(MAX_WAIT, max(backoff, getattr(finished, "after", None) or 0))
                        stats["forward_retries"] += 1
                        log("forward retry", uid, "attempt", n, "in", str(delay) + "s")
                        pause(delay, beat)
                        break
                    log("forward dropped", uid, "after", n, "attempts")
                offset = uid + 1
                atomic_write(off, str(offset))
                attempts.pop(uid, None)
    finally:
        try:
            log_stats(stats)
        finally:
            if term_installed:
                signal.signal(signal.SIGTERM, previous_term)
            if lock is not None:
                lock.close()


def main():
    try:
        run()
    except Exception as exc:
        detail = type(exc).__name__
        if isinstance(exc, OSError):
            if exc.errno is not None:
                detail += f" errno={exc.errno}"
            if exc.filename is not None:
                detail += " file=" + os.fsdecode(os.path.basename(exc.filename))
        log("fatal", detail)
        raise SystemExit(1) from None


if __name__ == "__main__":
    if sys.argv[1:] == ["--state-dir"]:
        print(state_dir())
    elif sys.argv[1:]:
        print("usage: python3 relay.py [--state-dir]", file=sys.stderr)
        raise SystemExit(2)
    else:
        main()

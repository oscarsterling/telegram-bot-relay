import copy
import importlib.util
import io
import http.client
import http.server
import threading
import shutil
import signal
import stat
import socket
import ssl
import subprocess
import urllib.error
import json
import os
import re
from pathlib import Path
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("relay", ROOT / "relay.py")
relay = importlib.util.module_from_spec(spec)
sys.dont_write_bytecode = True
spec.loader.exec_module(relay)


class RelayTests(unittest.TestCase):
    def setUp(self):
        old_umask = os.umask(0o022)
        self.addCleanup(os.umask, old_umask)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cfg = dict(RELAY_STATE_DIR=self.root, ALLOWED_CHAT_ID=-1, OWNER_USER_ID=1,
                        FORWARD_RETRIES=5, REPLY_CAP=100, HISTORY_SIZE=3, HISTORY_MAX_CHARS=10,
                        SENT_LOG=self.root / "sent.jsonl")
        self.state = relay.load_state(self.cfg)
        self.bodies = []
        self.output = io.StringIO()
        self.redirect = redirect_stdout(self.output)
        self.redirect.__enter__()
        self.addCleanup(self.redirect.__exit__, None, None, None)

    def update(self, mid, text="hello", sender=2, **fields):
        message = dict(message_id=mid, date=123, chat=dict(id=-1, type="group"),
                       **{"from": dict(id=sender, first_name="A", last_name="B", is_bot=False)})
        if text is not None:
            message["text"] = text
        message.update(fields)
        return dict(update_id=mid, message=message)

    def send(self, update):
        def post(body):
            self.bodies.append(body)
            return 200
        relay.process_update(update, self.cfg, self.state, post)

    def ids(self):
        return [e["message_id"] for e in self.state["history"]["-1"]]

    def write_sent(self, *items):
        with self.cfg["SENT_LOG"].open("wb") as out:
            for item in items:
                out.write(item if isinstance(item, bytes) else (json.dumps(item) + "\n").encode())

    def own(self, mid, date=123, text="own", chat=-1):
        return dict(chat_id=chat, message_id=mid, date=date, text=text)

    def history(self):
        return self.bodies[-1]["history"]

    @unittest.skipUnless(importlib.util.find_spec("fcntl"), "fcntl is unavailable")
    def test_kernel_lock_survives_supervisor_death(self):
        code = (
            "import importlib.util, sys, time; sys.dont_write_bytecode = True; "
            "s = importlib.util.spec_from_file_location('relay', sys.argv[1]); "
            "m = importlib.util.module_from_spec(s); s.loader.exec_module(m); "
            "lock = m.acquire_lock(sys.argv[2]); "
            "print('locked', flush=True); "
            "time.sleep(60) if len(sys.argv) > 3 else None"
        )
        args = [sys.executable, "-I", "-c", code, str(ROOT / "relay.py"), str(self.root)]
        first = subprocess.Popen(args + ["wait"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True)
        try:
            self.assertEqual(first.stdout.readline().strip(), "locked")
            second = subprocess.run(args, capture_output=True, text=True, timeout=5)
            self.assertEqual(second.returncode, 3)
            self.assertIn(f"another relay (pid {first.pid}) holds relay.lock, exiting",
                          second.stdout)
        finally:
            first.kill()
            first.communicate(timeout=5)
        self.assertEqual(first.returncode, -signal.SIGKILL)
        third = subprocess.run(args, capture_output=True, text=True, timeout=5)
        self.assertEqual(third.returncode, 0, third.stderr)
        self.assertEqual(third.stdout.strip(), "locked")
        self.assertEqual(stat.S_IMODE((self.root / "relay.lock").stat().st_mode), 0o600)

    def test_bootstrap_drains_large_backlog_and_private_files(self):
        self.cfg.update(TELEGRAM_BOT_TOKEN="fake", TELEGRAM_WEBHOOK_URL="https://example.invalid",
                        TELEGRAM_WEBHOOK_KEY="fake")
        backlog = [self.update(i) for i in range(1, 251)]
        polls, bodies = [], []
        class StopLoop(BaseException):
            pass
        def urlopen(req, timeout):
            params = json.loads(req.data)
            polls.append(params)
            offset = params.get("offset")
            result = (backlog[-1:] if offset == -1 else
                      [u for u in backlog if offset is None or u["update_id"] >= offset][:100])
            if params["timeout"] == relay.LONG_POLL:
                for update in result:
                    relay.process_update(update, self.cfg, self.state,
                                         lambda body: bodies.append(body) or 200)
                raise StopLoop()
            return io.BytesIO(json.dumps({"result": result}).encode())
        previous = os.umask(0)
        try:
            with patch.object(relay, "config", return_value=self.cfg), \
                    patch.object(relay.urllib.request.OpenerDirector, "open", side_effect=urlopen), \
                    patch.object(relay, "make_post", return_value=lambda body: bodies.append(body)):
                with self.assertRaises(StopLoop):
                    relay.run()
            self.assertEqual(os.umask(0o077), 0o077)
        finally:
            os.umask(previous)
        self.assertEqual(polls[0]["offset"], -1)
        self.assertEqual(polls[0]["timeout"], 0)
        self.assertEqual(polls[1]["offset"], 251)
        self.assertEqual(bodies, [])
        self.assertEqual((self.root / "offset").read_text(), "251")
        names = ["offset", "heartbeat"]
        if importlib.util.find_spec("fcntl"):
            names.append("relay.lock")
        for name in names:
            self.assertEqual(stat.S_IMODE((self.root / name).stat().st_mode), 0o600, name)

    @unittest.skipUnless(os.name == "posix", "directory fsync requires POSIX")
    def test_atomic_directory_fsync(self):
        target = self.root / "offset"
        calls = []
        real_fsync = os.fsync
        def fsync(fd):
            info = os.fstat(fd)
            directory = stat.S_ISDIR(info.st_mode)
            calls.append(directory)
            if directory:
                parent = target.parent.stat()
                self.assertEqual((info.st_dev, info.st_ino), (parent.st_dev, parent.st_ino))
                self.assertEqual(target.read_text(), "42")
            real_fsync(fd)
        with patch.object(relay.os, "fsync", side_effect=fsync):
            relay.atomic_write(target, "42")
        self.assertEqual(calls, [False, True])

    @unittest.skipUnless(os.name == "posix", "directory fsync requires POSIX")
    def test_directory_fsync_failure_is_error(self):
        def fsync(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError("directory sync failed")
        with patch.object(relay.os, "fsync", side_effect=fsync):
            with self.assertRaisesRegex(OSError, "directory sync failed"):
                relay.atomic_write(self.root / "offset", "42")

    def test_sent_tail_discards_partial_line(self):
        self.write_sent(self.own(1), self.own(2, text="x" * 300), self.own(3), self.own(4))
        raw = self.cfg["SENT_LOG"].read_bytes()
        start = len(raw) - 200
        self.assertGreater(start, raw.index(b"xxx"))
        self.assertLess(start, raw.index(b"xxx") + 300)
        with patch.object(relay, "SENT_LOG_TAIL_BYTES", 200):
            entries, exists = relay.sent_entries(self.cfg["SENT_LOG"], -1, 10)
        self.assertTrue(exists)
        self.assertEqual([entry["message_id"] for entry in entries], [3, 4])
        self.assertNotIn("sent log skipped", self.output.getvalue())

    def test_sent_order_and_shape(self):
        self.cfg["HISTORY_SIZE"] = 10
        self.send(self.update(1, date=10))
        self.send(self.update(4, date=40))
        self.write_sent(self.own(3, 30), self.own(2, 20), self.own(5, 40))
        self.send(self.update(9))
        self.assertEqual([e["message_id"] for e in self.history()], [1, 2, 3, 4, 5])
        self.assertEqual(self.history()[1], dict(message_id=2, date=20, from_id=None,
                         from_name="self", is_bot=True, self=True, text="own",
                         reply_to_message_id=None))

    def test_sent_merge_with_stored_none_date(self):
        buffered = relay.history_entry(self.update(1, date=None)["message"], 10)
        (self.root / "history.json").write_text(json.dumps({"-1": [buffered]}))
        self.state = relay.load_state(self.cfg)
        self.write_sent(self.own(2, date=20))
        self.send(self.update(9))
        self.assertEqual([entry["message_id"] for entry in self.history()], [1, 2])
        self.assertEqual(self.history()[0], buffered)
        self.assertTrue(self.history()[1]["self"])

    def test_sent_dedupe_and_quote(self):
        self.cfg["HISTORY_SIZE"] = 10
        quote = self.update(11, "quoted")["message"]
        self.send(self.update(12, reply_to_message=quote))
        self.write_sent(self.own(11, text="old"), self.own(11, text="last"),
                        self.own(12, text="clash"), self.own(13, text="current"))
        self.send(self.update(13))
        self.assertEqual([e["message_id"] for e in self.history()], [11, 12])
        self.assertEqual(self.history()[0]["text"], "last")
        self.assertTrue(self.history()[0]["self"])
        self.assertEqual(self.history()[1]["text"], "hello")
        self.assertEqual(self.history()[1]["reply_to"], relay.summary(quote, 10))

    def test_sent_corrupt_lines(self):
        self.write_sent(self.own(1), b"{PRIVATE-BAD-JSON\n",
                        self.own(2, text=42), b"\xffPRIVATE-UTF8\n", self.own(3), b"\n")
        self.send(self.update(9))
        self.assertEqual([e["message_id"] for e in self.history()], [1, 3])
        lines = [line for line in self.output.getvalue().splitlines() if "sent log" in line]
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].endswith(
            "sent log skipped 3 bad line(s) JSONDecodeError,UnicodeDecodeError,ValueError"))
        self.assertNotIn("PRIVATE", self.output.getvalue())

    def test_sent_shapes(self):
        invalid = [[], None, {}, self.own(True), self.own(2, date=False),
                   self.own(3, chat=True), self.own(4, text=None)]
        self.write_sent(*invalid, self.own(5))
        self.send(self.update(9))
        self.assertEqual([e["message_id"] for e in self.history()], [5])
        self.assertIn("sent log skipped 7 bad line(s) ValueError", self.output.getvalue())

    def test_sent_missing(self):
        self.send(self.update(1))
        snapshot = copy.deepcopy(self.state["history"]["-1"])
        self.send(self.update(2))
        self.assertEqual(self.history(), snapshot)
        self.assertNotIn("sent log", self.output.getvalue())
        self.assertEqual(self.bodies[-1]["history_note"], relay.HISTORY_NOTE)

    def test_sent_wrong_chat(self):
        self.write_sent(self.own(1, chat=-2), self.own(2))
        self.send(self.update(9))
        self.assertEqual([e["message_id"] for e in self.history()], [2])

    def test_sent_truncation(self):
        self.write_sent(self.own(1, text="abcdefghijk"), self.own(2, text="abcdefghij"),
                        self.own(3, text=""))
        self.send(self.update(9))
        self.assertEqual([e["text"] for e in self.history()], ["abcdefghij...", "abcdefghij", "[message]"])

    def test_sent_empty_text(self):
        self.write_sent(self.own(1, text=""))
        self.send(self.update(9))
        self.assertEqual(self.history()[0]["text"], "[message]")

    def test_sent_note(self):
        self.write_sent()
        self.send(self.update(1))
        self.assertEqual(self.bodies[-1]["history_note"], relay.SENT_HISTORY_NOTE)
        self.assertIn('marked "self": true', relay.SENT_HISTORY_NOTE)

    def test_sent_disabled(self):
        self.cfg["HISTORY_SIZE"] = 0
        self.write_sent(b"bad\n")
        update = self.update(9)
        with patch.object(relay, "sent_entries", side_effect=AssertionError("read sent log")):
            self.send(update)
        self.assertEqual(self.bodies, [update])

    def test_sent_directory(self):
        self.send(self.update(1))
        self.cfg["SENT_LOG"].mkdir()
        self.send(self.update(2))
        self.assertEqual([e["message_id"] for e in self.history()], [1])
        self.assertIn("sent log unreadable IsADirectoryError", self.output.getvalue())

    def test_sent_read_failure_discards_partial(self):
        class Broken(io.BytesIO):
            def __next__(self):
                if self.tell():
                    raise OSError("PRIVATE")
                return super().__next__()
        with patch.object(Path, "open", return_value=Broken((json.dumps(self.own(1)) + "\n").encode())):
            entries, exists = relay.sent_entries(self.cfg["SENT_LOG"], -1, 10)
        self.assertEqual(entries, [])
        self.assertTrue(exists)
        self.assertIn("sent log unreadable OSError", self.output.getvalue())
        self.assertNotIn("PRIVATE", self.output.getvalue())

    def test_sent_cap(self):
        self.send(self.update(1, date=10))
        self.write_sent(self.own(2, 20), self.own(3, 30), self.own(4, 40))
        self.send(self.update(9))
        self.assertEqual([e["message_id"] for e in self.history()], [2, 3, 4])

    def test_sent_not_persisted(self):
        self.write_sent(self.own(1))
        before = self.cfg["SENT_LOG"].read_bytes()
        self.send(self.update(9))
        self.assertEqual([e["message_id"] for e in self.history()], [1])
        persisted = json.loads((self.root / "history.json").read_text())
        self.assertEqual([e["message_id"] for e in persisted["-1"]], [9])
        self.assertEqual(self.cfg["SENT_LOG"].read_bytes(), before)

    def test_buffer_caps(self):
        for mid in range(1, 6):
            self.send(self.update(mid))
        self.assertEqual(self.ids(), [3, 4, 5])

    def test_truncation(self):
        self.send(self.update(1, "abcdefghijk"))
        entry = self.state["history"]["-1"][0]
        self.assertEqual(entry["text"], "abcdefghij...")
        self.assertEqual(len(entry["text"]), 13)
        self.send(self.update(2, "abcdefghij"))
        self.assertEqual(self.state["history"]["-1"][1]["text"], "abcdefghij")

    def test_quote(self):
        quote = self.update(9, "bot answer")["message"]
        quote["from"] = dict(id=99, username="bot", is_bot=True)
        self.send(self.update(1, reply_to_message=quote))
        entry = self.state["history"]["-1"][0]
        self.assertEqual(entry["reply_to_message_id"], 9)
        self.assertEqual(entry["reply_to"], dict(message_id=9, from_id=99,
                         from_name="bot", is_bot=True, text="bot answer"))

    def test_capped_recorded(self):
        self.cfg["REPLY_CAP"] = 1
        self.send(self.update(1))
        self.send(self.update(2))
        self.assertEqual(len(self.bodies), 1)
        self.send(self.update(3, sender=1))
        self.assertEqual([e["message_id"] for e in self.bodies[-1]["history"]], [1, 2])

    def test_excludes_current(self):
        self.send(self.update(1))
        self.send(self.update(2))
        self.assertEqual([e["message_id"] for e in self.bodies[-1]["history"]], [1])

    def test_disabled(self):
        self.cfg["HISTORY_SIZE"] = 0
        update = self.update(1)
        self.send(update)
        self.assertEqual(self.bodies, [update])
        self.assertEqual(self.state["history"], {})
        self.assertFalse((self.root / "history.json").exists())

    def test_atomic_failure(self):
        target = self.root / "history.json"
        target.write_text('{"old": []}')
        calls = []
        real_fsync = relay.os.fsync
        def fsync(fd):
            self.assertEqual(os.fstat(fd).st_size, 3)
            calls.append(("fsync", os.fstat(fd)))
            return real_fsync(fd)
        def fail(source, destination):
            calls.append(("replace", os.stat(source)))
            self.assertEqual(Path(source).parent, self.root)
            self.assertNotEqual(Path(source), target)
            self.assertEqual(Path(source).read_text(), "new")
            self.assertEqual(Path(destination), target)
            raise OSError("replace failed")
        with patch.object(relay.os, "fsync", side_effect=fsync) as sync, \
                patch.object(relay.os, "replace", side_effect=fail) as replace:
            with self.assertRaisesRegex(OSError, "replace failed"):
                relay.atomic_write(target, "new")
        sync.assert_called_once()
        replace.assert_called_once()
        self.assertEqual([name for name, _ in calls], ["fsync", "replace"])
        self.assertEqual((calls[0][1].st_dev, calls[0][1].st_ino),
                         (calls[1][1].st_dev, calls[1][1].st_ino))
        self.assertEqual(target.read_text(), '{"old": []}')
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["history.json"])

    def test_r5_zero_cap_log(self):
        self.cfg["REPLY_CAP"] = 0
        self.assertIs(relay.process_update(self.update(1), self.cfg, self.state, None), True)
        self.assertIn("capped 1 count 0", self.output.getvalue())

    def test_r5_deadline_cleanup(self):
        real = signal.setitimer
        def cancel(which, seconds):
            if seconds == 0:
                os.kill(os.getpid(), signal.SIGALRM)
            return real(which, seconds)
        previous = signal.getsignal(signal.SIGALRM)
        with patch.object(signal, "setitimer", side_effect=cancel):
            with relay.deadline(5):
                pass
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL)[0], 0)
        self.assertIs(signal.getsignal(signal.SIGALRM), previous)

    def test_r5_owner_reset_persisted(self):
        self.send(self.update(1))
        def failed(body):
            self.assertEqual(json.loads((self.root / "replycount.json").read_text()), {"-1": 0})
            return 404
        relay.process_update(self.update(2, sender=1), self.cfg, self.state, failed)
        self.assertEqual(relay.load_state(self.cfg)["counts"], {"-1": 0})

    def test_r5_poll_diagnostics(self):
        for error, expected in (
                (urllib.error.URLError(ssl.SSLCertVerificationError("PRIVATE")),
                 "poll error URLError (SSLCertVerificationError)"),
                (OSError(28, "PRIVATE", "/PRIVATE/history.json"), "poll error OSError errno=28")):
            with self.subTest(expected=expected):
                self.assertEqual(relay.poll_wait(error), (expected, 5))

    def test_r5_forward_auth(self):
        for status in (401, 403):
            with self.subTest(status=status):
                relay.process_update(self.update(status), self.cfg, self.state, lambda body: status)
                self.assertIn(f"forward failed {status} {status} not retried (check TELEGRAM_WEBHOOK_KEY)",
                              self.output.getvalue())

    def test_r5_fatal_diagnostics(self):
        with patch.object(relay, "run", side_effect=OSError(28, "PRIVATE", "/PRIVATE/history.json")):
            with self.assertRaises(SystemExit):
                relay.main()
        self.assertIn("fatal OSError errno=28 file=history.json", self.output.getvalue())
        self.assertNotIn("PRIVATE", self.output.getvalue())

    def test_r5_corrupt_basename(self):
        (self.root / "history.json").write_text("{bad")
        relay.load_state(self.cfg)
        self.assertIn("CORRUPT history.json JSONDecodeError", self.output.getvalue())
        self.assertNotIn(str(self.root), self.output.getvalue())

    def test_r5_lock_pid(self):
        lock = relay.acquire_lock(self.root)
        self.addCleanup(lock.close)
        self.assertEqual((self.root / "relay.lock").read_text().strip(), str(os.getpid()))

    def test_r5_lock_held(self):
        lock = relay.acquire_lock(self.root)
        self.addCleanup(lock.close)
        with self.assertRaises(SystemExit) as caught:
            relay.acquire_lock(self.root)
        self.assertEqual(caught.exception.code, 3)
        self.assertEqual(relay.LOCK_HELD_EXIT, 3)
        self.assertIn(f"another relay (pid {os.getpid()}) holds relay.lock, exiting", self.output.getvalue())
        self.assertNotIn(str(self.root), self.output.getvalue())

    def test_r5_history_dates(self):
        target = self.root / "history.json"
        self.write_sent(self.own(6, date=50))
        for date in ("x", True, 1.5, [], {}):
            with self.subTest(date=date):
                raw = json.dumps({"-1": [{"message_id": 5, "date": date}]})
                target.write_text(raw)
                self.state = relay.load_state(self.cfg)
                self.assertEqual(self.state["history"], {})
                self.assertTrue(any(p.read_text() == raw for p in self.root.glob("history.json.corrupt-*")))
                self.send(self.update(9))
                self.assertEqual([e["message_id"] for e in self.history()], [6])
        for entry in ({"message_id": 5}, {"message_id": 5, "date": None},
                      {"message_id": 5, "date": 1}):
            target.write_text(json.dumps({"-1": [entry]}))
            self.assertEqual(relay.load_state(self.cfg)["history"], {"-1": [entry]})

    def test_owner_reset(self):
        self.cfg["REPLY_CAP"] = 1
        self.send(self.update(1))
        self.send(self.update(2))
        self.assertEqual(len(self.bodies), 1)
        self.send(self.update(3, sender=1))
        self.assertEqual([b["update_id"] for b in self.bodies], [1, 3])
        self.assertEqual(self.state["counts"]["-1"], 1)

    def test_nontext(self):
        self.cfg["FORWARD_MEDIA"] = False
        self.send(self.update(1, None, photo=[{}]))
        self.assertEqual(self.state["history"]["-1"][0]["text"], "[photo]")
        self.assertEqual(self.bodies, [])
        self.assertIn("skipped 1", self.output.getvalue())

    def test_edit_in_place(self):
        self.cfg["FORWARD_EDITS"] = True
        self.send(self.update(1))
        self.send(self.update(2))
        edited = self.update(1, "edited")
        edited["edited_message"] = edited.pop("message")
        self.send(edited)
        self.assertEqual(self.ids(), [1, 2])
        self.assertEqual(self.state["history"]["-1"][0]["text"], "edited")
        self.assertEqual([e["message_id"] for e in self.bodies[-1]["history"]], [2])

    def test_reload(self):
        self.send(self.update(1))
        reloaded = relay.load_state(self.cfg)
        self.assertEqual(reloaded, self.state)
        self.state = reloaded
        self.send(self.update(2))
        self.assertEqual(self.bodies[-1]["history"][0]["message_id"], 1)

    def check_corrupt(self, name):
        target = self.root / name
        target.write_text("{bad")
        state = relay.load_state(self.cfg)
        self.assertEqual(state["history" if name == "history.json" else "counts"], {})
        backups = list(self.root.glob(name + ".corrupt-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), "{bad")
        self.assertFalse(target.exists())
        self.assertIn("CORRUPT " + target.name + " JSONDecodeError", self.output.getvalue())

    def test_corrupt_history(self):
        self.check_corrupt("history.json")

    def test_corrupt_counts(self):
        target = self.root / "replycount.json"
        for raw in ("{bad", "[]", '{"-1": "bad"}', '{"-1": true}'):
            with self.subTest(raw=raw):
                target.write_text(raw)
                self.state = relay.load_state(self.cfg)
                closed = {"-1": 100, "1": 100}
                self.assertEqual(self.state["counts"], closed)
                self.assertEqual(json.loads(target.read_text()), closed)
                self.assertTrue(any(p.read_text() == raw for p in self.root.glob("replycount.json.corrupt-*")))
                self.bodies.clear()
                self.send(self.update(1))
                self.assertEqual(self.bodies, [])
                self.send(self.update(2, sender=1))
                self.assertEqual([b["update_id"] for b in self.bodies], [2])
        self.assertIn("all chats capped", self.output.getvalue())

    def test_raw_unchanged(self):
        update = self.update(1, extra={"nested": [1, 2]})
        update["other"] = {"value": True}
        original = copy.deepcopy(update)
        self.send(update)
        self.assertEqual(update, original)
        self.assertEqual({k: self.bodies[-1][k] for k in original}, original)
        self.bodies[-1]["message"]["text"] = "changed"
        self.assertEqual(self.state["history"]["-1"][0]["text"], "hello")

    def test_allow_rule(self):
        denied = self.update(1)
        denied["message"]["chat"]["id"] = -2
        self.send(denied)
        self.assertEqual(self.state["history"], {})
        self.assertEqual(self.bodies, [])
        private = self.update(2, sender=1)
        private["message"]["chat"] = dict(id=1, type="private")
        self.send(private)
        self.assertEqual(self.bodies[-1]["update_id"], 2)

    def test_caption(self):
        self.send(self.update(1, None, photo=[{}], caption="caption"))
        self.assertEqual(self.state["history"]["-1"][0]["text"], "caption")
        self.assertEqual(len(self.bodies), 1)

    def test_snapshot_independent(self):
        self.send(self.update(1))
        self.send(self.update(2))
        self.bodies[-1]["history"][0]["text"] = "changed"
        self.assertEqual(self.state["history"]["-1"][0]["text"], "hello")

    def test_main_offsets_and_http(self):
        self.cfg.update(TELEGRAM_BOT_TOKEN="fake", TELEGRAM_WEBHOOK_URL="https://example.invalid",
                        TELEGRAM_WEBHOOK_KEY="fake", REPLY_CAP=1)
        calls = []
        class StopLoop(BaseException):
            pass
        class Response(io.BytesIO):
            status = 202
        polls = iter([dict(result=[dict(update_id=6)]),
                      dict(result=[self.update(7), self.update(8)])])
        def urlopen(req, timeout):
            calls.append((req, timeout))
            if req.full_url == "https://example.invalid":
                self.assertEqual(req.get_header("Authorization"), "Bearer fake")
                return Response(b"")
            try:
                return Response(json.dumps(next(polls)).encode())
            except StopIteration:
                raise StopLoop()
        with patch.object(relay, "config", return_value=self.cfg), \
                patch.object(relay.socket, "setdefaulttimeout") as default_timeout, \
                patch.object(relay.urllib.request.OpenerDirector, "open", side_effect=urlopen):
            with self.assertRaises(StopLoop):
                relay.main()
        default_timeout.assert_called_once_with(relay.SOCKET_TIMEOUT)
        self.assertEqual((self.root / "offset").read_text(), "9")
        self.assertEqual([timeout for _, timeout in calls], [relay.API_TIMEOUT, relay.API_TIMEOUT, relay.POST_TIMEOUT, relay.API_TIMEOUT])
        self.assertEqual(json.loads(calls[0][0].data), {"offset": -1, "timeout": 0, "allowed_updates": ["message"]})
        self.assertEqual(json.loads(calls[1][0].data)["offset"], 7)
        self.assertEqual(json.loads(calls[-1][0].data)["offset"], 9)
        self.assertEqual(json.loads(calls[-1][0].data)["allowed_updates"], ["message"])

    def test_post_failure_does_not_use_cap(self):
        def post(body):
            raise OSError("offline")
        relay.process_update(self.update(1), self.cfg, self.state, post)
        self.assertEqual(self.state["counts"].get("-1", 0), 0)
        self.assertIn("forward error 1 OSError", self.output.getvalue())
        self.assertNotIn("hello", self.output.getvalue())

    def test_sender_allowlist(self):
        self.cfg["ALLOWED_SENDER_IDS"] = {3}
        self.send(self.update(1, sender=2))
        self.assertEqual(self.bodies, [])
        self.assertEqual(self.state["history"], {})
        self.send(self.update(2, sender=3))
        self.assertEqual([b["update_id"] for b in self.bodies], [2])
        self.send(self.update(3, sender=1))
        self.assertEqual([b["update_id"] for b in self.bodies], [2, 3])

    def test_sender_allowlist_off(self):
        self.send(self.update(1, sender=42))
        self.assertEqual([b["update_id"] for b in self.bodies], [1])

    def test_edits_default_skip(self):
        update = self.update(1)
        update["edited_message"] = update.pop("message")
        self.send(update)
        self.assertEqual(self.bodies, [])
        self.assertEqual(self.state["history"], {})
        self.assertIn("skipped 1", self.output.getvalue())

    def test_owner_edit_does_not_reset(self):
        self.cfg.update(FORWARD_EDITS=True, REPLY_CAP=1)
        self.send(self.update(1))
        update = self.update(2, sender=1)
        update["edited_message"] = update.pop("message")
        self.send(update)
        self.assertEqual([b["update_id"] for b in self.bodies], [1])
        self.assertEqual(self.state["counts"]["-1"], 1)

    def test_status_controls_cap(self):
        for status, expected in ((500, 0), (200, 1)):
            with self.subTest(status=status):
                relay.process_update(self.update(status), self.cfg, self.state, lambda body: status)
                self.assertEqual(self.state["counts"].get("-1", 0), expected)
        self.assertEqual(json.loads((self.root / "replycount.json").read_text()), {"-1": 1})

    def test_http_error_does_not_use_cap(self):
        error = urllib.error.HTTPError("http://fake", 302, "redirect", {}, io.BytesIO(b""))
        self.addCleanup(error.close)
        def post(body):
            raise error
        relay.process_update(self.update(1), self.cfg, self.state, post)
        self.assertEqual(self.state["counts"], {})
        self.assertIn("forward failed 1 302", self.output.getvalue())

    def poll_main(self, edited=False, corrupt=False, fail=False):
        self.cfg.update(TELEGRAM_BOT_TOKEN="fake", TELEGRAM_WEBHOOK_URL="https://example.invalid",
                        TELEGRAM_WEBHOOK_KEY="fake", FORWARD_EDITS=edited)
        if corrupt:
            (self.root / "offset").write_text("garbage")
        calls = []
        class StopLoop(BaseException):
            pass
        replies = [dict(result=[dict(update_id=41)])]
        if fail:
            replies.insert(0, http.client.InvalidURL("nonnumeric port: '/botSECRETTOKEN/getUpdates'"))
        def urlopen(req, timeout):
            calls.append(json.loads(req.data))
            if not replies:
                raise StopLoop()
            result = replies.pop(0)
            if isinstance(result, Exception):
                raise result
            return io.BytesIO(json.dumps(result).encode())
        err = io.StringIO()
        with patch.object(relay, "config", return_value=self.cfg), \
                patch.object(relay.urllib.request.OpenerDirector, "open", side_effect=urlopen), \
                patch.object(relay.time, "sleep") as sleep, redirect_stderr(err):
            with self.assertRaises(StopLoop):
                relay.main()
        if fail:
            sleep.assert_called_once_with(5)
        self.assertNotIn("SECRETTOKEN", self.output.getvalue() + err.getvalue())
        return calls

    def test_bootstrap_retry_safe(self):
        calls = self.poll_main(fail=True)
        self.assertIn("poll error InvalidURL", self.output.getvalue())
        self.assertEqual(len(calls), 3)

    def test_enabled_poll_updates(self):
        calls = self.poll_main(edited=True)
        for call in calls:
            self.assertEqual(call["allowed_updates"], ["message", "edited_message"])

    def test_corrupt_offset(self):
        self.poll_main(corrupt=True)
        self.assertEqual((self.root / "offset").read_text(), "42")
        backups = list(self.root.glob("offset.corrupt-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), "garbage")
        self.assertIn("CORRUPT offset" + " ValueError", self.output.getvalue())

    def test_fatal_safe(self):
        err = io.StringIO()
        with patch.object(relay, "load_state", side_effect=RuntimeError("SECRETTOKEN")), \
                patch.object(relay, "config", return_value=self.cfg), redirect_stderr(err):
            with self.assertRaises(SystemExit) as caught:
                relay.main()
        self.assertNotEqual(caught.exception.code, 0)
        self.assertIn("fatal RuntimeError", self.output.getvalue())
        self.assertNotIn("SECRETTOKEN", self.output.getvalue() + err.getvalue())

    def supervisor(self, stub, max_age="2", state=None, own_pid=False, extra_env=None, shell="bash"):
        # Fail before spawning when the sandbox cannot guarantee group cleanup.
        os.killpg(os.getpgrp(), 0)
        if not (self.root / "run.sh").exists():
            shutil.copy2(ROOT / "run.sh", self.root / "run.sh")
            resolver = ("import os, sys\nfrom pathlib import Path\n"
                        "if sys.argv[1:] == ['--state-dir']:\n"
                        "    state = Path(os.environ['RELAY_STATE_DIR'].strip()).resolve()\n"
                        "    state.mkdir(mode=0o700, parents=True, exist_ok=True)\n"
                        "    print(state)\n    raise SystemExit(0)\n")
            (self.root / "relay.py").write_text(resolver + stub)
        env = dict(os.environ, HEARTBEAT_MAX_AGE=max_age, RELAY_STATE_DIR=str(self.root) if state is None else state,
                   HOME=str(self.root))
        env.update(extra_env or {})
        args = [shell, str(self.root / "run.sh")]
        if own_pid:
            args = ["bash", "-c", 'echo $$ > "$1/run.pid"; exec bash "$1/run.sh"',
                    "bash", str(self.root)]
        process = subprocess.Popen(args, env=env,
                                   start_new_session=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        def cleanup():
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.communicate(timeout=7)
            finally:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.communicate(timeout=5)
        self.addCleanup(cleanup)
        return process

    def env_file(self, content, name=".env", mode=0o600):
        path = self.root / name
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        with os.fdopen(fd, "w") as out:
            out.write(content)
        path.chmod(mode)
        return path

    def env_supervisor(self, names=("RELAY_TEST_A", "RELAY_TEST_B"), **kwargs):
        stub = ("import json, os, time\nfrom pathlib import Path\n"
                "Path('env-seen').write_text(json.dumps([os.environ.get(name) for name in "
                + repr(names) + "]))\n"
                "time.sleep(60)\n")
        return self.supervisor(stub, shell="/bin/bash" if os.path.exists("/bin/bash") else "bash",
                               **kwargs)

    def test_env_file_loaded(self):
        expected = "single' double\" dollar$ slash\\\nnext line\n"
        self.env_file("RELAY_TEST_A='hello world'\n"
                      r"RELAY_TEST_B=$'single\' double\" dollar$ slash\\\nnext line\n'" + "\n")
        process = self.env_supervisor()
        self.assertEqual(json.loads(self.await_file("env-seen")), ["hello world", expected])
        self.assertIsNone(process.poll())
        self.assertEqual((self.root / "run.pid").read_text(), str(process.pid) + "\n")
        self.assertNotIn("env file", (self.root / "relay.log").read_text())

    def test_env_file_world_writable_refused(self):
        for mode in (0o666, 0o620):
            with self.subTest(mode=oct(mode)):
                self.env_file("RELAY_TEST_A='ENV-PRIVATE-WRITABLE'\n", mode=mode)
                process = self.env_supervisor()
                process.communicate(timeout=3)
                self.assertEqual(process.returncode, 1)
                log = (self.root / "relay.log").read_text()
                self.assertIn("env file is group or world writable, refusing to source it (chmod 600), exiting", log)
                self.assertFalse((self.root / "env-seen").exists())
                self.assertNotIn("ENV-PRIVATE-WRITABLE", log)

    @unittest.skipUnless(Path("/etc/hosts").exists() and
                         Path("/etc/hosts").stat().st_uid != os.getuid(),
                         "/etc/hosts is absent or owned by this user")
    def test_env_file_not_owned_refused(self):
        process = self.env_supervisor(extra_env={"RELAY_ENV_FILE": "/etc/hosts"})
        process.communicate(timeout=3)
        self.assertEqual(process.returncode, 1)
        self.assertIn("env file is not owned by you, refusing to source it, exiting",
                      (self.root / "relay.log").read_text())
        self.assertFalse((self.root / "env-seen").exists())

    def test_env_file_exported_wins(self):
        self.env_file("RELAY_TEST_A='inner'\nRELAY_TEST_C='filled'\n")
        process = self.env_supervisor(names=("RELAY_TEST_A", "RELAY_TEST_C"),
                                      extra_env={"RELAY_TEST_A": "outer"})
        self.assertEqual(json.loads(self.await_file("env-seen")), ["outer", "filled"])
        self.assertIsNone(process.poll())

    def test_env_file_custom_and_missing(self):
        custom = self.env_file("RELAY_TEST_A='custom'\n", name="custom env")
        process = self.env_supervisor(extra_env={"RELAY_ENV_FILE": str(custom)})
        self.assertEqual(json.loads(self.await_file("env-seen")), ["custom", None])
        process.terminate()
        process.communicate(timeout=7)
        missing = self.env_supervisor(extra_env={"RELAY_ENV_FILE": "missing"})
        missing.communicate(timeout=3)
        self.assertEqual(missing.returncode, 1)
        self.assertIn("env file RELAY_ENV_FILE not found, exiting",
                      (self.root / "relay.log").read_text())
        before = (self.root / "env-seen").stat().st_mtime_ns
        plain = self.env_supervisor()
        until = relay.time.monotonic() + 3
        while (self.root / "env-seen").stat().st_mtime_ns == before and relay.time.monotonic() < until:
            relay.time.sleep(0.02)
        self.assertNotEqual((self.root / "env-seen").stat().st_mtime_ns, before)
        self.assertEqual(json.loads((self.root / "env-seen").read_text()), [None, None])
        self.assertIsNone(plain.poll())

    def test_env_file_syntax_error(self):
        self.env_file("RELAY_TEST_A='unterminated ENV-PRIVATE-SYNTAX\n")
        process = self.env_supervisor()
        process.communicate(timeout=3)
        self.assertEqual(process.returncode, 1)
        log = (self.root / "relay.log").read_text()
        self.assertIn("env file failed to load, exiting", log)
        self.assertNotIn("ENV-PRIVATE-SYNTAX", log)
        self.assertFalse((self.root / "env-seen").exists())

    def test_env_file_command_failure(self):
        self.env_file("false\nRELAY_TEST_A='must not load'\n")
        process = self.env_supervisor()
        process.communicate(timeout=3)
        self.assertEqual(process.returncode, 1)
        self.assertIn("env file failed to load, exiting", (self.root / "relay.log").read_text())
        self.assertFalse((self.root / "env-seen").exists())

    def test_env_file_error_does_not_leak(self):
        self.env_file("RELAY_TEST_A=ENV-PRIVATE-VALUE ENV-PRIVATE-COMMAND\n")
        process = self.env_supervisor()
        stdout, stderr = process.communicate(timeout=3)
        self.assertEqual(process.returncode, 1)
        log = (self.root / "relay.log").read_text()
        self.assertIn("env file failed to load, exiting", log)
        for private in ("ENV-PRIVATE-COMMAND", "ENV-PRIVATE-VALUE"):
            self.assertNotIn(private, log)
            self.assertNotIn(private.encode(), stdout)
            self.assertNotIn(private.encode(), stderr)
        self.assertFalse((self.root / "env-seen").exists())

    def test_env_file_unset_reference(self):
        self.env_file("unset RELAY_TEST_UNSET\nRELAY_TEST_A=$RELAY_TEST_UNSET\n")
        process = self.env_supervisor()
        process.communicate(timeout=3)
        self.assertEqual(process.returncode, 1)
        self.assertIn("env file failed to load, exiting", (self.root / "relay.log").read_text())
        self.assertFalse((self.root / "env-seen").exists())

    @unittest.skipUnless(os.path.exists("/bin/bash"), "/bin/bash is unavailable")
    def test_run_sh_syntax(self):
        for shell in ("/bin/bash", "bash"):
            with self.subTest(shell=shell):
                result = subprocess.run([shell, "-n", str(ROOT / "run.sh")],
                                        capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stderr, "")

    def await_file(self, name, expected=None, timeout=3):
        path = self.root / name
        until = relay.time.monotonic() + timeout
        while relay.time.monotonic() < until:
            if path.exists():
                value = path.read_text()
                if value and (expected is None or value == expected):
                    return value
            relay.time.sleep(0.02)
        self.fail("missing file value: " + str(path))

    def fresh_supervisor(self, max_age="2"):
        return self.supervisor(
            "import os, time\nfrom pathlib import Path\n"
            "root = Path(os.environ['RELAY_STATE_DIR'])\n"
            "(root / 'child.pid').write_text(str(os.getpid()))\n"
            "counter = 0\nwhile True:\n"
            "    counter += 1\n"
            "    (root / 'heartbeat').write_text(str(counter))\n"
            "    time.sleep(0.1)\n", max_age)

    def fake_ps(self, body):
        import shlex
        directory = self.root / "bin"
        directory.mkdir()
        path = directory / "ps"
        path.write_text("#!/bin/bash\n" + body + "\nexec " +
                        shlex.quote(shutil.which("ps")) + ' "$@"\n')
        path.chmod(0o700)
        return {"PATH": str(directory) + os.pathsep + os.environ["PATH"]}

    def test_round5_rotation_start(self):
        self.rotation_failure(True)

    def test_round5_rotation_loop(self):
        self.rotation_failure(False)

    def rotation_failure(self, startup):
        rotated = self.root / "relay.log.1"
        rotated.write_text("reserved")
        rotated.chmod(0)
        self.addCleanup(rotated.chmod, 0o600)
        if startup:
            (self.root / "relay.log").write_bytes(b"x" * 5242881)
        process = self.fresh_supervisor()
        child = int(self.await_file("child.pid"))
        if not startup:
            with (self.root / "relay.log").open("ab") as out:
                out.write(b"x" * 5242881)
        self.await_log("log rotation failed, continuing", timeout=4)
        before = (self.root / "heartbeat").read_text()
        relay.time.sleep(1.2)
        self.assertIsNone(process.poll())
        os.kill(child, 0)
        self.assertNotEqual((self.root / "heartbeat").read_text(), before)

    def test_round5_ps_old_pid_failure(self):
        old_pid = os.getpid()
        env = self.fake_ps(f'case " $* " in *" {old_pid} "*) exit 1 ;; esac')
        (self.root / "run.pid").write_text(str(old_pid))
        process = self.supervisor("from pathlib import Path\nimport time\n"
                                  "Path('started').write_text('yes')\ntime.sleep(60)\n",
                                  extra_env=env)
        self.await_file("started", "yes")
        self.assertIsNone(process.poll())
        self.assertEqual((self.root / "run.pid").read_text(), str(process.pid) + "\n")

    def test_round5_parentage(self):
        # A different reported parent stands in for a reused child pid.
        env = self.fake_ps('if [ "$1" = "-o" ] && [ "$2" = "ppid=" ] && '
                           '[ -f child.pid ] && [ "$4" = "$(cat child.pid)" ]; then\n'
                           '  echo 1\n  exit 0\nfi')
        process = self.supervisor(
            "import os, signal, time\nfrom pathlib import Path\n"
            "signal.signal(signal.SIGTERM, lambda *args: (Path('termed').write_text('yes'), os._exit(0)))\n"
            "Path('child.pid').write_text(str(os.getpid()))\n"
            "Path('heartbeat').write_text('1')\n"
            "while True:\n    Path('beating').write_text(str(time.monotonic()))\n"
            "    time.sleep(0.1)\n", extra_env=env)
        self.await_file("child.pid")
        before = self.await_file("beating")
        relay.time.sleep(5)
        self.assertIsNone(process.poll())
        self.assertNotEqual((self.root / "beating").read_text(), before)
        self.assertFalse((self.root / "termed").exists())
        self.assertNotIn("heartbeat stale", (self.root / "relay.log").read_text())

    def test_round5_stop_child_parentage(self):
        env = self.fake_ps('if [ "$1" = "-o" ] && [ "$2" = "ppid=" ] && '
                           '[ -f foreign ] && [ "$4" = "$(cat child.pid)" ]; then\n'
                           '  echo 1\n  exit 0\nfi')
        process = self.supervisor(
            "import os, signal, time\nfrom pathlib import Path\n"
            "signal.signal(signal.SIGTERM, lambda *args: (Path('termed').write_text('yes'), os._exit(0)))\n"
            "Path('child.pid').write_text(str(os.getpid()))\n"
            "counter = 0\nwhile True:\n"
            "    counter += 1\n"
            "    Path('heartbeat').write_text(str(counter))\n"
            "    time.sleep(0.1)\n", max_age="180", extra_env=env)
        self.await_file("child.pid")
        before = self.await_file("heartbeat")
        (self.root / "foreign").touch()
        process.send_signal(signal.SIGTERM)
        until = relay.time.monotonic() + 1.5
        while relay.time.monotonic() < until:
            self.assertFalse((self.root / "termed").exists(),
                             "stop_child signalled a child with a foreign parent")
            relay.time.sleep(0.02)
        self.assertFalse((self.root / "termed").exists(),
                         "stop_child signalled a child with a foreign parent")
        self.assertNotEqual((self.root / "heartbeat").read_text(), before)

    def test_round5_pidfile_create_failure(self):
        dead = subprocess.Popen([sys.executable, "-I", "-c", "pass"])
        dead.wait(timeout=3)
        with self.assertRaises(ProcessLookupError):
            os.kill(dead.pid, 0)
        state = self.root / "state"
        state.mkdir(mode=0o700)
        (state / "run.pid").write_text(str(dead.pid) + "\n")
        state.chmod(0o500)
        self.addCleanup(state.chmod, 0o700)
        process = self.supervisor("raise AssertionError('child must not start')\n",
                                  state=str(state))
        process.communicate(timeout=2)
        self.assertEqual(process.returncode, 1)
        log = (self.root / "relay.log").read_text()
        self.assertIn("cannot create pidfile run.pid, exiting", log)
        self.assertNotIn(str(state), log)

    def test_round5_heartbeat_write_failure(self):
        state = self.root / "state"
        state.mkdir(mode=0o700)
        (state / "heartbeat").mkdir()
        process = self.supervisor("from pathlib import Path\n"
                                  "Path('started').write_text('started')\n",
                                  state=str(state))
        log = self.await_log("cannot write heartbeat, retrying in", timeout=3)
        self.assertIsNone(process.poll())
        self.assertFalse((self.root / "started").exists())
        self.assertNotIn(str(state), log)

    def test_round5_stop_child_prompt(self):
        process = self.supervisor(
            "import os, signal, time\nfrom pathlib import Path\n"
            "signal.signal(signal.SIGTERM, lambda *args: exit(0))\n"
            "Path('child.pid').write_text(str(os.getpid()))\n"
            "while True: time.sleep(0.1)\n", max_age="180")
        child = int(self.await_file("child.pid"))
        started = relay.time.monotonic()
        process.terminate()
        process.communicate(timeout=2)
        self.assertLess(relay.time.monotonic() - started, 2)
        self.assertEqual(process.returncode, 0)
        with self.assertRaises(ProcessLookupError):
            os.kill(child, 0)
        # Timing excludes a five-second poll, but cannot detect an immediate stray KILL.

    def test_round5_lock_held(self):
        process = self.supervisor("print('another relay (pid 123) holds relay.lock, exiting', flush=True)\n"
                                  "raise SystemExit(3)\n")
        log = self.await_log("probably an orphan left by a killed supervisor", timeout=4)
        self.assertEqual(log.count("relay.lock is held by another relay"), 1)
        self.assertIn("stop that process to let this supervisor take over", log)
        self.assertIn("relay exited (rc=3), restarting in 10s",
                      self.await_log("restarting in 10s"))
        relay.time.sleep(1)
        self.assertIsNone(process.poll())
        self.assertEqual((self.root / "relay.log").read_text().count("holds relay.lock, exiting"), 1)

    def test_round5_ps_self_check(self):
        env = self.fake_ps('case " $* " in *" ppid= "*) exit 0 ;; esac')
        process = self.supervisor("from pathlib import Path\nimport time\n"
                                  "Path('started').write_text('yes')\ntime.sleep(60)\n",
                                  extra_env=env)
        process.communicate(timeout=2)
        self.assertEqual(process.returncode, 1)
        self.assertIn("ps -o ppid= -p is not supported here (BusyBox ps?); install procps",
                      (self.root / "relay.log").read_text())
        self.assertFalse((self.root / "started").exists())

    def test_supervisor_duplicate_refused(self):
        first = self.fresh_supervisor()
        self.await_file("run.pid", str(first.pid) + "\n")
        self.await_file("child.pid")
        second = self.fresh_supervisor()
        second.communicate(timeout=3)
        self.assertEqual(second.returncode, 1)
        self.assertIn("already running (pid " + str(first.pid) + ")",
                      (self.root / "relay.log").read_text())
        self.assertEqual((self.root / "run.pid").read_text(), str(first.pid) + "\n")
        self.assertIsNone(first.poll())

    def test_supervisor_dead_pid_replaced(self):
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait(timeout=3)
        with self.assertRaises(ProcessLookupError):
            os.kill(dead.pid, 0)
        self.check_stale_pid(dead.pid)

    def test_supervisor_other_command_replaced(self):
        self.check_stale_pid(os.getpid())

    def check_stale_pid(self, old_pid):
        (self.root / "run.pid").write_text(str(old_pid) + "\n")
        process = self.fresh_supervisor()
        self.await_file("run.pid", str(process.pid) + "\n")
        self.await_file("child.pid")
        self.assertIn("removing stale pidfile (pid " + str(old_pid) + ")",
                      (self.root / "relay.log").read_text())
        self.assertIsNone(process.poll())

    def test_supervisor_term_cleanup(self):
        process = self.fresh_supervisor("180")
        self.await_file("run.pid", str(process.pid) + "\n")
        child = int(self.await_file("child.pid"))
        started = relay.time.monotonic()
        process.send_signal(signal.SIGTERM)
        process.communicate(timeout=3)
        self.assertLess(relay.time.monotonic() - started, 3)
        self.assertEqual(process.returncode, 0)
        self.assertFalse((self.root / "run.pid").exists())
        with self.assertRaises(ProcessLookupError):
            os.kill(child, 0)

    def test_supervisor_term_during_backoff(self):
        process = self.supervisor("raise SystemExit(0)\n")
        supervisor_pid = process.pid
        self.await_log("restarting in 10s")
        before = subprocess.check_output(
            ["ps", "-A", "-o", "pid=,ppid=,command="], text=True)
        sleeps = []
        for line in before.splitlines():
            fields = line.split(None, 2)
            if len(fields) == 3 and int(fields[1]) == supervisor_pid:
                if Path(fields[2].split()[0]).name == "sleep":
                    sleeps.append(int(fields[0]))
        self.assertTrue(sleeps, "backoff sleep was not observed")
        started = relay.time.monotonic()
        process.send_signal(signal.SIGTERM)
        process.communicate(timeout=3)
        self.assertLess(relay.time.monotonic() - started, 3)
        self.assertEqual(process.returncode, 0)
        self.assertFalse((self.root / "run.pid").exists())
        after = subprocess.check_output(
            ["ps", "-A", "-o", "ppid=,command="], text=True)
        for line in after.splitlines():
            fields = line.split(None, 1)
            if len(fields) == 2 and int(fields[0]) == supervisor_pid:
                self.assertNotEqual(Path(fields[1].split()[0]).name, "sleep")
        # Reparenting must not hide a sleep leaked by the supervisor.
        for pid in sleeps:
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)

    def test_supervisor_cleanup_keeps_foreign_pidfile(self):
        process = self.fresh_supervisor()
        self.await_file("run.pid", str(process.pid) + "\n")
        self.await_file("child.pid")
        (self.root / "run.pid").write_text("999999\n")
        process.send_signal(signal.SIGTERM)
        process.communicate(timeout=7)
        self.assertTrue((self.root / "run.pid").exists())
        self.assertEqual((self.root / "run.pid").read_text(), "999999\n")

    def await_log(self, text, timeout=12):
        until = relay.time.monotonic() + timeout
        path = self.root / "relay.log"
        while relay.time.monotonic() < until:
            if path.exists() and text in path.read_text():
                return path.read_text()
            relay.time.sleep(0.05)
        self.fail("missing log line: " + text + "; log=" +
                  (path.read_text()[-2000:] if path.exists() else "<missing>"))

    @unittest.skipUnless(shutil.which("bash"), "bash is not on PATH")
    def test_log_rotation_before_start(self):
        size = 5242881
        (self.root / "relay.log").write_bytes(b"x" * size)
        self.supervisor(
            "import os, time\nfrom pathlib import Path\n"
            "root = Path(os.environ['RELAY_STATE_DIR'])\n"
            "(root / 'started').write_text('started')\n"
            "counter = 0\nwhile True:\n"
            "    counter += 1\n"
            "    (root / 'heartbeat').write_text(str(counter))\n"
            "    time.sleep(0.1)\n", max_age="180")
        self.await_file("started", "started")
        started = relay.time.monotonic()
        # The first watchdog tick is 30 seconds away, so only startup can rotate.
        self.assertEqual((self.root / "relay.log.1").stat().st_size, size)
        self.assertLess((self.root / "relay.log").stat().st_size, 1000)
        self.assertLess(relay.time.monotonic() - started, 3)

    @unittest.skipUnless(shutil.which("bash"), "bash is not on PATH")
    def test_log_rotation(self):
        size = 5242881
        (self.root / "relay.log").write_bytes(b"x" * size)
        self.supervisor("raise SystemExit(0)\n")
        self.await_log("relay exited (rc=0), restarting in 10s", timeout=4)
        self.assertEqual((self.root / "relay.log.1").stat().st_size, size)
        self.assertLess((self.root / "relay.log").stat().st_size, 1000)

    @unittest.skipUnless(shutil.which("bash"), "bash is not on PATH")
    def test_supervisor_stale(self):
        self.supervisor("import os, time\nfrom pathlib import Path\n"
                        "Path('heartbeat').write_text('1')\n"
                        "Path('pid').write_text(str(os.getpid()))\n"
                        "Path('started').write_text('started')\ntime.sleep(60)\n")
        log = self.await_log("relay killed (heartbeat stale), restarting in 10s")
        self.assertIn("relay heartbeat stale (no progress for 3s > 2s), killing pid ", log)
        self.assertEqual(log.count("restarting in"), 1)
        self.assertEqual((self.root / "started").read_text(), "started")
        pid = int((self.root / "pid").read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    @unittest.skipUnless(shutil.which("bash"), "bash is not on PATH")
    def test_supervisor_fresh(self):
        self.supervisor("import time\nfrom pathlib import Path\n"
                        "for counter in range(1, 18):\n"
                        "    Path('heartbeat').write_text(str(counter))\n"
                        "    time.sleep(0.3)\n")
        log = self.await_log("relay exited (rc=0), restarting in 10s")
        self.assertNotIn("heartbeat stale", log)
        self.assertEqual(log.count("restarting in"), 1)

    def test_supervisor_idle_resets_on_progress(self):
        # Safe pauses add up to more than the limit, so progress must reset idle.
        self.supervisor("import time\nfrom pathlib import Path\n"
                        "for counter in range(1, 5):\n"
                        "    Path('heartbeat').write_text(str(counter))\n"
                        "    time.sleep(2.5)\n", max_age="4")
        log = self.await_log("relay exited (rc=0)", timeout=15)
        self.assertNotIn("heartbeat stale", log)
        self.assertIn("relay exited (rc=0)", log)

    def test_supervisor_same_value_stale(self):
        self.supervisor("import time\nfrom pathlib import Path\n"
                        "while True:\n"
                        "    Path('heartbeat').write_text('7')\n"
                        "    time.sleep(0.2)\n")
        log = self.await_log("relay killed (heartbeat stale), restarting in 10s")
        self.assertIn("no progress for 3s > 2s", log)

    def test_supervisor_decreasing_progress(self):
        self.supervisor("import time\nfrom pathlib import Path\n"
                        "for value in (5, 4, 3, 2, 1):\n"
                        "    Path('heartbeat').write_text(str(value))\n"
                        "    time.sleep(0.3)\n")
        log = self.await_log("relay exited (rc=0), restarting in 10s")
        self.assertNotIn("heartbeat stale", log)

    def test_supervisor_padded_state(self):
        state = self.root / "state"
        process = self.supervisor("import time\ntime.sleep(60)\n",
                                  state="  " + str(state) + "  ")
        self.await_file("state/run.pid", str(process.pid) + "\n")
        self.assertEqual(self.await_file("state/heartbeat"), "0\n")
        self.assertFalse((self.root / "run.pid").exists())

    def test_supervisor_own_pid_stale(self):
        process = self.supervisor("from pathlib import Path\nimport time\n"
                                  "Path('started').write_text('yes')\ntime.sleep(60)\n",
                                  own_pid=True)
        self.await_file("started", "yes")
        self.assertEqual((self.root / "run.pid").read_text(), str(process.pid) + "\n")
        self.assertIn("removing stale pidfile (pid " + str(process.pid) + ")",
                      (self.root / "relay.log").read_text())

    def test_supervisor_umask(self):
        process = self.fresh_supervisor()
        self.await_file("run.pid", str(process.pid) + "\n")
        self.await_file("child.pid")
        for name in ("run.pid", "relay.log"):
            self.assertEqual(stat.S_IMODE((self.root / name).stat().st_mode), 0o600, name)

    def test_supervisor_in_loop_rotation(self):
        process = self.supervisor(
            "import os, time\nfrom pathlib import Path\n"
            "Path('child.pid').write_text(str(os.getpid()))\n"
            "for _ in range(3):\n    os.write(1, b'x\\n' * 1048576)\n"
            "for counter in range(1, 11):\n"
            "    Path('heartbeat').write_text(str(counter))\n"
            "    time.sleep(0.3)\n")
        child = int(self.await_file("child.pid"))
        until = relay.time.monotonic() + 2.5
        rotated = self.root / "relay.log.1"
        while relay.time.monotonic() < until:
            if rotated.exists() and (self.root / "relay.log").stat().st_size < 5242880:
                break
            relay.time.sleep(0.02)
        self.assertTrue(rotated.exists())
        self.assertGreater(rotated.stat().st_size, 5242880)
        self.assertLess((self.root / "relay.log").stat().st_size, 5242880)
        os.kill(child, 0)
        self.assertIsNone(process.poll())

    def test_timeout_relationships(self):
        # Two address attempts must leave enough time for a healthy response.
        self.assertLess(2 * relay.CONNECT_TIMEOUT + relay.LONG_POLL, relay.API_DEADLINE)
        self.assertLess(2 * relay.CONNECT_TIMEOUT + relay.POST_TIMEOUT, relay.POST_DEADLINE)
        default = re.search(r"HEARTBEAT_MAX_AGE-(\d+)", (ROOT / "run.sh").read_text())
        self.assertIsNotNone(default)
        self.assertLess(relay.API_DEADLINE + relay.POST_DEADLINE + 60,
                        int(default.group(1)))

    @unittest.skipUnless(shutil.which("bash"), "bash is not on PATH")
    def test_supervisor_invalid_age(self):
        for value in ("x", "0", "00"):
            with self.subTest(value=value):
                process = self.supervisor("from pathlib import Path\nPath('started').touch()\n", value)
                process.communicate(timeout=2)
                self.assertEqual(process.returncode, 1)
                self.assertIn("HEARTBEAT_MAX_AGE must be a positive integer",
                              (self.root / "relay.log").read_text())
                self.assertFalse((self.root / "started").exists())

    def test_history_shapes(self):
        target = self.root / "history.json"
        for value in ({"-1": {}}, {"-1": [1]}, {"-1": [{"message_id": "x"}]},
                      {"-1": [{"message_id": True}]}, []):
            with self.subTest(value=value):
                raw = json.dumps(value)
                target.write_text(raw)
                self.state = relay.load_state(self.cfg)
                self.assertEqual(self.state["history"], {})
                self.assertTrue(any(p.read_text() == raw for p in self.root.glob("history.json.corrupt-*")))
                self.assertIn("CORRUPT " + target.name + " ValueError", self.output.getvalue())
                self.send(self.update(1))

    def test_finished_values(self):
        for status, expected in ((200, True), (500, False), (429, False), (404, True)):
            with self.subTest(status=status):
                result = relay.process_update(self.update(status), self.cfg, self.state,
                                              lambda body: status)
                if expected:
                    self.assertIs(result, True)
                else:
                    self.assertIsInstance(result, relay.Retry)
                    self.assertIsNone(result.after)
        self.cfg["REPLY_CAP"] = 0
        self.assertIs(relay.process_update(self.update(2), self.cfg, self.state, None), True)
        self.assertIs(relay.process_update(self.update(3, None), self.cfg, self.state, None), True)
        self.assertIs(relay.process_update({"update_id": 4}, self.cfg, self.state, None), True)
        self.cfg["REPLY_CAP"] = 100
        result = relay.process_update(self.update(5), self.cfg, self.state,
                                      unittest.mock.Mock(side_effect=OSError()))
        self.assertIsInstance(result, relay.Retry)
        self.assertIsNone(result.after)

    def retry_main(self, outcomes, batches, retries=5):
        self.cfg.update(TELEGRAM_BOT_TOKEN="fake", TELEGRAM_WEBHOOK_URL="https://example.invalid",
                        TELEGRAM_WEBHOOK_KEY="fake", FORWARD_RETRIES=retries)
        (self.root / "offset").write_text("1")
        polls, bodies, events = [], [], []
        class StopLoop(BaseException):
            pass
        replies = iter(batches)
        results = iter(outcomes)
        def urlopen(req, timeout):
            params = json.loads(req.data)
            polls.append(params)
            events.append("poll")
            try:
                result = next(replies)
            except StopIteration:
                raise StopLoop()
            if isinstance(result, Exception):
                raise result
            return io.BytesIO(json.dumps({"result": result}).encode())
        def post(body):
            bodies.append(body)
            result = next(results)
            if isinstance(result, Exception):
                raise result
            return result
        with patch.object(relay, "config", return_value=self.cfg), \
                patch.object(relay.urllib.request.OpenerDirector, "open", side_effect=urlopen), \
                patch.object(relay, "make_post", return_value=post), \
                patch.object(relay.time, "sleep") as sleep, \
                patch.object(relay, "heartbeat", side_effect=lambda path: events.append("beat")):
            with self.assertRaises(StopLoop):
                relay.main()
        return polls, bodies, sleep.call_args_list, events

    def test_retry_success_and_idempotence(self):
        u = self.update(1)
        polls, bodies, sleeps, events = self.retry_main([OSError(), 200], [[u], [u]])
        self.assertEqual([p["offset"] for p in polls], [1, 1, 2])
        self.assertTrue(all(p["timeout"] == relay.LONG_POLL for p in polls))
        self.assertEqual((self.root / "offset").read_text(), "2")
        self.assertEqual([b["history"] for b in bodies], [[], []])
        self.assertEqual([e["message_id"] for e in json.loads(
            (self.root / "history.json").read_text())["-1"]], [1])
        self.assertEqual(json.loads((self.root / "replycount.json").read_text()), {"-1": 1})
        self.assertEqual([c.args for c in sleeps], [(5,)])
        self.assertIn("forward retry 1 attempt 1 in 5s", self.output.getvalue())
        self.assertEqual(self.output.getvalue().count("forwarded 1 200"), 1)
        self.assertEqual(events[0], "beat")

    def test_retry_exhaustion(self):
        u = self.update(1)
        polls, bodies, sleeps, events = self.retry_main(
            [OSError(), OSError(), OSError(), 200], [[u], [u], [u], [self.update(2)]], 3)
        self.assertEqual([p["offset"] for p in polls], [1, 1, 1, 2, 3])
        self.assertEqual([c.args for c in sleeps], [(5,), (10,)])
        self.assertIn("forward dropped 1 after 3 attempts", self.output.getvalue())
        self.assertIn("forwarded 2 200", self.output.getvalue())
        self.assertEqual((self.root / "offset").read_text(), "3")

    def test_skip_one(self):
        for status in (401, 302):
            for raised in (False, True):
                with self.subTest(status=status, raised=raised):
                    result = status
                    if raised:
                        result = urllib.error.HTTPError("http://fake", status, "private", {}, io.BytesIO())
                    polls, bodies, sleeps, events = self.retry_main([result], [[self.update(1)]])
                    self.assertEqual([p["offset"] for p in polls], [1, 2])
                    self.assertEqual(sleeps, [])
                    self.assertIn("forward failed 1 " + str(status) + " not retried", self.output.getvalue())

    def test_http_transient(self):
        for status in (429, 500):
            error = urllib.error.HTTPError("http://fake", status, "private", {}, io.BytesIO())
            result = relay.process_update(self.update(1), self.cfg, self.state,
                                          unittest.mock.Mock(side_effect=error))
            self.assertIsInstance(result, relay.Retry)
            self.assertIsNone(result.after)

    def test_heartbeat_file(self):
        path = self.root / "heartbeat"
        with patch.object(relay, "_heartbeat_counter", 0):
            for expected in (1, 2, 3):
                relay.heartbeat(path)
                self.assertTrue(path.is_file(), "heartbeat file missing")
                self.assertEqual(int(path.read_text()), expected)
                self.assertLess(int(path.read_text()), 1_000_000)

    def test_pause_slices_and_heartbeats(self):
        beat = self.root / "heartbeat"
        observed = []
        with patch.object(relay, "_heartbeat_counter", 0), \
                patch.object(relay.time, "sleep", side_effect=lambda seconds:
                             observed.append((seconds, int(beat.read_text())))):
            relay.pause(75, beat)
        self.assertEqual(observed, [(30, 1), (30, 2), (15, 3)])

    def test_retry_after_body(self):
        self.assertEqual(relay.retry_after_from_body(
            b'{"parameters": {"retry_after": 17}}'), 17)
        for raw in (b'{}', b'bad', b'[]', b'null', b'42', b'{"parameters": null}',
                    b'{"parameters": []}', b'\xff'):
            with self.subTest(raw=raw):
                self.assertIsNone(relay.retry_after_from_body(raw))
        for value in ("x", 0, -1, True, 1.5):
            with self.subTest(value=value):
                self.assertIsNone(relay.retry_after_from_body(
                    json.dumps({"parameters": {"retry_after": value}}).encode()))

    def test_retry_after_headers(self):
        for value, expected in (("120", 120), ("0", 0),
                                ("Wed, 21 Oct 2026 07:28:00 GMT", None),
                                (None, None), ("-5", None), ("1.5", None),
                                ("+5", None), ("", None)):
            with self.subTest(value=value):
                headers = {} if value is None else {"Retry-After": value}
                self.assertEqual(relay.retry_after_header(headers), expected)
        self.assertIsNone(relay.retry_after_header(None))

    def test_webhook_retry_after(self):
        for code in (429, 503):
            for value, delay in (("120", 120), ("1000", 300), ("0", 5)):
                with self.subTest(code=code, value=value):
                    body = io.BytesIO(b"SECRETTOKEN")
                    error = urllib.error.HTTPError("http://fake", code, "SECRETTOKEN",
                                                   {"Retry-After": value}, body)
                    polls, bodies, sleeps, events = self.retry_main(
                        [error], [[self.update(1)]])
                    self.assertEqual([p["offset"] for p in polls], [1, 1])
                    self.assertEqual((self.root / "offset").read_text(), "1")
                    self.assertEqual([c.args[0] for c in sleeps],
                                     [30] * (delay // 30) + ([delay % 30] if delay % 30 else []))
                    self.assertNotIn("forward dropped", self.output.getvalue())
                    self.assertEqual(len(bodies), 1)
                    self.assertTrue(body.closed)
                    self.assertIn(f"forward retry 1 attempt 1 in {delay}s", self.output.getvalue())
        self.assertNotIn("SECRETTOKEN", self.output.getvalue())

    def test_retry_result_after(self):
        error = urllib.error.HTTPError("http://fake", 429, "private",
                                      {"Retry-After": "120"}, io.BytesIO())
        result = relay.process_update(self.update(1), self.cfg, self.state,
                                      unittest.mock.Mock(side_effect=error))
        self.assertIsInstance(result, relay.Retry)
        self.assertEqual(result.after, 120)

    def test_poll_wait(self):
        for code in (401, 404, 409, 500, 429):
            error = urllib.error.HTTPError("http://SECRETTOKEN", code, "SECRETTOKEN",
                                          {}, io.BytesIO(b"SECRETTOKEN"))
            self.addCleanup(error.close)
            message, seconds = relay.poll_wait(error)
            if code in (401, 404):
                expected = (f"poll failed {code}: Telegram rejected the bot token "
                            "(check TELEGRAM_BOT_TOKEN)")
                wait = 60
            elif code == 409:
                expected = ("poll failed 409: a webhook is set for this bot or another poller is using it "
                            "(call deleteWebhook, or stop the other copy)")
                wait = 60
            elif code == 429:
                expected, wait = "poll failed 429: rate limited, waiting 5s", 5
            else:
                expected, wait = "poll failed 500", 5
            self.assertEqual((message, seconds), (expected, wait))
            relay.log(message)
        for error in (urllib.error.URLError("SECRETTOKEN"), OSError("SECRETTOKEN")):
            self.assertEqual(relay.poll_wait(error), ("poll error URLError (str)" if isinstance(error, urllib.error.URLError)
                                                    else "poll error OSError", 5))
        self.assertNotIn("SECRETTOKEN", self.output.getvalue())

    def test_poll_http_errors_in_both_loops(self):
        self.cfg.update(TELEGRAM_BOT_TOKEN="fake", TELEGRAM_WEBHOOK_URL="https://example.invalid",
                        TELEGRAM_WEBHOOK_KEY="fake")
        class StopLoop(BaseException):
            pass
        for bootstrap in (True, False):
            for code, after, wait in ((429, 17, 17), (429, 1000, 300),
                                      (401, None, 60), (404, None, 60),
                                      (409, None, 60), (500, None, 5)):
                with self.subTest(bootstrap=bootstrap, code=code, after=after):
                    off = self.root / "offset"
                    if bootstrap:
                        if off.exists():
                            off.rename(self.root / "previous-offset")
                    else:
                        off.write_text("1")
                    body = io.BytesIO(json.dumps({"description": "SECRETTOKEN",
                        "parameters": {"retry_after": after}}).encode())
                    error = urllib.error.HTTPError("http://SECRETTOKEN", code,
                                                   "SECRETTOKEN", {}, body)
                    with patch.object(relay, "config", return_value=self.cfg), \
                            patch.object(relay.urllib.request.OpenerDirector, "open",
                                         side_effect=[error, StopLoop()]), \
                            patch.object(relay, "make_post"), \
                            patch.object(relay.time, "sleep") as sleep:
                        with self.assertRaises(StopLoop):
                            relay.run()
                    self.assertEqual(sum(c.args[0] for c in sleep.call_args_list), wait)
                    self.assertTrue(all(c.args[0] <= 30 for c in sleep.call_args_list))
                    self.assertTrue(body.closed)
                    self.assertIn(relay.poll_wait(error)[0], self.output.getvalue())
                    if code == 429:
                        self.assertIn(f"poll failed 429: rate limited, waiting {wait}s",
                                      self.output.getvalue())
        self.assertNotIn("SECRETTOKEN", self.output.getvalue())

    def test_heartbeat_poll_error(self):
        polls, bodies, sleeps, events = self.retry_main([], [OSError(), []])
        self.assertEqual(events, ["beat", "poll", "beat", "poll", "beat", "poll"])
        self.assertEqual([c.args for c in sleeps], [(5,)])

    @unittest.skipUnless(hasattr(signal, "SIGALRM"), "SIGALRM is unavailable")
    def test_deadline(self):
        previous = signal.getsignal(signal.SIGALRM)
        started = relay.time.monotonic()
        with self.assertRaises(relay.Hang):
            with relay.deadline(0.2):
                relay.time.sleep(2)
        self.assertLess(relay.time.monotonic() - started, 1.5)
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL)[0], 0)
        self.assertIs(signal.getsignal(signal.SIGALRM), previous)

    @unittest.skipUnless(hasattr(signal, "SIGALRM"), "SIGALRM is unavailable")
    def test_deadline_completed_cancels_timer(self):
        previous = signal.getsignal(signal.SIGALRM)
        with relay.deadline(0.1):
            pass
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL)[0], 0)
        relay.time.sleep(0.2)
        self.assertIs(signal.getsignal(signal.SIGALRM), previous)

    @unittest.skipUnless(hasattr(signal, "SIGALRM"), "SIGALRM is unavailable")
    def test_post_hang_transient(self):
        previous = signal.getsignal(signal.SIGALRM)
        with patch.object(relay, "POST_DEADLINE", 0.2), \
                patch.object(relay.urllib.request.OpenerDirector, "open",
                             side_effect=lambda *args, **kwargs: relay.time.sleep(2)):
            post = relay.make_post("https://example.invalid", "fake")
            result = relay.process_update(self.update(1), self.cfg, self.state, post)
            self.assertIsInstance(result, relay.Retry)
            self.assertIsNone(result.after)
        self.assertIn("forward error 1 Hang", self.output.getvalue())
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL)[0], 0)
        self.assertIs(signal.getsignal(signal.SIGALRM), previous)

    def test_bootstrap_heartbeat_sequence(self):
        self.cfg.update(TELEGRAM_BOT_TOKEN="fake", TELEGRAM_WEBHOOK_URL="https://example.invalid",
                        TELEGRAM_WEBHOOK_KEY="fake")
        events = []
        class StopLoop(BaseException):
            pass
        def urlopen(req, timeout):
            events.append("poll")
            count = events.count("poll")
            if count == 1:
                raise OSError()
            if count == 2:
                return io.BytesIO(b'{"result": []}')
            raise StopLoop()
        with patch.object(relay, "config", return_value=self.cfg), \
                patch.object(relay.urllib.request.OpenerDirector, "open", side_effect=urlopen), \
                patch.object(relay, "heartbeat", side_effect=lambda path: events.append("beat")), \
                patch.object(relay.time, "sleep", side_effect=lambda seconds: events.append("sleep")):
            with self.assertRaises(StopLoop):
                relay.main()
        self.assertEqual(events, ["beat", "poll", "beat", "sleep", "poll", "beat", "poll"])

    def test_deadline_thread(self):
        results = []
        def worker():
            try:
                with relay.deadline(0.01):
                    relay.time.sleep(0.05)
                results.append("finished")
            except Exception as exc:
                results.append(type(exc).__name__)
        thread = threading.Thread(target=worker)
        thread.start()
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(results, ["finished"])

    @unittest.skipUnless(hasattr(signal, "SIGALRM"), "SIGALRM is unavailable")
    def test_poll_hang(self):
        self.cfg.update(TELEGRAM_BOT_TOKEN="fake", TELEGRAM_WEBHOOK_URL="https://example.invalid",
                        TELEGRAM_WEBHOOK_KEY="fake")
        (self.root / "offset").write_text("1")
        real_sleep = relay.time.sleep
        calls = []
        class StopLoop(BaseException):
            pass
        def urlopen(req, timeout):
            calls.append(timeout)
            if len(calls) == 1:
                real_sleep(2)
            raise StopLoop()
        with patch.object(relay, "config", return_value=self.cfg), \
                patch.object(relay, "API_DEADLINE", 0.2), \
                patch.object(relay.urllib.request.OpenerDirector, "open", side_effect=urlopen), \
                patch.object(relay.time, "sleep") as sleep:
            with self.assertRaises(StopLoop):
                relay.main()
        self.assertEqual(calls, [relay.API_TIMEOUT, relay.API_TIMEOUT])
        sleep.assert_not_called()
        self.assertIn("poll error Hang", self.output.getvalue())
        self.assertIn("(read phase timeout), retrying now", self.output.getvalue())

    def stalled_run(self, success_at=None, bootstrap=False, rate=None, missing_result=False):
        self.cfg.update(TELEGRAM_BOT_TOKEN="SECRETTOKEN",
                        TELEGRAM_WEBHOOK_URL="https://example.invalid",
                        TELEGRAM_WEBHOOK_KEY="fake", POLL_STALL_MAX=60)
        if not bootstrap:
            (self.root / "offset").write_text("1")
        clock, polls, sleeps = [0], [], []
        class StopLoop(BaseException):
            pass
        def sleep(seconds):
            sleeps.append(seconds)
            clock[0] += seconds
        def opened(req, timeout):
            if len(polls) > 200:
                raise StopLoop()
            polls.append(clock[0])
            if rate is True and len(polls) == 5:
                raise StopLoop()
            if success_at is not None and len(polls) == success_at:
                return io.BytesIO(b'{"ok": true}' if missing_result else b'{"result": []}')
            if rate is not None:
                body = {"parameters": {"retry_after": 120}} if rate else {}
                raise urllib.error.HTTPError("https://SECRETTOKEN", 429, "", {},
                                             io.BytesIO(json.dumps(body).encode()))
            raise OSError()
        with patch.object(relay, "config", return_value=self.cfg), \
                patch.object(relay.urllib.request.OpenerDirector, "open", side_effect=opened), \
                patch.object(relay, "make_post"), \
                patch.object(relay.time, "monotonic", side_effect=lambda: clock[0]), \
                patch.object(relay.time, "sleep", side_effect=sleep):
            if rate is True:
                with self.assertRaises(StopLoop):
                    relay.run()
            else:
                try:
                    with self.assertRaises(SystemExit) as caught:
                        relay.run()
                except StopLoop:
                    self.fail("relay never exited for a poll stall")
                self.assertEqual(caught.exception.code, relay.POLL_STALL_EXIT)
                self.assertEqual(relay.POLL_STALL_EXIT, 4)
        return polls, sleeps

    def test_poll_stall(self):
        polls, sleeps = self.stalled_run()
        self.assertEqual(polls, list(range(0, 61, 5)))
        self.assertEqual(sum(sleeps), 60)
        self.assertIn("no successful poll for 60s, exiting for restart",
                      self.output.getvalue().splitlines()[-2])
        self.assertIn("stats polls=13 ", self.output.getvalue())
        self.assertIn("stall_exits=1", self.output.getvalue().splitlines()[-1])

    def test_poll_stall_reset(self):
        polls, sleeps = self.stalled_run(success_at=10)
        self.assertEqual(polls[9], 45)
        self.assertEqual(polls[-1], 105)
        self.assertEqual(len(polls), 23)
        self.assertEqual(sum(sleeps), 105)
        self.assertTrue(self.output.getvalue().splitlines()[-2].endswith(
            "no successful poll for 60s, exiting for restart"))

    def test_poll_stall_missing_result(self):
        polls, sleeps = self.stalled_run(success_at=10, missing_result=True)
        self.assertEqual(polls[9], 45)
        self.assertEqual(polls[-1], 60)
        self.assertEqual(sum(sleeps), 60)

    def test_poll_stall_bootstrap(self):
        polls, sleeps = self.stalled_run(bootstrap=True)
        self.assertEqual(polls, list(range(0, 61, 5)))
        self.assertFalse((self.root / "offset").exists())
        self.assertTrue(self.output.getvalue().splitlines()[-2].endswith(
            "no successful poll for 60s, exiting for restart"))

    def test_poll_stall_retry_after(self):
        polls, sleeps = self.stalled_run(rate=True)
        self.assertEqual(polls, [0, 120, 240, 360, 480])
        self.assertEqual(sum(sleeps), 480)
        self.assertIn("stall_exits=0", self.output.getvalue().splitlines()[-1])
        self.assertIn("waiting 120s", self.output.getvalue())
        polls, sleeps = self.stalled_run(rate=False)
        self.assertEqual(polls, list(range(0, 61, 5)))
        self.assertEqual(sum(sleeps), 60)
        self.assertIn("waiting 5s", self.output.getvalue())
        self.assertIn("stall_exits=1", self.output.getvalue().splitlines()[-1])

    def test_immediate_poll_retry(self):
        connect = socket.timeout()
        connect.relay_phase = "connect"
        cases = [
            ([socket.timeout(), []], [], "(read phase timeout), retrying now"),
            ([socket.timeout(), socket.timeout(), []], [5], "(read phase timeout)"),
            ([socket.timeout(), socket.timeout(), socket.timeout(), []], [5, 5],
             "(read phase timeout)"),
            ([urllib.error.URLError(connect), []], [], "(connect phase timeout), retrying now"),
            ([urllib.error.HTTPError("https://SECRETTOKEN", 500, "", {}, io.BytesIO()), []],
             [5], "poll failed 500"),
        ]
        for batches, expected, reason in cases:
            with self.subTest(reason=reason, expected=expected):
                self.output.seek(0)
                self.output.truncate()
                polls, bodies, sleeps, events = self.retry_main([], batches)
                self.assertEqual([c.args[0] for c in sleeps], expected)
                self.assertIn(reason, self.output.getvalue())
                self.assertEqual(len(polls), len(batches) + 1)
                if expected:
                    self.assertNotIn("retrying now", self.output.getvalue().split(reason)[-1])
        self.assertFalse(relay.poll_retryable(OSError()))
        self.assertTrue(relay.poll_retryable(ConnectionError()))
        self.assertTrue(relay.poll_retryable(socket.gaierror()))

    def test_connect_phase_marker(self):
        connection = relay.HTTPSConnection("example.invalid")
        with patch.object(socket, "create_connection", side_effect=socket.timeout()):
            with self.assertRaises(socket.timeout) as caught:
                connection._create_connection(("example.invalid", 443), 5)
        self.assertEqual(caught.exception.relay_phase, "connect")
        self.assertEqual(relay.poll_phase(urllib.error.URLError(caught.exception)), "connect")
        self.assertEqual(relay.poll_phase(socket.timeout()), "read")
        error = urllib.error.HTTPError("https://example.invalid", 500, "", {}, io.BytesIO())
        self.addCleanup(error.close)
        self.assertIsNone(relay.poll_phase(error))
        self.assertFalse(relay.poll_retryable(error))

    def test_media_forwarding(self):
        photo = [{"file_id": "small"}, {"file_id": "large"}]
        original = self.update(1, None, sender=1, photo=photo)
        self.state["counts"]["-1"] = self.cfg["REPLY_CAP"]
        self.send(original)
        self.assertEqual(self.bodies[-1]["message"], original["message"])
        self.assertEqual(self.state["counts"]["-1"], 1)
        entry = self.state["history"]["-1"][-1]
        self.assertEqual(entry["file_id"], "large")
        self.assertEqual(entry["text"], "[photo]")
        self.cfg["FORWARD_MEDIA"] = False
        self.send(self.update(2, None, photo=photo))
        self.assertEqual(len(self.bodies), 1)
        self.assertIn("skipped 2", self.output.getvalue())
        self.assertNotIn("file_id", self.state["history"]["-1"][-1])
        self.cfg["FORWARD_MEDIA"] = True
        self.send(self.update(3, None, sticker={"file_id": "sticker"}))
        self.assertEqual(len(self.bodies), 1)
        self.assertIn("skipped 3", self.output.getvalue())
        for mid, kind in enumerate(("document", "voice", "audio", "video"), 4):
            with self.subTest(kind=kind):
                update = self.update(mid, None, **{kind: {"file_id": kind}})
                self.send(update)
                self.assertEqual(self.bodies[-1]["message"], update["message"])
                self.assertEqual(self.state["history"]["-1"][-1]["file_id"], kind)

    def test_media_cap_and_shapes(self):
        self.cfg["REPLY_CAP"] = 1
        for mid in (1, 2):
            self.send(self.update(mid, None, photo=[{"file_id": "large"}]))
        self.assertEqual(len(self.bodies), 1)
        self.assertIn("capped 2 count 1", self.output.getvalue())
        for shape in (dict(photo="x"), dict(photo=[]), dict(photo=[1]),
                      dict(photo=[{}]), dict(document={}), dict(audio={"file_id": ""}),
                      dict(video={"file_id": 1})):
            with self.subTest(shape=shape):
                self.assertIsNone(relay.media_file_id(shape))
                self.assertNotIn("file_id", relay.history_entry(shape, 10, media=True))

    def test_poll_counters(self):
        polls, bodies, sleeps, events = self.retry_main([], [[]] * 100)
        lines = [line.split("stats ", 1)[1] for line in self.output.getvalue().splitlines()
                 if "stats " in line]
        self.assertEqual(lines, [
            "polls=100 timeouts_connect=0 timeouts_read=0 poll_errors=0 forward_retries=0 stall_exits=0",
            "polls=101 timeouts_connect=0 timeouts_read=0 poll_errors=0 forward_retries=0 stall_exits=0"])
        self.assertNotIn(self.cfg["TELEGRAM_BOT_TOKEN"], self.output.getvalue())

    def test_error_counters(self):
        connect = socket.timeout()
        connect.relay_phase = "connect"
        update = self.update(1)
        self.retry_main([OSError(), 200],
                        [urllib.error.URLError(connect), socket.timeout(), OSError(),
                         [update], [update]])
        self.assertTrue(self.output.getvalue().splitlines()[-1].endswith(
            "stats polls=6 timeouts_connect=1 timeouts_read=1 poll_errors=1 "
            "forward_retries=1 stall_exits=0"))

    @unittest.skipUnless(hasattr(signal, "SIGTERM"), "SIGTERM is unavailable")
    def test_sigterm_restored(self):
        previous = signal.getsignal(signal.SIGTERM)
        self.retry_main([], [[]])
        self.assertIs(signal.getsignal(signal.SIGTERM), previous)

    @unittest.skipUnless(hasattr(signal, "SIGTERM"), "SIGTERM is unavailable")
    def test_sigterm_shutdown(self):
        previous = signal.getsignal(signal.SIGTERM)
        self.cfg.update(TELEGRAM_BOT_TOKEN="fake",
                        TELEGRAM_WEBHOOK_URL="https://example.invalid",
                        TELEGRAM_WEBHOOK_KEY="fake")
        (self.root / "offset").write_text("1")
        recorded = []
        class StopLoop(BaseException):
            pass
        def opened(req, timeout):
            handler = signal.getsignal(signal.SIGTERM)
            recorded.append(handler)
            if len(recorded) > 1:
                raise StopLoop()
            if callable(handler) and handler is not previous:
                handler(signal.SIGTERM, None)
            else:
                raise StopLoop()
        with patch.object(relay, "config", return_value=self.cfg), \
                patch.object(relay.urllib.request.OpenerDirector, "open", side_effect=opened) as poll, \
                patch.object(relay, "make_post"), \
                patch.object(relay.time, "sleep"), \
                patch.object(relay, "heartbeat"):
            try:
                with self.assertRaises(SystemExit) as caught:
                    relay.main()
            except StopLoop:
                self.fail("SIGTERM handler was not installed")
        self.assertIsNot(recorded[0], previous)
        self.assertEqual(caught.exception.code, 128 + signal.SIGTERM)
        poll.assert_called_once()
        self.assertTrue(self.output.getvalue().splitlines()[-1].endswith(
            "stats polls=1 timeouts_connect=0 timeouts_read=0 poll_errors=0 "
            "forward_retries=0 stall_exits=0"))
        self.assertIs(signal.getsignal(signal.SIGTERM), previous)

    def test_stall_supervisor_restart(self):
        process = self.supervisor("from pathlib import Path\n"
                                  "p = Path('starts')\n"
                                  "p.write_text(p.read_text() + 'x' if p.exists() else 'x')\n"
                                  "raise SystemExit(4)\n")
        self.assertIn("relay exited (rc=4), restarting in",
                      self.await_log("relay exited (rc=4), restarting in"))
        self.await_file("starts", "xx", timeout=15)
        self.assertIsNone(process.poll())

    def test_import_no_effects(self):
        module = importlib.util.module_from_spec(spec)
        with patch.object(os, "environ", {}), patch("builtins.open", side_effect=AssertionError("file")), \
                patch.object(relay.socket, "setdefaulttimeout", side_effect=AssertionError("socket")), \
                patch.object(relay.urllib.request.OpenerDirector, "open", side_effect=AssertionError("network")):
            spec.loader.exec_module(module)
        self.assertTrue(callable(module.process_update))


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def cfg(self, **values):
        env = dict(TELEGRAM_BOT_TOKEN="fake", TELEGRAM_WEBHOOK_URL="https://example.invalid/hook",
                   TELEGRAM_WEBHOOK_KEY="fake", ALLOWED_CHAT_ID="-1", OWNER_USER_ID="1")
        env["HOME"] = self.temp.name
        env.update(values)
        with patch.dict(os.environ, env, clear=True):
            return relay.config()

    def test_state_dir_resolution(self):
        home = Path(self.temp.name).resolve()
        default = home / ".local/state/telegram-bot-relay"
        previous = os.umask(0o022)
        self.addCleanup(os.umask, previous)
        with patch.dict(os.environ, {"HOME": str(home)}, clear=True):
            self.assertEqual(relay.state_dir(), default)
            self.assertEqual(stat.S_IMODE(default.stat().st_mode), 0o700)
            with patch.dict(os.environ, {"RELAY_STATE_DIR": ""}):
                self.assertEqual(relay.state_dir(), default)
            absolute = home / "absolute"
            with patch.dict(os.environ, {"RELAY_STATE_DIR": "  " + str(absolute) + "  "}):
                self.assertEqual(relay.state_dir(), absolute)
            with patch.dict(os.environ, {"RELAY_STATE_DIR": "relative"}):
                with patch.object(os, "getcwd", return_value=str(home)):
                    self.assertEqual(relay.state_dir(), home / "relative")
            absolute.chmod(0o755)
            with patch.dict(os.environ, {"RELAY_STATE_DIR": str(absolute)}):
                with self.assertRaisesRegex(SystemExit, "RELAY_STATE_DIR"):
                    relay.state_dir()
                self.assertEqual(stat.S_IMODE(absolute.stat().st_mode), 0o755)

    def test_state_dir_cli(self):
        env = {"HOME": self.temp.name, "PATH": os.defpath}
        result = subprocess.run([sys.executable, "-I", str(ROOT / "relay.py"), "--state-dir"],
                                env=env, capture_output=True, text=True, timeout=5)
        expected = Path(self.temp.name).resolve() / ".local/state/telegram-bot-relay"
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, str(expected) + "\n")
        self.assertEqual(result.stderr, "")
        self.assertTrue(expected.is_dir())
        bad = subprocess.run([sys.executable, "-I", str(ROOT / "relay.py"), "--bad"],
                             env=env, capture_output=True, text=True, timeout=5)
        self.assertEqual(bad.returncode, 2)
        self.assertEqual(bad.stdout, "")
        self.assertIn("usage:", bad.stderr)

    def test_state_dir_cli_padded(self):
        state = Path(self.temp.name).resolve() / "padded"
        env = {"HOME": self.temp.name, "PATH": os.defpath,
               "RELAY_STATE_DIR": "  " + str(state) + "  "}
        result = subprocess.run([sys.executable, "-I", str(ROOT / "relay.py"), "--state-dir"],
                                env=env, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, str(state) + "\n")
        self.assertTrue(state.is_dir())

    def test_r5_positive_limits(self):
        for name in ("REPLY_CAP", "HISTORY_MAX_CHARS"):
            with self.subTest(name=name), self.assertRaisesRegex(SystemExit, name):
                self.cfg(**{name: "0"})
        self.assertEqual(self.cfg(HISTORY_SIZE="0")["HISTORY_SIZE"], 0)

    def test_r5_state_permissions(self):
        root = Path(self.temp.name)
        for mode in (0o755, 0o700):
            root.chmod(mode)
            with patch.dict(os.environ, {"RELAY_STATE_DIR": str(root)}):
                if mode == 0o755:
                    with self.assertRaisesRegex(SystemExit, "RELAY_STATE_DIR must be owned by you"):
                        relay.state_dir()
                else:
                    self.assertEqual(relay.state_dir(), root.resolve())
        with patch.dict(os.environ, {"RELAY_STATE_DIR": str(root)}), \
                patch.object(os, "getuid", return_value=root.stat().st_uid + 1):
            with self.assertRaisesRegex(SystemExit, "RELAY_STATE_DIR must be owned by you"):
                relay.state_dir()

    def test_poll_stall_config(self):
        self.assertEqual(self.cfg()["POLL_STALL_MAX"], 180)
        self.assertEqual(self.cfg(POLL_STALL_MAX="60")["POLL_STALL_MAX"], 60)
        for value in ("59", "x"):
            with self.subTest(value=value), self.assertRaisesRegex(SystemExit, "POLL_STALL_MAX"):
                self.cfg(POLL_STALL_MAX=value)

    def test_forward_media_config(self):
        self.assertIs(self.cfg()["FORWARD_MEDIA"], True)
        self.assertIs(self.cfg(FORWARD_MEDIA="0")["FORWARD_MEDIA"], False)
        with self.assertRaisesRegex(SystemExit, "FORWARD_MEDIA must be 0 or 1"):
            self.cfg(FORWARD_MEDIA="2")

    def test_forward_retries(self):
        self.assertEqual(self.cfg()["FORWARD_RETRIES"], 5)
        for value in ("0", "x"):
            with self.subTest(value=value), self.assertRaises(SystemExit) as caught:
                self.cfg(FORWARD_RETRIES=value)
            self.assertIn("FORWARD_RETRIES", str(caught.exception))

    def test_sent_paths(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            self.assertEqual(self.cfg(RELAY_STATE_DIR=temp)["SENT_LOG"], root / "sent.jsonl")
            self.assertEqual(self.cfg(SENT_LOG=" replies.jsonl ", RELAY_STATE_DIR=temp)["SENT_LOG"],
                             root / "replies.jsonl")
            absolute = root / "absolute.jsonl"
            self.assertEqual(self.cfg(SENT_LOG=str(absolute))["SENT_LOG"], absolute)

    def test_sender_ids(self):
        self.assertEqual(self.cfg(ALLOWED_SENDER_IDS="1, 2,3")["ALLOWED_SENDER_IDS"], {1, 2, 3})
        self.assertEqual(self.cfg()["ALLOWED_SENDER_IDS"], set())

    def test_config_errors(self):
        for name, value in (("ALLOWED_SENDER_IDS", "x"), ("FORWARD_EDITS", "yes"),
                            ("ALLOWED_CHAT_ID", "x"), ("REPLY_CAP", "-1"),
                            ("HISTORY_SIZE", "-1"), ("HISTORY_MAX_CHARS", "-1"),
                            ("TELEGRAM_WEBHOOK_KEY", "")):
            with self.subTest(name=name), self.assertRaises(SystemExit) as caught:
                self.cfg(**{name: value})
            self.assertIn(name, str(caught.exception))

    def test_token(self):
        for token in ("fake secret", "fake\rsecret", "fake\x7fsecret", "  "):
            with self.subTest(token=repr(token)):
                with self.assertRaises(SystemExit) as caught:
                    self.cfg(TELEGRAM_BOT_TOKEN=token)
                self.assertIn("TELEGRAM_BOT_TOKEN", str(caught.exception))
                self.assertNotIn(token, str(caught.exception))
        self.assertEqual(self.cfg(TELEGRAM_BOT_TOKEN=" fake\r\n ")["TELEGRAM_BOT_TOKEN"], "fake")

    def test_https(self):
        url = "http://example.invalid/hook"
        with self.assertRaises(SystemExit) as caught:
            self.cfg(TELEGRAM_WEBHOOK_URL=url)
        self.assertIn("TELEGRAM_WEBHOOK_URL", str(caught.exception))
        self.assertNotIn(url, str(caught.exception))
        self.assertEqual(self.cfg()["TELEGRAM_WEBHOOK_URL"], "https://example.invalid/hook")

    def test_strip_and_edits(self):
        cfg = self.cfg(TELEGRAM_WEBHOOK_KEY=" key ", ALLOWED_CHAT_ID=" -1 ", FORWARD_EDITS=" 1 ")
        self.assertEqual(cfg["TELEGRAM_WEBHOOK_KEY"], "key")
        self.assertEqual(cfg["ALLOWED_CHAT_ID"], -1)
        self.assertTrue(cfg["FORWARD_EDITS"])
        self.assertFalse(self.cfg()["FORWARD_EDITS"])

    def test_missing(self):
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(SystemExit) as caught:
            relay.config()
        self.assertIn("TELEGRAM_BOT_TOKEN", str(caught.exception))


class PostTests(unittest.TestCase):
    def setUp(self):
        self.seen = []
        seen = self.seen
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                seen.append((self.path, self.headers.get("Authorization")))
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                self.send_response(302 if self.path == "/hook" else 200)
                if self.path == "/hook":
                    self.send_header("Location", "/stolen")
                self.end_headers()

            do_GET = do_POST

            def log_message(self, *args):
                pass
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.addCleanup(self.close)
        self.url = "http://127.0.0.1:" + str(self.server.server_port)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def test_fresh_connections(self):
        requests = []
        original = self.server.RequestHandlerClass.do_POST
        def record(handler):
            requests.append((handler.client_address[1], handler.headers.get("Connection")))
            original(handler)
        opener = relay.make_opener()
        with patch.object(self.server.RequestHandlerClass, "do_POST", record):
            for _ in range(2):
                request = urllib.request.Request(self.url + "/ok", data=b"{}")
                with opener.open(request, timeout=5) as response:
                    self.assertEqual(response.status, 200)
                    response.read()
        self.assertEqual(len(requests), 2)
        self.assertNotEqual(requests[0][0], requests[1][0])
        self.assertEqual([item[1] for item in requests], ["close", "close"])

    def test_r5_connection_handlers(self):
        for cls in (relay.HTTPConnection, relay.HTTPSConnection):
            with self.subTest(cls=cls.__name__):
                connection = cls("example.invalid", timeout=17)
                sock = unittest.mock.Mock()
                with patch.object(socket, "create_connection", return_value=sock) as create:
                    self.assertIs(connection._create_connection(("example.invalid", 443), 17, None), sock)
                create.assert_called_once_with(("example.invalid", 443), relay.CONNECT_TIMEOUT, None)
                sock.settimeout.assert_called_once_with(17)
        context = ssl.create_default_context()
        handler = relay.HTTPSHandler(context=context)
        request = urllib.request.Request("https://example.invalid")
        with patch.object(handler, "do_open") as opened:
            handler.https_open(request)
        opened.assert_called_once_with(relay.HTTPSConnection, request, context=context)

    def test_r5_connect_timeout(self):
        real_getaddrinfo = socket.getaddrinfo
        real_connect = socket.socket.connect
        attempts = []
        def addresses(host, port, *args, **kwargs):
            if host == "relay.test":
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))
                        for ip in ("10.255.255.1", "127.0.0.1")]
            return real_getaddrinfo(host, port, *args, **kwargs)
        def connect(sock, address):
            attempts.append((address[0], sock.gettimeout()))
            if address[0] == "10.255.255.1":
                # Emulation avoids external traffic and machines where this address fails fast.
                relay.time.sleep(sock.gettimeout())
                raise socket.timeout()
            return real_connect(sock, address)
        original = self.server.RequestHandlerClass.do_POST
        def long_poll(handler):
            relay.time.sleep(1)
            original(handler)
        with patch.object(self.server.RequestHandlerClass, "do_POST", long_poll), \
                patch.object(socket, "getaddrinfo", side_effect=addresses), \
                patch.object(socket.socket, "connect", connect), \
                patch.object(relay, "CONNECT_TIMEOUT", 1, create=True), \
                patch.object(relay, "API_TIMEOUT", 3), patch.object(relay, "API_DEADLINE", 4), \
                patch.object(relay, "POST_TIMEOUT", 3), patch.object(relay, "POST_DEADLINE", 4), \
                patch.dict(os.environ, {"no_proxy": "*", "NO_PROXY": "*"}):
            url = self.url.replace("127.0.0.1", "relay.test") + "/ok"
            self.assertEqual(relay.make_post(url, "fake")({}), 200)
            with relay.deadline(relay.API_DEADLINE):
                request = urllib.request.Request(url, data=b"{}")
                with relay.make_opener().open(request, timeout=relay.API_TIMEOUT) as response:
                    self.assertEqual(response.status, 200)
                    response.read()
        self.assertEqual(attempts, [("10.255.255.1", 1), ("127.0.0.1", 1)] * 2)

    def test_redirect_blocked(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            relay.make_post(self.url + "/hook", "fake-key")({"text": "test"})
        self.assertEqual(caught.exception.code, 302)
        caught.exception.close()
        self.assertEqual(self.seen, [("/hook", "Bearer fake-key")])

    def test_post_control(self):
        self.assertEqual(relay.make_post(self.url + "/ok", "fake-key")({}), 200)
        self.assertEqual(self.seen, [("/ok", "Bearer fake-key")])




if __name__ == "__main__":
    print(sys.version)
    print(sys.executable)
    unittest.main()

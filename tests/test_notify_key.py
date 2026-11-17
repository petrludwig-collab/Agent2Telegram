"""``notify --key`` (idempotent send) and ``notify-status`` (read-only). No network: a scripted
urllib opener stands in for Telegram, so the real client retry/fallback code is exercised."""
import contextlib
import fcntl
import http.client
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
from pathlib import Path
from unittest.mock import Mock, patch

from agent2telegram import notify_ledger
from agent2telegram.__main__ import _main
from agent2telegram.config import Config

REPO_ROOT = Path(__file__).resolve().parent.parent


class _Resp:
    def __init__(self, payload):
        self._raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeNet:
    """Opener double. Each request consumes one scripted step (default "ok") and is recorded."""

    def __init__(self, *script):
        self.script = list(script)
        self.requests = []

    def open(self, req, timeout=None):
        params = {k: v[0] for k, v in urllib.parse.parse_qs(req.data.decode()).items()}
        self.requests.append(params)
        step = self.script.pop(0) if self.script else "ok"
        if callable(step):
            step(params)
            step = "ok"
        if step == "ok":
            return _Resp({"ok": True, "result": {"message_id": 100 + len(self.requests)}})
        if step == "nomsgid":
            return _Resp({"ok": True, "result": True})
        if step == "garbled":
            return _Resp(b"<html>gateway")
        if step == "timeout":
            raise TimeoutError("timed out")
        if step == "reset":
            raise ConnectionResetError("reset by peer")
        if isinstance(step, int):                 # a well-formed Telegram error answer
            raise self._http(step, {"ok": False, "error_code": step, "description": f"Error {step}: refused"})
        if step[0] == "tg":                       # ("tg", code, description[, parameters])
            body = {"ok": False, "error_code": step[1], "description": step[2]}
            if len(step) > 3:
                body["parameters"] = step[3]
            raise self._http(step[1], body)
        if step[0] == "raw":                      # ("raw", code, bytes): an HTTP error with any body
            raise self._http(step[1], step[2])
        if step[0] == "ok200":                    # ("ok200", bytes): HTTP 200 with any body
            return _Resp(step[1])
        raise AssertionError(step)

    @staticmethod
    def _http(code, body):
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        return urllib.error.HTTPError("https://x", code, "error", {}, io.BytesIO(raw))


PARSE = ("tg", 400, "Bad Request: can't parse entities: Unsupported start tag")


def _raise_incomplete_read(params):
    raise http.client.IncompleteRead(b"")       # not an OSError: the client does not classify it


def snapshot(root: Path) -> dict:
    out = {}
    for dirpath, dirs, files in os.walk(root):
        for name in dirs + files:
            p = Path(dirpath) / name
            st = os.lstat(p)
            out[str(p.relative_to(root))] = (st.st_mode, st.st_size, st.st_mtime_ns, st.st_ino)
    return out


class KeyedBase(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        self.state = tmp / "state"
        self.ledger = self.state / "notify-keys"
        self.cfg = Config(agent="generic", token="1:2", allowed_user_ids=[7], allowed_group_ids=[-500])
        self.clock = [1_000_000.0]
        patcher = patch("agent2telegram.notify_ledger._now", side_effect=lambda: self.clock[0])
        patcher.start()
        self.addCleanup(patcher.stop)

    def cli(self, net, *args):
        out, err = io.StringIO(), io.StringIO()
        with patch("agent2telegram.config.load", return_value=self.cfg), \
                patch("urllib.request.build_opener", return_value=net), \
                patch.dict(os.environ, {"AGENT2TELEGRAM_STATE": str(self.state)}), \
                patch("time.sleep") as sleep, \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = _main(list(args))
        self.sleep = sleep
        self.out, self.err = out.getvalue(), err.getvalue()
        return rc

    def status(self, key, *extra):
        rc = self.cli(None, "notify-status", key, *extra)
        self.assertEqual(rc, 0)
        return self.out

    def record(self, key):
        return json.loads(notify_ledger._base(self.ledger, "1", key).with_suffix(".json").read_text())


class OutcomeTests(KeyedBase):
    def test_repeat_after_delivery_sends_nothing(self):
        net = FakeNet()
        self.assertEqual(self.cli(net, "notify", "--key", "k1", "hello"), 0)
        self.assertEqual(len(net.requests), 1)
        again = FakeNet()
        self.assertEqual(self.cli(again, "notify", "--key", "k1", "hello"), 0)
        self.assertEqual(again.requests, [])
        self.assertIn("already delivered", self.out)
        self.assertIn("101", self.out)                      # original message_id reported
        self.assertEqual(self.record("k1")["state"], "delivered")

    def test_intent_is_on_disk_before_the_request(self):
        seen = []
        net = FakeNet(lambda params: seen.append(self.record("k1")["state"]))
        self.assertEqual(self.cli(net, "notify", "--key", "k1", "hello"), 0)
        self.assertEqual(seen, ["sending"])

    def test_collision_on_different_text_or_target(self):
        self.assertEqual(self.cli(FakeNet(), "notify", "--key", "k1", "hello"), 0)
        for args in (("other text",), ("hello", "--to-group", "-500")):
            with self.subTest(args=args):
                net = FakeNet()
                self.assertEqual(self.cli(net, "notify", "--key", "k1", *args), 3)
                self.assertEqual(net.requests, [])
                self.assertIn("collision", self.err)
                self.assertEqual(self.record("k1")["state"], "delivered")

    def test_lost_confirmation_is_uncertain_and_never_resent(self):
        net = FakeNet("timeout")
        self.assertEqual(self.cli(net, "notify", "--key", "k1", "hello"), 4)
        self.assertEqual(len(net.requests), 1)              # no internal retry
        self.sleep.assert_not_called()                      # no backoff either
        for _ in range(2):
            rerun = FakeNet()
            self.assertEqual(self.cli(rerun, "notify", "--key", "k1", "hello"), 4)
            self.assertEqual(rerun.requests, [])
        self.assertEqual(self.record("k1")["state"], "uncertain")

    def test_no_retry_or_fallback_after_an_unclear_result(self):
        for i, step in enumerate(("timeout", "reset", 502, "garbled", "nomsgid", _raise_incomplete_read)):
            with self.subTest(step=i):
                net = FakeNet(step)
                self.assertEqual(self.cli(net, "notify", "--key", f"k{i}", "hello"), 4)
                self.assertEqual(len(net.requests), 1)      # no retry, no plain-text fallback
                self.sleep.assert_not_called()

    def test_definite_rejection_allows_plain_fallback_once(self):
        net = FakeNet(PARSE, "ok")
        self.assertEqual(self.cli(net, "notify", "--key", "k1", "**hi**"), 0)
        self.assertEqual(len(net.requests), 2)
        self.assertEqual(net.requests[0].get("parse_mode"), "HTML")
        self.assertNotIn("parse_mode", net.requests[1])

    def test_certain_failure_is_failed_and_a_rerun_may_send(self):
        net = FakeNet(PARSE, 400)
        self.assertEqual(self.cli(net, "notify", "--key", "k1", "hello"), 1)
        self.assertEqual(len(net.requests), 2)
        self.assertEqual(self.record("k1")["state"], "failed")
        again = FakeNet()
        self.assertEqual(self.cli(again, "notify", "--key", "k1", "hello"), 0)
        self.assertEqual(len(again.requests), 1)
        self.assertEqual(self.record("k1")["state"], "delivered")

    def test_other_rejections_fail_without_a_second_request(self):
        for i, step in enumerate((400, 401, 403, 404, ("tg", 409, "Conflict"))):
            with self.subTest(step=step):
                net = FakeNet(step)
                self.assertEqual(self.cli(net, "notify", "--key", f"k{i}", "hello"), 1)
                self.assertEqual(len(net.requests), 1)            # no plain-text fallback
                self.assertEqual(self.record(f"k{i}")["state"], "failed")

    def test_flood_control_is_failed_with_retry_after_and_never_waited_out(self):
        net = FakeNet(("tg", 429, "Too Many Requests: retry after 7", {"retry_after": 7}))
        self.assertEqual(self.cli(net, "notify", "--key", "k1", "hello"), 1)
        self.assertEqual(len(net.requests), 1)
        self.sleep.assert_not_called()
        self.assertIn("retry after 7s", self.record("k1")["detail"])
        again = FakeNet()
        self.assertEqual(self.cli(again, "notify", "--key", "k1", "hello"), 0)   # retry allowed later

    def test_only_a_well_formed_error_answer_is_a_definite_rejection(self):
        odd = (("raw", 400, b"<html>bad gateway"), ("raw", 400, b'{"ok": false}'),
               ("raw", 403, b'{"ok": null, "error_code": 403}'),
               ("raw", 400, b'{"ok": false, "error_code": "400"}'),
               ("raw", 400, b'{"ok": false, "error_code": true}'),
               ("raw", 400, b'{"ok": false, "error_code": 500}'),      # does not match the HTTP status
               ("raw", 429, b"slow down"),
               ("ok200", b'{"result": {"message_id": 5}}'), ("ok200", b'{"ok": null}'),
               ("ok200", b'{"ok": false}'), ("ok200", b'{"ok": false, "error_code": 502}'),
               ("ok200", b'{"ok": false, "error_code": "400"}'), ("ok200", b'{"ok": false, "error_code": 400.0}'),
               ("raw", 400, b'{"ok": false, "error_code": 400.0}'),
               # a "success" that is not literally ok=true with an int message_id is not a confirmation
               ("ok200", b'{"ok": "false", "result": {"message_id": 123}}'),
               ("ok200", b'{"ok": 1, "result": {"message_id": 123}}'),
               ("ok200", b'{"ok": true, "result": {"message_id": true}}'),
               ("ok200", b'{"ok": true, "result": {"message_id": "5"}}'),
               ("ok200", b'{"ok": true, "result": {"message_id": 5.0}}'))
        for i, step in enumerate(odd):
            with self.subTest(step=step):
                net = FakeNet(step)
                self.assertEqual(self.cli(net, "notify", "--key", f"k{i}", "hello"), 4)
                self.assertEqual(len(net.requests), 1)            # no fallback
                self.sleep.assert_not_called()
                self.assertEqual(self.record(f"k{i}")["state"], "uncertain")

    def test_well_formed_error_inside_an_http_200_answer_counts(self):
        body = b'{"ok": false, "error_code": 400, "description": "Bad Request: can\'t parse entities"}'
        net = FakeNet(("ok200", body), "ok")
        self.assertEqual(self.cli(net, "notify", "--key", "k1", "**hi**"), 0)
        self.assertEqual(len(net.requests), 2)

    def test_multipart_all_delivered(self):
        net = FakeNet()
        self.assertEqual(self.cli(net, "notify", "--key", "k1", "x" * 5000), 0)
        self.assertEqual(len(net.requests), 2)
        self.assertEqual(self.record("k1")["message_ids"], [101, 102])

    def test_partial_multipart_is_uncertain_and_never_resent(self):
        for steps in (("ok", "timeout"), ("ok", PARSE, 400)):
            with self.subTest(steps=steps):
                key = "k-" + "-".join(str(x)[:6] for x in steps)
                net = FakeNet(*steps)
                self.assertEqual(self.cli(net, "notify", "--key", key, "x" * 5000), 4)
                rec = self.record(key)
                self.assertEqual((rec["state"], rec["message_ids"], rec["parts_total"]), ("uncertain", [101], 2))
                rerun = FakeNet()
                self.assertEqual(self.cli(rerun, "notify", "--key", key, "x" * 5000), 4)
                self.assertEqual(rerun.requests, [])

    def test_unreadable_record_blocks_sending(self):
        self.assertEqual(self.cli(FakeNet(), "notify", "--key", "k1", "hello"), 0)
        notify_ledger._base(self.ledger, "1", "k1").with_suffix(".json").write_text("{broken")
        net = FakeNet()
        self.assertEqual(self.cli(net, "notify", "--key", "k1", "hello"), 4)
        self.assertEqual(net.requests, [])

    def test_unrecorded_result_is_reported_as_uncertain(self):
        real_write = notify_ledger._write
        calls = []

        def write(path, rec):
            calls.append(rec["state"])
            if len(calls) == 2:                               # the write that records the outcome
                raise OSError("disk full")
            real_write(path, rec)

        net = FakeNet()
        with patch("agent2telegram.notify_ledger._write", side_effect=write):
            self.assertEqual(self.cli(net, "notify", "--key", "k1", "hello"), 4)
        self.assertEqual(len(net.requests), 1)
        again = FakeNet()
        self.assertEqual(self.cli(again, "notify", "--key", "k1", "hello"), 4)   # record still says sending
        self.assertEqual(again.requests, [])

    def test_unusable_ledger_stops_before_any_send(self):
        blocker = self.state.parent / "not-a-dir"
        blocker.write_text("x")
        self.state = blocker / "state"
        net = FakeNet()
        self.assertEqual(self.cli(net, "notify", "--key", "k1", "hello"), 2)
        self.assertEqual(net.requests, [])
        self.assertIn("nothing was sent", self.err)

    def test_group_target_is_used(self):
        net = FakeNet()
        self.assertEqual(self.cli(net, "notify", "--key", "k1", "hello", "--to-group"), 0)
        self.assertEqual(net.requests[0]["chat_id"], "-500")


class UsageTests(KeyedBase):
    def test_key_with_file_is_rejected_before_any_send(self):
        net = FakeNet()
        self.assertEqual(self.cli(net, "notify", "--key", "k1", "hello", "--file", "/nonexistent"), 2)
        self.assertEqual(net.requests, [])
        self.assertFalse(self.state.exists())
        self.assertIn("text only", self.err)

    def test_invalid_key_and_ttl_arguments(self):
        for args in (("--key", ""), ("--key", "k" * 201), ("--key", "a\nb"),
                     ("--key-ttl", "60"), ("--key", "k", "--key-ttl", "0"),
                     ("--key", "k", "--key-ttl", "nan"), ("--key", "k", "--key-ttl", "inf"),
                     ("--key", "k", "--key-ttl", "315360001"), ("--key", "k", "--key-ttl", "1e300")):
            with self.subTest(args=args):
                net = FakeNet()
                self.assertEqual(self.cli(net, "notify", "hello", *args), 2)
                self.assertEqual(net.requests, [])
                self.assertFalse(self.state.exists())
                if "--key-ttl" in args and len(args) == 4:
                    self.assertIn("--key-ttl", self.err)        # refused by the argument check itself

    def test_empty_text_is_rejected_before_anything_is_recorded(self):
        for text in ("", "   \n"):
            with self.subTest(text=text):
                net = FakeNet()
                with patch("sys.stdin", io.StringIO("")):
                    self.assertEqual(self.cli(net, "notify", "--key", "k1", text), 2)
                self.assertEqual(net.requests, [])
                self.assertFalse(self.state.exists())
        for bad in (None, "", "  "):
            with self.assertRaises(ValueError):
                notify_ledger.notify_keyed(Mock(), self.ledger, "1", "k1", 7, bad)
        self.assertFalse(self.state.exists())

    def test_library_refuses_a_nonfinite_ttl_before_recording(self):
        for ttl in (float("nan"), float("inf"), 0, -1, None, True, notify_ledger.MAX_TTL + 1, 1e300):
            with self.subTest(ttl=ttl), self.assertRaises(ValueError):
                notify_ledger.notify_keyed(Mock(), self.ledger, "1", "k1", 7, "hello", ttl=ttl)
        self.assertFalse(self.state.exists())

    def test_invalid_arguments_from_the_library_get_a_usage_message(self):
        with patch("agent2telegram.notify_ledger.notify_keyed", side_effect=ValueError("nothing to send")):
            self.assertEqual(self.cli(FakeNet(), "notify", "--key", "k1", "hello"), 2)
        self.assertIn("invalid notify --key arguments", self.err)
        self.assertNotIn("ledger unavailable", self.err)

    def test_without_key_behaviour_is_unchanged(self):
        client = Mock()
        with patch("agent2telegram.config.load", return_value=self.cfg), \
                patch("agent2telegram.telegram.TelegramClient", return_value=client), \
                patch.dict(os.environ, {"AGENT2TELEGRAM_STATE": str(self.state)}), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(_main(["notify", "Notice"]), 0)
        client.send_message.assert_called_once_with(7, "Notice")
        client.send_part_once.assert_not_called()
        self.assertFalse(self.state.exists())              # no ledger touched


class StatusTests(KeyedBase):
    def test_unknown_key_and_nothing_created(self):
        self.assertIn("state: unknown", self.status("nope"))
        self.assertEqual(json.loads(self.status("nope", "--json")), {"key": "nope", "state": "unknown"})
        self.assertFalse(self.state.exists())

    def test_reports_delivered_without_content_or_fingerprint(self):
        self.cli(FakeNet(), "notify", "--key", "k1", "secret words")
        data = json.loads(self.status("k1", "--json"))
        self.assertEqual((data["state"], data["message_ids"], data["expired"]), ("delivered", [101], False))
        self.assertNotIn("secret words", json.dumps(data))
        self.assertNotIn("fingerprint", data)
        self.assertIn("state: delivered", self.status("k1"))

    def test_status_is_strictly_read_only(self):
        self.cli(FakeNet(), "notify", "--key", "k-done", "hello")
        self.cli(FakeNet("timeout"), "notify", "--key", "k-unsure", "hello")
        self.cli(FakeNet(), "notify", "--key", "k-old", "hello", "--key-ttl", "10")
        self.clock[0] += 100                                  # k-old is now expired
        notify_ledger._base(self.ledger, "1", "k-done").with_suffix(".lock").unlink()   # status must not recreate it
        before = snapshot(self.state)
        for key in ("k-done", "k-unsure", "k-old", "k-unknown"):
            self.status(key)
            self.status(key, "--json")
        self.assertEqual(snapshot(self.state), before)
        self.assertTrue(json.loads(self.status("k-old", "--json"))["expired"])   # expired but NOT pruned

    def test_status_does_not_wait_for_a_sender(self):
        self.cli(FakeNet(), "notify", "--key", "k1", "hello")
        fd = os.open(notify_ledger._base(self.ledger, "1", "k1").with_suffix(".lock"), os.O_RDWR)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        started = time.monotonic()
        self.assertIn("state: delivered", self.status("k1"))
        self.assertLess(time.monotonic() - started, 2)


class TtlTests(KeyedBase):
    def test_window_expiry_allows_a_new_send_only_through_the_write_path(self):
        self.cli(FakeNet(), "notify", "--key", "k1", "hello", "--key-ttl", "100")
        self.clock[0] += 50
        net = FakeNet()
        self.assertEqual(self.cli(net, "notify", "--key", "k1", "hello"), 0)
        self.assertEqual(net.requests, [])                    # still inside the window
        self.clock[0] += 100
        before = snapshot(self.state)
        self.assertTrue(json.loads(self.status("k1", "--json"))["expired"])
        self.assertEqual(snapshot(self.state), before)        # status did not prune
        net = FakeNet()
        self.assertEqual(self.cli(net, "notify", "--key", "k1", "hello"), 0)
        self.assertEqual(len(net.requests), 1)                # write path: expired key is free again
        self.assertEqual(self.record("k1")["created_at"], int(self.clock[0]))

    def test_write_path_prunes_expired_keys_but_not_live_or_busy_ones(self):
        for key, ttl in (("old", "100"), ("busy", "100"), ("live", "100000")):
            self.cli(FakeNet(), "notify", "--key", key, "hello", "--key-ttl", ttl)
        busy_lock = os.open(notify_ledger._base(self.ledger, "1", "busy").with_suffix(".lock"), os.O_RDWR)
        self.addCleanup(os.close, busy_lock)
        fcntl.flock(busy_lock, fcntl.LOCK_EX)
        self.clock[0] += 1000
        self.cli(FakeNet(), "notify", "--key", "new", "hello")
        names = {p.stem for p in self.ledger.glob("*.json")}
        want = {notify_ledger._base(self.ledger, "1", k).name for k in ("busy", "live", "new")}
        self.assertEqual(names, want)
        self.assertFalse(notify_ledger._base(self.ledger, "1", "old").with_suffix(".lock").exists())


class BotIdentityTests(KeyedBase):
    TOKEN_A, TOKEN_B = "1:AAA-secret-part-one", "2:BBB-secret-part-two"

    def as_bot(self, token):
        self.cfg.token = token

    def test_two_bots_sharing_a_state_dir_do_not_see_each_others_keys(self):
        self.as_bot(self.TOKEN_A)
        net_a = FakeNet()
        self.assertEqual(self.cli(net_a, "notify", "--key", "k1", "hello"), 0)
        self.as_bot(self.TOKEN_B)
        net_b = FakeNet("timeout")                          # bot B's own send is lost
        self.assertEqual(self.cli(net_b, "notify", "--key", "k1", "hello"), 4)
        self.assertEqual(len(net_b.requests), 1)            # B really sent: not "already delivered"
        self.assertEqual(json.loads(self.status("k1", "--json"))["state"], "uncertain")
        self.as_bot(self.TOKEN_A)
        again = FakeNet()
        self.assertEqual(self.cli(again, "notify", "--key", "k1", "hello"), 0)
        self.assertEqual(again.requests, [])
        self.assertIn("already delivered", self.out)
        self.assertEqual(json.loads(self.status("k1", "--json"))["state"], "delivered")
        self.as_bot("3:CCC-another")
        self.assertEqual(json.loads(self.status("k1", "--json"))["state"], "unknown")   # a third bot: nothing
        self.assertEqual(len(list(self.ledger.glob("*.json"))), 2)

    def test_the_token_secret_never_reaches_the_ledger(self):
        for token in (self.TOKEN_A, self.TOKEN_B):
            self.as_bot(token)
            self.cli(FakeNet(), "notify", "--key", "k1", "hello")
            self.cli(FakeNet("timeout"), "notify", "--key", "k2", "hello")
            self.status("k1")
        blob = b""
        for p in self.state.rglob("*"):
            blob += str(p.relative_to(self.state)).encode()
            if p.is_file():
                blob += p.read_bytes()
        for secret in (b"AAA-secret", b"BBB-secret", b"part-one", b"part-two", b"1:AAA", b"2:BBB"):
            self.assertNotIn(secret, blob)
        self.assertEqual(json.loads(next(self.ledger.glob("*.json")).read_text())["bot"] in ("1", "2"), True)

    def test_same_bot_repeating_the_same_key_sends_nothing_more(self):
        self.as_bot(self.TOKEN_B)
        self.assertEqual(self.cli(FakeNet(), "notify", "--key", "k1", "hello"), 0)
        for _ in range(2):
            net = FakeNet()
            self.assertEqual(self.cli(net, "notify", "--key", "k1", "hello"), 0)
            self.assertEqual(net.requests, [])
            self.assertIn("already delivered", self.out)

    def test_a_record_planted_under_another_bots_or_keys_path_is_unusable(self):
        self.as_bot(self.TOKEN_A)
        self.assertEqual(self.cli(FakeNet(), "notify", "--key", "k1", "hello"), 0)
        src = notify_ledger._base(self.ledger, "1", "k1").with_suffix(".json")
        planted = [("2", "k1"), ("1", "k2")]           # another bot's path, another key's path
        for bot, key in planted:
            dst = notify_ledger._base(self.ledger, bot, key).with_suffix(".json")
            dst.write_text(src.read_text())             # a perfectly valid record, but not theirs
        self.as_bot(self.TOKEN_B)
        net = FakeNet()
        self.assertEqual(self.cli(net, "notify", "--key", "k1", "hello"), 4)
        self.assertEqual(net.requests, [])
        data = json.loads(self.status("k1", "--json"))
        self.assertEqual(data["state"], "uncertain")
        self.assertIn("unusable", data["detail"])
        self.as_bot(self.TOKEN_A)
        net = FakeNet()
        self.assertEqual(self.cli(net, "notify", "--key", "k2", "hello"), 4)
        self.assertEqual(net.requests, [])
        self.assertEqual(json.loads(self.status("k2", "--json"))["state"], "uncertain")
        self.assertEqual(json.loads(self.status("k1", "--json"))["state"], "delivered")   # the genuine one

    def test_library_status_refuses_a_bad_bot_id(self):
        for bot in ("", "abc", "१२", None, 5):
            with self.subTest(bot=bot), self.assertRaises(ValueError):
                notify_ledger.read_status(self.ledger, bot, "k1")
        self.assertFalse(self.state.exists())

    def test_token_without_a_numeric_bot_id_is_a_usage_error(self):
        for token in ("abc:xyz", ":xyz", "1x:y"):
            with self.subTest(token=token):
                self.as_bot(token)
                net = FakeNet()
                self.assertEqual(self.cli(net, "notify", "--key", "k1", "hello"), 2)
                self.assertEqual(net.requests, [])
                self.assertIn("bot id", self.err)
                self.assertFalse(self.state.exists())
                self.assertEqual(self.cli(None, "notify-status", "k1"), 2)
                self.assertIn("bot id", self.err)
                self.assertNotIn("xyz", self.out + self.err)


class LedgerIntegrityTests(KeyedBase):

    def test_status_survives_absurd_but_finite_timestamps(self):
        self.cli(FakeNet(), "notify", "--key", "k1", "hello")
        self.corrupt("k1", {"created_at": 1e300, "expires_at": 1e300, "updated_at": 1e300})
        self.assertEqual(self.cli(None, "notify-status", "k1"), 0)
        self.assertIn("state: delivered", self.out)
        self.assertEqual(self.cli(None, "notify-status", "k1", "--json"), 0)

    def corrupt(self, key, change):
        path = notify_ledger._base(self.ledger, "1", key).with_suffix(".json")
        text = path.read_text()
        data = json.loads(text)
        data = change(data) if callable(change) else {**data, **change}
        path.write_text(json.dumps(data))

    def test_invalid_records_block_sending_and_never_crash(self):
        def drop(field):
            return lambda d: {k: v for k, v in d.items() if k != field}
        changes = [{"bot": ""}, {"bot": "abc"}, {"bot": "१२"}, {"bot": 5}, drop("bot"),
                   {"state": "failed", "message_ids": [5]},            # failed but a part was confirmed
                   {"message_ids": []}, {"parts_total": 2},            # delivered without full confirmation
                   {"parts_total": 0, "message_ids": []}, {"parts_total": -1},
                   {"expires_at": None}, {"expires_at": "soon"}, drop("expires_at"), {"expires_at": True},
                   {"state": "weird"}, {"state": None}, {"fingerprint": 5}, drop("fingerprint"),
                   {"message_ids": "x"}, {"message_ids": [1, "a"]}, {"parts_total": None},
                   {"created_at": float("nan")}, {"expires_at": float("inf")}, {"v": 2}, {"detail": 3},
                   lambda d: [d]]
        for i, change in enumerate(changes):
            with self.subTest(i=i):
                key = f"k{i}"
                self.cli(FakeNet(), "notify", "--key", key, "hello")
                self.corrupt(key, change)
                net = FakeNet()
                self.assertEqual(self.cli(net, "notify", "--key", key, "hello"), 4)
                self.assertEqual(net.requests, [])
                data = json.loads(self.status(key, "--json"))
                self.assertEqual(data["state"], "uncertain")
                self.assertIn("unusable", data["detail"])
                self.assertIn("unusable", self.status(key))
        # a pass over all those broken records (prune) must not break a send under another key
        self.clock[0] += 10 ** 7
        net = FakeNet()
        self.assertEqual(self.cli(net, "notify", "--key", "fresh", "hello"), 0)
        self.assertEqual(len(net.requests), 1)
        self.assertEqual(len(list(self.ledger.glob("*.json"))), len(changes) + 1)   # none of them pruned

    def test_prune_does_not_delete_a_record_renewed_after_the_expiry_check(self):
        self.cli(FakeNet(), "notify", "--key", "x", "hello", "--key-ttl", "100")
        self.clock[0] += 1000                                  # x is expired, as far as the first check can see
        x_json = notify_ledger._base(self.ledger, "1", "x").with_suffix(".json")
        x_lock = x_json.with_suffix(".lock")
        real = notify_ledger._try_lock

        def racing(lock_path):
            if lock_path == x_lock:                            # another process renews x right now
                fresh = json.loads(x_json.read_text())
                fresh.update(created_at=self.clock[0], expires_at=self.clock[0] + 500)
                x_json.write_text(json.dumps(fresh))
            return real(lock_path)

        with patch("agent2telegram.notify_ledger._try_lock", side_effect=racing):
            self.assertEqual(self.cli(FakeNet(), "notify", "--key", "other", "hello"), 0)
        self.assertTrue(x_json.exists())
        again = FakeNet()
        self.assertEqual(self.cli(again, "notify", "--key", "x", "hello"), 0)
        self.assertEqual(again.requests, [])                   # still deduplicated

    def test_ledger_directory_creation_is_made_durable(self):
        synced = []
        real = notify_ledger._fsync_dir
        with patch("agent2telegram.notify_ledger._fsync_dir", side_effect=lambda p: (synced.append(Path(p)), real(p))):
            self.assertEqual(self.cli(FakeNet(), "notify", "--key", "k1", "hello"), 0)
        self.assertTrue({self.state.parent, self.state, self.ledger} <= set(synced), synced)

    def test_parents_are_synced_even_when_the_directory_already_exists(self):
        self.assertEqual(self.cli(FakeNet(), "notify", "--key", "k1", "hello"), 0)   # creates the ledger
        synced = []
        real = notify_ledger._fsync_dir
        with patch("agent2telegram.notify_ledger._fsync_dir", side_effect=lambda p: (synced.append(Path(p)), real(p))):
            self.assertEqual(self.cli(FakeNet(), "notify", "--key", "k2", "hello"), 0)
        self.assertTrue({self.state.parent, self.state, self.ledger} <= set(synced), synced)

    def test_failed_parent_sync_stops_before_any_send(self):
        net = FakeNet()
        with patch("agent2telegram.notify_ledger._fsync_dir", side_effect=OSError("io error")):
            self.assertEqual(self.cli(net, "notify", "--key", "k1", "hello"), 2)
        self.assertEqual(net.requests, [])
        self.assertIn("nothing was sent", self.err)

    def test_failed_sync_of_a_higher_ancestor_stops_before_any_send(self):
        real = notify_ledger._fsync_dir
        for failing in (self.state.parent, self.state, self.ledger):
            with self.subTest(failing=failing.name):
                def sync(p, failing=failing):
                    if Path(p) == failing:
                        raise OSError("io error")
                    real(p)
                net = FakeNet()
                with patch("agent2telegram.notify_ledger._fsync_dir", side_effect=sync):
                    self.assertEqual(self.cli(net, "notify", "--key", "k1", "hello"), 2)
                self.assertEqual(net.requests, [])
                self.assertIn("nothing was sent", self.err)
                self.assertEqual(list(self.ledger.glob("*.json")) if self.ledger.exists() else [], [])

    def test_empty_ledger_path_is_refused(self):
        with self.assertRaises(ValueError):
            notify_ledger.notify_keyed(Mock(), Path(""), "1", "k1", 7, "hello")


class WindowTests(KeyedBase):
    def test_retry_after_a_certain_failure_keeps_the_original_window(self):
        self.assertEqual(self.cli(FakeNet(400), "notify", "--key", "k1", "hello", "--key-ttl", "1000"), 1)
        first = self.record("k1")
        self.clock[0] += 300
        self.assertEqual(self.cli(FakeNet(), "notify", "--key", "k1", "hello"), 0)   # no --key-ttl this time
        rec = self.record("k1")
        self.assertEqual((rec["created_at"], rec["expires_at"]), (first["created_at"], first["expires_at"]))
        self.assertEqual(rec["expires_at"] - rec["created_at"], 1000)
        self.assertEqual(rec["state"], "delivered")

    def test_fractional_ttl_is_not_shortened(self):
        self.clock[0] = 1_000_000.2
        self.cli(FakeNet(), "notify", "--key", "k1", "hello", "--key-ttl", "0.5")
        self.clock[0] += 0.3                                   # still inside the 0.5 s window
        net = FakeNet()
        self.assertEqual(self.cli(net, "notify", "--key", "k1", "hello"), 0)
        self.assertEqual(net.requests, [])
        self.clock[0] += 0.3                                   # now past it
        self.assertEqual(self.cli(net, "notify", "--key", "k1", "hello"), 0)
        self.assertEqual(len(net.requests), 1)


CHILD = r"""
import os, sys, time
from pathlib import Path
from unittest.mock import patch
from agent2telegram.__main__ import _main
from agent2telegram.config import Config
mode, state, log, flags, key = sys.argv[1:6]

class Resp:
    def read(self): return b'{"ok": true, "result": {"message_id": 7}}'
    def __enter__(self): return self
    def __exit__(self, *a): return False

class Net:
    def open(self, req, timeout=None):
        with open(log, "a") as fh:
            fh.write("request\n")
        if mode == "crash":
            os._exit(9)                      # process dies after the request left, before any result
        if mode == "block":
            Path(flags, "entered").write_text("x")
            end = time.time() + 30
            while not Path(flags, "go").exists() and time.time() < end:
                time.sleep(0.02)
        if mode == "slow":
            time.sleep(0.4)
        return Resp()

os.environ["AGENT2TELEGRAM_STATE"] = state
cfg = Config(agent="generic", token="1:2", allowed_user_ids=[7])
with patch("agent2telegram.config.load", return_value=cfg), \
        patch("urllib.request.build_opener", return_value=Net()):
    sys.exit(_main(["notify", "--key", key, "hello"]))
"""


class ProcessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.state, self.log, self.flags = self.tmp / "state", self.tmp / "log", self.tmp / "flags"
        self.flags.mkdir()
        self.log.write_text("")

    def spawn(self, mode, key="k1"):
        return subprocess.Popen([sys.executable, "-c", CHILD, mode, str(self.state), str(self.log),
                                 str(self.flags), key], cwd=REPO_ROOT, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)

    def run_child(self, mode, key="k1"):
        proc = self.spawn(mode, key)
        out, err = proc.communicate(timeout=60)
        return proc.returncode, out + err

    def requests(self):
        return len(self.log.read_text().splitlines())

    def test_crash_between_intent_and_result_is_uncertain_and_never_resent(self):
        self.assertEqual(self.run_child("crash")[0], 9)
        self.assertEqual(self.requests(), 1)
        rec = json.loads(next((self.state / "notify-keys").glob("*.json")).read_text())
        self.assertEqual(rec["state"], "sending")             # the durable intent survived the crash
        rc, text = self.run_child("ok")
        self.assertEqual(rc, 4, text)
        self.assertEqual(self.requests(), 1)                  # rerun sent nothing
        rec = json.loads(next((self.state / "notify-keys").glob("*.json")).read_text())
        self.assertEqual(rec["state"], "uncertain")

    def test_second_process_during_a_send_does_not_send(self):
        first = self.spawn("block")
        self.addCleanup(first.kill)
        deadline = time.time() + 30
        while not (self.flags / "entered").exists():
            self.assertLess(time.time(), deadline, "first sender never reached the network")
            time.sleep(0.02)
        rc, text = self.run_child("ok")
        self.assertEqual(rc, 5, text)
        self.assertEqual(self.requests(), 1)
        (self.flags / "go").write_text("x")
        first.communicate(timeout=30)
        self.assertEqual(first.returncode, 0)
        rc, text = self.run_child("ok")
        self.assertEqual(rc, 0, text)
        self.assertIn("already delivered", text)
        self.assertEqual(self.requests(), 1)

    def test_racing_processes_send_exactly_once(self):
        procs = [self.spawn("slow") for _ in range(4)]
        for proc in procs:
            proc.communicate(timeout=60)
        codes = sorted(p.returncode for p in procs)
        self.assertEqual(self.requests(), 1, codes)
        self.assertEqual(codes[0], 0, codes)
        self.assertTrue(set(codes) <= {0, 5}, codes)


if __name__ == "__main__":
    unittest.main()

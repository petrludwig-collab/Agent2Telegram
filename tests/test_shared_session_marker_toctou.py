"""Consuming the evaluated Stop marker must never delete the NEXT turn's Stop, even when that Stop lands exactly between the
bridge's comparison of the marker and its removal.

The Stop of turn 2 is written by the REAL hook (`stop_hook._mark`: temp file + replace) from inside the read that the consumer
uses to compare the marker (the seam that exists before and after the fix); the consequence is observed through the real outer
loop, the send gate and the fake Telegram: answer 2 reaches the right chat once, and turn 2 ends with ITS OWN Stop (the idle
fallback is disabled)."""
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from n1_rig import GROUP, OWNER, RigTestCase
import test_shared_session_finish_owner as fo
from test_shared_session_s3a import ANSWER_1, DURATION_2, FOLLOW_ANSWER


class MarkerToctouTests(RigTestCase):
    begin = fo.FinishOwnershipTests.begin
    feed = fo.FinishOwnershipTests.feed

    def finish_turn_one(self, inject_at_compare, during_finishing=None):
        """Turn 1's correct Stop is accepted; its finishing reads the start of turn 2 (held), and `inject_at_compare()` runs
        right after the consumer has read the marker it is about to remove."""
        r = self.rig
        marker = r.bridge._turn_end
        r.stop()
        state = {"in_consume": False, "done": False}
        real_read, real_consume, real_wait = Path.read_text, r.bridge._consume_turn_end, r.bridge._wait_backstop_retry

        def read_text(path, *a, **k):
            text = real_read(path, *a, **k)
            if state["in_consume"] and not state["done"] and path.name in (marker.name, ".turn_end-consume-%d" % os.getpid()):
                state["done"] = True
                inject_at_compare()
            return text

        def consume(*a, **k):
            state["in_consume"] = True
            try:
                return real_consume(*a, **k)
            finally:
                state["in_consume"] = False

        def arrive():
            if not state.get("fed"):
                state["fed"] = True
                self.feed(ANSWER_1, DURATION_2)                        # turn 2 runs completely while turn 1 is being finished
                if during_finishing:
                    during_finishing()
            real_wait()
        r.bridge._consume_turn_end, r.bridge._wait_backstop_retry = consume, arrive
        with patch.object(Path, "read_text", read_text):
            r.tick()
        r.bridge._consume_turn_end, r.bridge._wait_backstop_retry = real_consume, real_wait
        self.assertTrue(state["done"], "the comparison seam was reached")
        return marker

    def check(self, chat):
        r = self.rig
        marker = self.finish_turn_one(r.stop)                          # turn 2's real Stop lands between compare and removal
        r.drain()
        for _ in range(4):
            r.tick()
        self.assertEqual(r.sent_to(chat).count(FOLLOW_ANSWER), 1, "answer 2 delivered exactly once, to ITS chat")
        other = OWNER if chat == GROUP else GROUP
        self.assertEqual(r.sent_to(other).count(FOLLOW_ANSWER), 0)
        self.assertEqual(r.bridge._route.kind, "none", "turn 2 ended with ITS OWN Stop (idle fallback disabled)")
        self.assertFalse(marker.exists())
        self.assertEqual(sorted(p.name for p in marker.parent.glob(".turn_end-*")), [], "no temporary file is left behind")

    def test_dm_turn_two_stop_between_compare_and_removal_is_not_lost(self):
        self.begin(follow_chat=OWNER)
        self.check(OWNER)

    def test_group_turn_two_stop_between_compare_and_removal_is_not_lost(self):
        self.begin(follow_chat=GROUP)
        self.check(GROUP)

    def test_two_stops_in_a_row_the_newest_survives_and_nothing_is_left_over(self):
        r = self.rig
        self.begin(follow_chat=OWNER)
        marker = r.bridge._turn_end
        newest = {}

        def two_more():
            r.stop()                                                    # turn 2's Stop ...
            r.stop()                                                    # ... and another one right after it
            newest["text"] = marker.read_text("utf-8")
        self.finish_turn_one(two_more)
        self.assertTrue(marker.exists(), "a Stop that landed during the consumption is still there")
        self.assertEqual(marker.read_text("utf-8"), newest["text"], "the newest Stop is the one that stays")
        self.assertEqual(sorted(p.name for p in marker.parent.glob(".turn_end-*")), [])

    def test_a_newer_stop_that_was_taken_is_given_back_unless_an_even_newer_one_arrived(self):
        r = self.rig
        self.begin(follow_chat=OWNER)
        marker = r.bridge._turn_end
        newest = {}

        def third():
            r.stop()                                                    # turn 3's Stop lands while turn 2's is being given back
            newest["text"] = marker.read_text("utf-8")
        self.finish_turn_one(third, during_finishing=r.stop)            # turn 2's Stop is already there when turn 1's is consumed
        self.assertEqual(marker.read_text("utf-8"), newest["text"])
        self.assertEqual(sorted(p.name for p in marker.parent.glob(".turn_end-*")), [])

    def test_a_newer_stop_that_was_taken_is_given_back_to_the_empty_path_and_ends_turn_two(self):
        r = self.rig
        self.begin(follow_chat=OWNER)
        marker = r.bridge._turn_end
        self.finish_turn_one(lambda: None, during_finishing=r.stop)    # turn 2's Stop is already there; nothing else happens
        self.assertTrue(marker.exists(), "the claimed newer Stop went back to the path")
        r.drain()
        for _ in range(4):
            r.tick()
        self.assertEqual(r.sent_to(OWNER).count(FOLLOW_ANSWER), 1)
        self.assertEqual(r.bridge._route.kind, "none", "turn 2 ended with ITS OWN Stop (idle fallback disabled)")
        self.assertEqual(sorted(p.name for p in marker.parent.glob(".turn_end-*")), [])

    def test_a_link_failure_other_than_exists_does_not_destroy_the_newer_stop(self):
        r = self.rig
        self.begin(follow_chat=OWNER)
        marker = r.bridge._turn_end
        with patch("agent2telegram.attach.os.link", side_effect=PermissionError("no hard links")):
            self.finish_turn_one(lambda: None, during_finishing=r.stop)
        self.assertTrue(marker.exists(), "moved back without the link")
        r.drain()
        for _ in range(4):
            r.tick()
        self.assertEqual(r.sent_to(OWNER).count(FOLLOW_ANSWER), 1)
        self.assertEqual(r.bridge._route.kind, "none")
        self.assertEqual(sorted(p.name for p in marker.parent.glob(".turn_end-*")), [])

    def test_a_link_failure_with_the_path_taken_keeps_the_newer_stop_in_the_claim_file(self):
        r = self.rig
        self.begin(follow_chat=OWNER)
        marker = r.bridge._turn_end

        real_open = os.open

        def failing_open(path, flags, *a, **k):
            if str(path) == str(marker) and flags & os.O_EXCL:
                raise PermissionError("cannot create")
            return real_open(path, flags, *a, **k)
        with patch("agent2telegram.attach.os.link", side_effect=PermissionError("no hard links")), \
                patch("agent2telegram.attach.os.open", failing_open):
            self.finish_turn_one(lambda: None, during_finishing=r.stop)
        # taking the claim worked; placing it back failed: the only copy is kept, never deleted
        self.assertEqual([p.name for p in marker.parent.glob(".turn_end-consume-*")], [".turn_end-consume-%d" % os.getpid()])

    def test_a_link_failure_never_clobbers_an_even_newer_stop(self):
        r = self.rig
        self.begin(follow_chat=OWNER)
        marker = r.bridge._turn_end
        newest = {}

        def third():
            r.stop()
            newest["text"] = marker.read_text("utf-8")
        with patch("agent2telegram.attach.os.link", side_effect=PermissionError("no hard links")):
            self.finish_turn_one(third, during_finishing=r.stop)
        self.assertEqual(marker.read_text("utf-8"), newest["text"])
        self.assertEqual(sorted(p.name for p in marker.parent.glob(".turn_end-*")), [])

    def test_the_hook_publishing_an_even_newer_stop_in_the_fallback_window_is_never_overwritten(self):
        r = self.rig
        self.begin(follow_chat=OWNER)
        marker = r.bridge._turn_end
        newest = {}
        real_exists, real_open = Path.exists, os.open

        def publish_once():
            if not newest:
                r.stop()                                                # C, written by the real hook (temp file + replace)
                newest["text"] = real_read_text(marker)

        real_read_text = Path.read_text

        def exists(path):
            result = real_exists(path)
            if path == marker and not result:
                publish_once()                                          # window: after the free-check, before the placing
            return result

        def open_(path, flags, *a, **k):
            if str(path) == str(marker) and flags & os.O_EXCL:
                publish_once()                                          # window: before the exclusive create
            return real_open(path, flags, *a, **k)
        with patch("agent2telegram.attach.os.link", side_effect=PermissionError("no hard links")), \
                patch.object(Path, "exists", exists), patch("agent2telegram.attach.os.open", open_):
            self.finish_turn_one(lambda: None, during_finishing=r.stop)  # B is already there when turn 1's Stop is consumed
        self.assertTrue(newest, "the fallback window was reached")
        self.assertEqual(real_read_text(marker), newest["text"], "C survived, nothing older overwrote it")
        self.assertEqual(sorted(p.name for p in marker.parent.glob(".turn_end-*")), [])

    def test_a_short_write_in_the_fallback_keeps_the_claim_with_the_full_stop(self):
        r = self.rig
        self.begin(follow_chat=OWNER)
        marker = r.bridge._turn_end
        real_write, full = os.write, {}

        def short_write(fd, data):
            if bytes(data).startswith(b'{"timestamp"'):
                full["data"] = bytes(data)
                return real_write(fd, data[:5])                         # a prefix only, no exception
            return real_write(fd, data)
        with patch("agent2telegram.attach.os.link", side_effect=PermissionError("no hard links")), \
                patch("agent2telegram.attach.os.write", short_write):
            self.finish_turn_one(lambda: None, during_finishing=r.stop)
        claim = marker.parent / (".turn_end-consume-%d" % os.getpid())
        self.assertTrue(full, "the fallback wrote")
        self.assertTrue(claim.exists(), "the only full copy of the Stop is kept")
        self.assertEqual(claim.read_bytes(), full["data"])


if __name__ == "__main__":
    unittest.main()

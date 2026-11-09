"""Inbound videos, video notes and GIFs reach the agent as files (issue #12).

A gallery video or a screen recording arrives as `video`, not `document`. Both bridge modes
handled only photos and documents, so without a caption the message was dropped and the user got
no reply at all. Unsupported content (a sticker, a location) now gets a short notice instead.
"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent2telegram.telegram import file_media
from test_attach_queue import _bridge
from test_bridge import BridgeTestBase


def _update(**media):
    return {"update_id": 1, "message": {"chat": {"id": 7}, "from": {"id": 7},
                                       "message_id": 10, **media}}


class FileMediaTests(unittest.TestCase):
    def test_video_kinds_are_files(self):
        for kind, name in (("video", "video.mp4"), ("video_note", "video_note.mp4"),
                           ("animation", "animation.mp4")):
            media, fallback = file_media({kind: {"file_id": "x"}})
            self.assertEqual((media["file_id"], fallback), ("x", name))

    def test_gif_keeps_the_document_name(self):
        msg = {"animation": {"file_id": "a"}, "document": {"file_id": "d", "file_name": "cat.gif"}}
        self.assertEqual(file_media(msg)[0]["file_name"], "cat.gif")

    def test_text_and_photo_are_not_file_media(self):
        self.assertIsNone(file_media({"text": "hi"}))
        self.assertIsNone(file_media({"photo": [{"file_id": "p"}]}))


class AttachVideoTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        patcher = mock.patch.object(Path, "home", return_value=Path(self.home.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.home.cleanup)

    def test_video_without_caption_is_downloaded_and_injected(self):
        b = _bridge()
        b._handle(_update(video={"file_id": "v1", "file_size": 1024}))

        self.assertEqual(b.tg.file_paths, ["v1"])
        self.assertEqual(len(b._session.injected), 1)
        self.assertIn("attachments/video.mp4", b._session.injected[0])
        self.assertTrue((Path(self.home.name) / ".local/state/agent2telegram/attachments/video.mp4").exists())

    def test_video_keeps_caption_and_file_name(self):
        b = _bridge()
        b._handle(_update(caption="what is wrong here?",
                          video={"file_id": "v1", "file_name": "Screen Recording.mp4"}))

        injected = b._session.injected[0]
        self.assertTrue(injected.startswith("what is wrong here?"))
        self.assertIn("Screen_Recording.mp4", injected)

    def test_video_note_is_downloaded(self):
        b = _bridge()
        b._handle(_update(video_note={"file_id": "n1"}))

        self.assertIn("video_note.mp4", b._session.injected[0])

    def test_video_over_bot_api_limit_is_rejected_before_download(self):
        b = _bridge()
        b._handle(_update(video={"file_id": "v1", "file_size": 21 * 1024 * 1024}))

        self.assertEqual(b.tg.downloads, [])
        self.assertEqual(b._session.injected, [])
        self.assertIn("20 MB", b.tg.sent[0][1])

    def test_sticker_gets_a_notice_instead_of_silence(self):
        b = _bridge()
        b._handle(_update(sticker={"file_id": "s1"}))

        self.assertEqual(b._session.injected, [])
        self.assertEqual(len(b.tg.sent), 1)
        self.assertIn("can't pass this kind of message", b.tg.sent[0][1])

    def test_slash_caption_on_a_video_is_not_a_bridge_command(self):
        b = _bridge()
        b._handle(_update(caption="/help", video={"file_id": "v1"}))

        self.assertEqual(len(b._session.injected), 1)
        self.assertTrue(b._session.injected[0].startswith("/help"))


class ModeVideoTests(BridgeTestBase):
    def test_video_is_downloaded(self):
        task = self.bridge._build_task(100, {"video": {"file_id": "v1"}}, "")
        self.assertTrue(task.attachment.endswith("video.mp4"))

    def test_video_note_is_downloaded(self):
        task = self.bridge._build_task(100, {"video_note": {"file_id": "n1"}}, "look")
        self.assertTrue(task.attachment.endswith("video_note.mp4"))
        self.assertEqual(task.text, "look")


if __name__ == "__main__":
    unittest.main()

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent2telegram.session import TmuxSession


class TmuxSessionSubmitTests(unittest.TestCase):
    def _session(self, submit="enter"):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patches = [
            mock.patch("agent2telegram.session.shutil.which", return_value="/usr/bin/tmux"),
            mock.patch("agent2telegram.session.TmuxSession._exists", return_value=True),
            mock.patch("agent2telegram.session.time.sleep"),
            mock.patch("agent2telegram.session._tmux"),
        ]
        mocked = [p.start() for p in patches]
        for p in patches:
            self.addCleanup(p.stop)
        sess = TmuxSession([], cwd=Path(tmp.name), name="target", submit=submit)
        return sess, mocked[-1]

    def test_enter_submit_mode_uses_tmux_enter(self):
        sess, tmux = self._session()

        sess.inject("hello\nthere")

        tmux.assert_has_calls([
            mock.call("send-keys", "-t", "target", "C-u"),
            mock.call("send-keys", "-t", "target", "-l", "--", "hello there"),
            mock.call("send-keys", "-t", "target", "Enter"),
        ])

    def test_csi_u_submit_mode_uses_literal_csi_enter(self):
        sess, tmux = self._session(submit="csi-u")

        sess.inject("hello")

        tmux.assert_has_calls([
            mock.call("send-keys", "-t", "target", "C-u"),
            mock.call("send-keys", "-t", "target", "-l", "--", "hello"),
            mock.call("send-keys", "-t", "target", "-l", "\x1b[13u"),
        ])

    def test_invalid_submit_mode_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("agent2telegram.session.shutil.which", return_value="/usr/bin/tmux"):
                with self.assertRaises(ValueError):
                    TmuxSession([], cwd=Path(tmp), name="target", submit="space")


if __name__ == "__main__":
    unittest.main()

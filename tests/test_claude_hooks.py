"""Offline regressions for manual Claude attach installations and stale busy notices."""
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from agent2telegram import __main__ as cli, wizard
from agent2telegram.attach import AttachBridge
from agent2telegram.config import Config, save
from agent2telegram import claude_hooks


class ClaudeHookTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="hook home 'fixture-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.path = self.root / 'configs/config.json'
        env = patch.dict(os.environ, {'HOME': str(self.root),
            'CLAUDE_CONFIG_DIR': str(self.root / 'claude config'),
            'AGENT2TELEGRAM_CONFIG': str(self.path)})
        env.start(); self.addCleanup(env.stop)
        self.cfg = Config(agent='claude-code', token='1:FIXTURE', allowed_user_ids=[7],
                          bot_username='fixture_bot', mode='attach', tmux_session='fixture',
                          signal_file=str(self.root / 'state/answer.txt'),
                          claude_session_id='fixture-session')
        save(self.cfg, self.path)
        self.settings = self.root / 'claude config/settings.json'

    def run_bridge(self):
        with patch('agent2telegram.attach.AttachBridge') as bridge:
            self.assertEqual(cli._cmd_run(SimpleNamespace(config=str(self.path))), 0)
            bridge.return_value.run.assert_called_once()

    def test_run_registers_hook_idempotently_in_active_config_directory(self):
        self.run_bridge()
        self.assertTrue(self.settings.exists(), 'manual attach startup must install the missing hook')
        first = self.settings.read_bytes()
        self.run_bridge()
        self.assertEqual(self.settings.read_bytes(), first)
        hooks = json.loads(first)['hooks']['Stop']
        self.assertEqual(len(hooks), 1)
        self.assertIn('agent2telegram.stop_hook', hooks[0]['hooks'][0]['command'])
        self.assertFalse((self.root / '.claude/settings.json').exists())

    def test_custom_marker_writer_is_preserved_without_duplicate(self):
        marker = Path(self.cfg.signal_file).parent / 'turn_end'
        script = self.root / 'custom_hook.py'
        script.write_text('import os\nTARGET = os.path.expanduser(' + repr(str(marker)) + ')\n'
                          'def main():\n    with open(TARGET, "w") as stream:\n'
                          '        stream.write("done")\n')
        self.settings.parent.mkdir()
        data = {'customOption': {'keep': True}, 'hooks': {'Stop': [
            {'hooks': [{'type': 'command', 'command': f'python3 "{script}"'}]}]}}
        self.settings.write_text(json.dumps(data))
        before = self.settings.read_bytes()
        self.run_bridge()
        self.assertEqual(self.settings.read_bytes(), before)

    def test_broken_settings_are_preserved_and_warn_loudly(self):
        self.settings.parent.mkdir()
        self.settings.write_text('{invalid fixture settings')
        with self.assertLogs('agent2telegram.claude_hooks', level='WARNING'):
            self.run_bridge()
        self.assertEqual(self.settings.read_text(), '{invalid fixture settings')

    def test_doctor_fails_missing_hook_with_repair_command(self):
        with patch('agent2telegram.adapters.REGISTRY', {'claude-code': Mock(detect=lambda: True)}), \
             patch('agent2telegram.telegram.TelegramClient') as tg, \
             patch('sys.stdout', new_callable=io.StringIO) as output:
            tg.return_value.get_me.return_value = {'username': 'fixture_bot'}
            self.assertEqual(cli._cmd_doctor(SimpleNamespace()), 1)
            self.assertIn('claude-hook', output.getvalue())
            self.assertIn('Stop hook', output.getvalue())

    def test_wizard_honors_claude_config_directory(self):
        wizard._register_claude_hook()
        self.assertTrue(self.settings.exists())
        self.assertFalse((self.root / '.claude/settings.json').exists())

    def test_default_directory_and_standalone_repair_cli(self):
        with patch.dict(os.environ):
            os.environ.pop('CLAUDE_CONFIG_DIR', None)
            self.assertEqual(cli.main(['claude-hook', '--config', str(self.path)]), 0)
            self.assertTrue((self.root / '.claude/settings.json').exists())
            self.assertTrue(claude_hooks.check(self.cfg)[0])

    def test_doctor_repair_command_is_standalone_and_pins_settings_directory(self):
        env = dict(os.environ); env.pop('PYTHONPATH', None); env.pop('CLAUDE_CONFIG_DIR', None)
        result = subprocess.run(shlex.split(claude_hooks.repair_command(self.path)),
                                cwd=self.root, env=env, text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.settings.exists())
        self.assertFalse((self.root / '.claude/settings.json').exists())

    def test_generated_command_runs_from_unrelated_cwd_and_routes_session(self):
        self.run_bridge()
        command = json.loads(self.settings.read_text())['hooks']['Stop'][0]['hooks'][0]['command']
        second = Config(**{**vars(self.cfg), 'signal_file': str(self.root / 'other/answer.txt'),
                           'claude_session_id': 'other-session'})
        save(second, self.path.with_name('second.json'))
        # A shared config directory may contain unrelated JSON and Codex bridges.
        self.path.with_name('unrelated.json').write_text('[]')
        save(Config(**{**vars(second), 'agent': 'codex', 'claude_session_id': '',
                       'signal_file': str(self.root / 'codex/answer.txt')}),
             self.path.with_name('codex.json'))
        original = self.settings.read_bytes()
        self.assertEqual(claude_hooks.register(second, self.path.with_name('second.json')), 'present')
        self.assertEqual(self.settings.read_bytes(), original)
        env = dict(os.environ); env.pop('PYTHONPATH', None)
        for sid in ('unrelated-session', 'fixture-session'):
            result = subprocess.run(shlex.split(command), input=json.dumps({
                'transcript_path': f'/fixture/{sid}.jsonl'}), text=True, capture_output=True,
                cwd=self.root, env=env, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((self.root / 'state/turn_end').exists(), sid == 'fixture-session')
            self.assertFalse((self.root / 'other/turn_end').exists())
            self.assertFalse((self.root / 'codex/turn_end').exists())

    def test_false_positive_and_wrong_config_directory_do_not_cover(self):
        marker = self.root / 'state/turn_end'
        comment = self.root / 'comment.py'
        comment.write_text(f'# open({str(marker)!r}, "w")\nprint("turn_end")\n')
        wrong = self.root / 'wrong.py'
        wrong.write_text(f'open({str(self.root / "another/turn_end")!r}, "w")\n')
        cases = [f'python3 {shlex.quote(str(comment))}', f'python3 {shlex.quote(str(wrong))}',
                 'echo python3 -m agent2telegram.stop_hook',
                 'env AGENT2TELEGRAM_CONFIG=/fixture/elsewhere/config.json python3 -m agent2telegram.stop_hook']
        self.settings.parent.mkdir()
        for command in cases:
            data = {'hooks': {'Stop': [{'hooks': [{'type': 'command', 'command': command}]}]},
                    'env': {'FIXTURE_OPTION': 'preserve'}}
            self.settings.write_text(json.dumps(data))
            self.assertFalse(claude_hooks.check(self.cfg)[0])
            self.assertEqual(claude_hooks.register(self.cfg), 'registered')
            result = json.loads(self.settings.read_text())
            self.assertEqual(len(result['hooks']['Stop']), 2)
            self.assertEqual(result['hooks']['Stop'][0], data['hooks']['Stop'][0])
            self.assertEqual(result['env'], data['env'])

    def test_disabled_invalid_unreadable_and_symlink_settings_never_overwritten(self):
        self.settings.parent.mkdir()
        for data in ([], {'hooks': []}, {'hooks': {'Stop': {}}}, {'hooks': {'Stop': [None]}},
                     {'disableAllHooks': True}, {'hooks': {'Stop': [{'hooks': [None]}]}}):
            self.settings.write_text(json.dumps(data))
            before = self.settings.read_bytes()
            with self.assertLogs('agent2telegram.claude_hooks', level='WARNING'):
                self.assertEqual(claude_hooks.register(self.cfg), 'error')
            self.assertEqual(self.settings.read_bytes(), before)
        self.settings.write_text('{}')
        with patch.object(Path, 'read_bytes', side_effect=PermissionError('fixture-secret')), \
             self.assertLogs('agent2telegram.claude_hooks', level='WARNING') as logs:
            self.assertEqual(claude_hooks.register(self.cfg), 'error')
            self.assertNotIn('fixture-secret', str(logs.output))
        target = self.root / 'preserve.json'; target.write_text('{}')
        self.settings.unlink(); self.settings.symlink_to(target)
        self.assertEqual(claude_hooks.register(self.cfg), 'error')
        self.assertEqual(target.read_text(), '{}')

    def test_atomic_failure_preserves_settings_and_mode(self):
        self.settings.parent.mkdir(); self.settings.write_text('{"keep": true}')
        self.settings.chmod(0o640); original = self.settings.read_bytes()
        with patch('agent2telegram.claude_hooks.os.replace', side_effect=OSError('fixture-secret')), \
             self.assertLogs('agent2telegram.claude_hooks', level='WARNING') as logs:
            self.assertEqual(claude_hooks.register(self.cfg), 'error')
            self.assertNotIn('fixture-secret', str(logs.output))
        self.assertEqual(self.settings.read_bytes(), original)
        self.assertEqual(list(self.settings.parent.glob('.settings-a2t-*')), [])
        self.assertEqual(claude_hooks.register(self.cfg), 'registered')
        self.assertEqual(self.settings.stat().st_mode & 0o777, 0o640)

    def test_concurrent_registration_adds_one_hook(self):
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: claude_hooks.register(self.cfg), range(4)))
        self.assertEqual(sorted(results), ['present', 'present', 'present', 'registered'])
        self.assertEqual(len(json.loads(self.settings.read_text())['hooks']['Stop']), 1)
        self.assertEqual(self.settings.stat().st_mode & 0o777, 0o600)

    def test_doctor_passes_existing_hook_and_non_claude_never_registers(self):
        self.run_bridge()
        with patch('agent2telegram.adapters.REGISTRY', {'claude-code': Mock(detect=lambda: True)}), \
             patch('agent2telegram.telegram.TelegramClient'), patch('sys.stdout', new_callable=io.StringIO):
            self.assertEqual(cli._cmd_doctor(SimpleNamespace()), 0)
        self.cfg.agent = 'codex'; save(self.cfg, self.path)
        with patch('agent2telegram.claude_hooks.register') as register:
            self.run_bridge(); register.assert_not_called()

    def test_stale_missing_hook_suppresses_busy_without_closing_turn(self):
        b = object.__new__(AttachBridge)
        b.cfg = self.cfg; b._allowed = {7}; b.tg = Mock()
        b._turn_active = threading.Event(); b._turn_active.set()
        b._claude_hook_ready = False
        b._last_activity = 1; b._turn_started = 1
        with patch('agent2telegram.attach.time.monotonic', return_value=200):
            b._maybe_ack_queued({'message': {'from': {'id': 7}, 'chat': {'id': 7}}})
        b.tg.send_message.assert_not_called()
        self.assertTrue(b._turn_active.is_set(), 'silence must not release chat ownership')

    def test_recent_activity_and_confirmed_hook_keep_busy(self):
        for ready, activity in ((False, 190), (True, 1)):
            b = object.__new__(AttachBridge)
            b.cfg = self.cfg; b._allowed = {7}; b.tg = Mock()
            b._turn_active = threading.Event(); b._turn_active.set()
            b._claude_hook_ready = ready; b._last_activity = activity; b._turn_started = 1
            with patch('agent2telegram.attach.time.monotonic', return_value=200):
                b._maybe_ack_queued({'message': {'from': {'id': 7}, 'chat': {'id': 7}}})
            b.tg.send_message.assert_called_once()

    def test_stale_typing_stops_but_late_reply_keeps_original_chat(self):
        from test_late_answer import _bridge, _assistant
        b = _bridge(self.root)
        b._turn_active.set(); b._claude_hook_ready = False
        b._turn_end = self.root / 'state/turn_end'
        b._last_activity = 1; b._turn_started = 1; b._last_typing = 1
        b.tg.send_chat_action = Mock()
        with patch('agent2telegram.attach.time.monotonic', return_value=200), \
             patch.object(b._stop, 'wait', side_effect=lambda _: b._stop.set()):
            b._typing_loop()
        b.tg.send_chat_action.assert_not_called()
        self.assertTrue(b._turn_active.is_set())
        self.assertEqual(b._idle_limit(), 600)
        b._transcript.write_text(_assistant('late', '[TG] The late answer.') + '\n')
        b._drain_transcript()
        self.assertEqual(b.tg.sent, [(7, 'The late answer.')])
        self.assertTrue(b._busy_for_notice())

    def test_cross_chat_silence_does_not_claim_busy_or_release_owner(self):
        from agent2telegram import attach
        from test_reply_binding import make_bridge, msg, OWNER, OTHER, GROUP
        b = make_bridge(self.root)
        b.cfg.group_interim = True  # preserve coverage of the optional cross-chat notice
        b._turn_active.set(); b._claude_hook_ready = False
        # Absolute zero is not 'long ago' on a freshly booted CI runner (monotonic ~ uptime).
        b._last_activity = b._turn_started = time.monotonic() - attach.IDLE_DONE - 1
        warned = threading.Event()
        b.tg.send_message.side_effect = lambda *_: warned.set()
        with patch.object(attach, 'CHAT_SWITCH_MAX_WAIT', 0), patch.object(attach, 'CHAT_SWITCH_POLL', .01):
            worker = threading.Thread(target=b._handle, args=(msg(OTHER, GROUP, 'supergroup'),))
            worker.start()
            try:
                self.assertTrue(warned.wait(2))
                self.assertIn('confirms completion', b.tg.send_message.call_args.args[1])
                self.assertEqual(b._owner_chat, OWNER)
                b._inject.assert_not_called()
            finally:
                b._turn_active.clear(); worker.join(2)
        self.assertEqual(b._owner_chat, GROUP)

    def test_observed_marker_confirms_hook_without_waiting_for_silence(self):
        from test_late_answer import _bridge
        b = _bridge(self.root); b._claude_hook_ready = False
        b._turn_active.set(); b._turn_end = self.root / 'turn_end'; b._turn_end.touch()
        b._stop = Mock(); b._stop.is_set.side_effect = [False, True]
        for name in ('_maybe_reresolve','_flush_pending','_drain_transcript','_drain_signal',
                     '_expire_background_status','_replay_pending_inbound','_beat'):
            setattr(b, name, Mock())
        b._end_turn = Mock()
        b._outbound_loop()
        self.assertTrue(b._claude_hook_ready)
        b._end_turn.assert_called_once()

    def test_uninstall_honors_active_claude_directory(self):
        self.run_bridge()
        with patch('agent2telegram.config.DEFAULT_PATH', self.path), \
             patch('agent2telegram.updater._running_bridges', return_value=[]), \
             patch('importlib.util.find_spec', return_value=None), \
             patch('sys.stdout', new_callable=io.StringIO):
            self.assertEqual(cli._cmd_uninstall(SimpleNamespace(yes=True)), 0)
        self.assertEqual(json.loads(self.settings.read_text())['hooks']['Stop'], [])


if __name__ == '__main__':
    unittest.main()

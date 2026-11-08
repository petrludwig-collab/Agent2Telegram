"""Sender display context never changes authorization or reply routing."""
import io
import tempfile
import unicodedata
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from unittest.mock import Mock, patch

from agent2telegram.config import Config, ConfigError
from tests.test_reply_binding import make_bridge, msg


class SenderLabelTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.b=make_bridge(self.tmp.name)
        # Sender labels are opt-in; English remains the default language.
        self.b.cfg.inbound_sender_label=True
        self.b._limit_inbound_prompt=lambda text,chat:text

    def incoming(self,kind='private',name='Alex',title='Team'):
        update=msg(22,22 if kind=='private' else -500,kind)
        update['message']['from']['first_name']=name
        update['message']['chat']['title']=title
        return update

    def test_private_label_and_original_routing(self):
        self.assertTrue(self.b._handle(self.incoming()))
        self.b._inject.assert_called_once_with('[From: Alex · DM] Hello')
        self.assertEqual(self.b._owner_chat,22)

    def test_group_label_and_original_routing(self):
        self.assertTrue(self.b._handle(self.incoming('supergroup')))
        self.b._inject.assert_called_once_with('[From: Alex · group "Team"] Hello')
        self.assertEqual(self.b._owner_chat,-500)

    def test_label_sanitizes_brackets_controls_bidi_quotes_and_length(self):
        self.b._handle(self.incoming('group',name='A]\n\x00\u202e["'+'x'*1000,title='T]\r\t\u2028["'+'y'*1000))
        text=self.b._inject.call_args.args[0]
        self.assertTrue(text.startswith('[From: ')); self.assertTrue(text.endswith('] Hello'))
        self.assertEqual(text.count('['),1); self.assertEqual(text.count(']'),1)
        self.assertFalse(any(unicodedata.category(c).startswith('C') or c in '\n\r\t\u2028' for c in text))
        self.assertLess(len(text),200)
        self.assertEqual(text.count('"'),2)

    def test_disabled_unchanged_and_config_strict_boolean(self):
        self.b.cfg.inbound_sender_label=False
        self.b._handle(self.incoming('group')); self.b._inject.assert_called_once_with('Hello')
        cfg=Config(agent='codex',token='fixture:only')
        self.assertFalse(cfg.inbound_sender_label)
        cfg.inbound_sender_label='yes'
        with self.assertRaises(ConfigError): cfg.validate()

    def test_voice_attachment_reply_context_preserve_current_sender(self):
        self.b._transcribe=Mock(return_value='Synthetic voice')
        update=self.incoming(); update['message'].update(text='',voice={'file_id':'fixture'})
        self.b._handle(update)
        text=self.b._inject.call_args.args[0]
        self.assertTrue(text.startswith('[From: Alex · DM] ')); self.assertIn('voice transcript',text)
        self.b._inject.reset_mock(); self.b._download_note=Mock(return_value='[file: fixture]')
        update=self.incoming('group'); update['message']['document']={'file_id':'fixture'}
        update['message']['reply_to_message']={'text':'Another speaker','from':{'first_name':'Robin'}}
        self.b._handle(update)
        text=self.b._inject.call_args.args[0]
        self.assertTrue(text.startswith('[From: Alex · group "Team"] ')); self.assertIn('[file: fixture]',text)

    def test_unauthorized_cannot_inject_label(self):
        update=self.incoming(); update['message']['from']['id']=99
        self.b._handle(update); self.b._inject.assert_not_called()

    def test_notify_remains_owner_dm_with_flag_enabled(self):
        from agent2telegram.__main__ import _cmd_notify
        args=Namespace(config=None,file=None,message='Synthetic notice')
        with patch('agent2telegram.config.load',return_value=self.b.cfg), \
             patch('agent2telegram.telegram.TelegramClient') as client, redirect_stdout(io.StringIO()):
            self.assertEqual(_cmd_notify(args),0)
        client.return_value.send_message.assert_called_once_with(11,'Synthetic notice')

    def test_reaction_uses_reacting_sender(self):
        self.b._handle({'message_reaction': {
            'user': {'id':22,'first_name':'Alex'},
            'chat': {'id':22,'type':'private'}, 'message_id':7,
            'new_reaction':[{'type':'emoji','emoji':'❤'}]}})
        self.assertTrue(self.b._inject.call_args.args[0].startswith('[From: Alex · DM] ❤ reacted'))

    def test_prefix_precedes_label_in_actual_tmux_payload(self):
        from agent2telegram.session import TmuxSession
        session=object.__new__(TmuxSession)
        session.name='fixture'; session._origin='[TG] '
        session._pane_ok=Mock(return_value=(True,''))
        self.b._handle(self.incoming())
        with patch('agent2telegram.session._tmux') as tmux, patch('agent2telegram.session.time.sleep'):
            tmux.return_value.stdout = ''
            session._send_keys(self.b._inject.call_args.args[0])
        tmux.assert_any_call('send-keys','-t','fixture','-l','--','[TG] [From: Alex · DM] Hello')

    def test_missing_display_fields_have_bounded_generic_fallback(self):
        from agent2telegram.inbound import sender_label
        self.assertEqual(sender_label({'chat':{'type':'group'}}),
                         '[From: user · group "untitled"]')

    def test_english_default_and_explicit_czech_compatibility(self):
        from agent2telegram.inbound import sender_label
        private = {'from': {'first_name': 'Alex'}, 'chat': {'type': 'private'}}
        self.assertEqual(sender_label(private), '[From: Alex · DM]')
        self.assertEqual(sender_label(private, language='cs'), '[Od: Alex · DM]')
        self.assertEqual(sender_label({'chat': {'type': 'group'}}, language='cs'),
                         '[Od: uživatel · skupina "bez názvu"]')

    def test_czech_injection_preserves_prefix_for_all_inbound_paths(self):
        self.b.cfg.language = 'cs'
        for kind, expected in [('private', '[Od: Alex · DM] '),
                               ('supergroup', '[Od: Alex · skupina "Team"] ')]:
            for media in ({}, {'voice': {'file_id': 'fixture'}},
                          {'document': {'file_id': 'fixture'}}):
                with self.subTest(kind=kind, media=media):
                    self.b._transcribe = Mock(return_value='Synthetic voice')
                    self.b._download_note = Mock(return_value='[file: fixture]')
                    update = self.incoming(kind)
                    update['message'].update(media)
                    self.b._handle(update)
                    self.assertTrue(self.b._inject.call_args.args[0].startswith(expected))
        self.b._handle({'message_reaction': {
            'user': {'id': 22, 'first_name': 'Alex'},
            'chat': {'id': 22, 'type': 'private'}, 'message_id': 7,
            'new_reaction': [{'type': 'emoji', 'emoji': '❤'}]}})
        self.assertTrue(self.b._inject.call_args.args[0].startswith('[Od: Alex · DM] ❤ reacted'))


if __name__=='__main__': unittest.main()

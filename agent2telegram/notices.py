"""Opt-in operational notices; no model calls, credentials, or shell commands."""
import hashlib
import json
import logging
import math
import os
import time

from .compat import AlreadyRunning, single_instance_lock
from .config import _state_dir
from .durable import _atomic_write_json


log = logging.getLogger('agent2telegram.notices')
COOLDOWN = 30.0


def busy_text(cfg):
    if getattr(cfg, 'language', 'en') == 'cs':
        return '⚡ Zprávu mám. Dostanu se k ní, jakmile dokončím rozpracovaný úkol.'
    return "⚡ Got your message. I'll get to it as soon as I finish what I'm on."


def waiting_for_turn_end_text(cfg):
    if getattr(cfg, 'language', 'en') == 'cs':
        return '⏳ Zpráva čeká ve frontě na potvrzení konce předchozího tahu. Směrování odpovědi zůstává chráněné.'
    return '⏳ Your message is queued until the previous turn confirms completion. Reply routing stays protected.'


def default_restart_text(language):
    return '🔄 Most je znovu připojený.' if language == 'cs' else '🔄 Bridge started and connected.'


def restart_notice(bridge, chat_id):
    """Call once after startup checks, never from the poll/health loop.

    Claim the timestamp BEFORE sending: if Telegram accepted the message but its
    acknowledgement was lost, a process restart must not immediately send it again.
    On a failed attempt there is no background retry; normal bridge work continues.
    """
    cfg = bridge.cfg
    if not getattr(cfg, 'restart_notice', False) or chat_id is None:
        return
    if getattr(bridge, '_restart_notice_attempted', False):
        return
    bridge._restart_notice_attempted = True
    identity = f'{cfg.tmux_session}\0{cfg.bot_username}\0{chat_id}'
    suffix = hashlib.sha256(identity.encode()).hexdigest()[:16]
    root = _state_dir(cfg)
    marker = root / ('last_start_notified_' + suffix + '.json')
    try:
        root.mkdir(parents=True, exist_ok=True)
        with single_instance_lock(root / ('restart_notice_' + suffix + '.lock')):
            now = time.time()
            try:
                fd = os.open(marker, os.O_RDONLY | os.O_NOFOLLOW)
                with os.fdopen(fd) as stream:
                    previous = json.load(stream)['at']
                if type(previous) not in (int, float) or not math.isfinite(previous):
                    raise ValueError('invalid restart notice timestamp')
                if now - previous < COOLDOWN:
                    return
            except FileNotFoundError:
                pass
            _atomic_write_json(marker, {'at':now, 'pid':os.getpid()})
            text = cfg.restart_notice_text or default_restart_text(cfg.language)
            if getattr(bridge, '_explicit_only', False):
                from .outbound_policy import RestartNotice
                bridge.tg.send_service(chat_id, RestartNotice(cfg.language))
            else:
                bridge.tg.send_message(chat_id, text)
            log.info('startup notice sent')
    except AlreadyRunning:
        return
    except Exception as exc:
        # Provider exceptions may contain token-bearing URLs. Never log their message.
        log.warning('startup notice skipped/uncertain (%s); bridge continues', type(exc).__name__)

"""Display-only defense in depth. Redact structured credentials before truncation."""
import hashlib
import json
import re
from urllib.parse import urlsplit, urlunsplit

MASK = "[REDACTED]"
KEY = r"(?:[\w-]*(?:api[_-]?key|token|password|passwd|secret|authorization|credential|private[_-]?key)|pass|heslo)"
QUOTED = r'''"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*' '''
# Consume an existing marker whole: later passes must not leave its closing ].
VALUE = "(?:" + QUOTED.strip() + "|(?:" + re.escape(MASK) + r"|[^\s,;}\]&])+)"
PAIR = re.compile(r'''(?i)(["']?\b''' + KEY + r'''["']?[ \t]*[:=][ \t]*)''' + VALUE)
ARG = re.compile(r"(?i)(--?" + KEY + r"\s+)" + VALUE)
ENV = re.compile(r"(?<![\w-])(([A-Z_][A-Z0-9_]*)[ \t]*=[ \t]*)(" + VALUE + ")")
ENV_SECRET_KEY = re.compile(r"(?:^|_)(?:key|token|secret|password|passwd|pass|auth(?:orization)?|credentials?|heslo)(?:_|$)", re.I)
AUTH = re.compile(r"(?i)\b(?:Bearer|Basic)\s+[A-Za-z0-9+/_.=:%-]+")
TOKENS = re.compile(r"(?:\d{6,16}:[A-Za-z0-9_-]{20,}|\b(?:sk-ant-|sk-proj-|sk_|ghp_|github_pat_|xox[bp]-|AIza|AKIA)[A-Za-z0-9_-]{6,})")
URL = re.compile(r"""https?://[^\s<>"']+""", re.I)

def sensitive(key):
    return bool(re.fullmatch(KEY, str(key), re.I))

def _env(match):
    key, value = match.group(2), match.group(3)
    if sensitive(key) or ENV_SECRET_KEY.search(key) or TOKENS.search(value):
        return match.group(1) + MASK
    return match.group()

def _url(match):
    try:
        u=urlsplit(match.group())
        host=u.hostname or ""
        if ":" in host:host="["+host+"]"
        if u.port:host+=":"+str(u.port)
        return urlunsplit((u.scheme,host,u.path, MASK if u.query else "", MASK if u.fragment else ""))
    except ValueError:
        return MASK

def _text(text):
    # Shell/JSON escaping can hide delimiters from a text regex. Diagnostic
    # output may be suppressed entirely; never partially expose such a value.
    if '\\' in text and re.search(KEY, text, re.I):
        return MASK
    text=URL.sub(_url,text)
    text=AUTH.sub(MASK,text)
    for pattern in (PAIR,ARG):
        text=pattern.sub(lambda m:m.group(1)+MASK,text)
    text=ENV.sub(_env,text)
    return TOKENS.sub(MASK,text)

def _tree(value):
    if isinstance(value,dict):
        return {k:MASK if sensitive(k) else _tree(v) for k,v in value.items()}
    if isinstance(value,list):return [_tree(v) for v in value]
    if isinstance(value,str):return _text(value)
    return value

def redact(value):
    text=str(value)
    try:parsed=json.loads(text)
    except (ValueError,RecursionError):return _text(text)
    try:return json.dumps(_tree(parsed),ensure_ascii=False) if isinstance(parsed,(dict,list)) else _text(text)
    except RecursionError:return MASK


def message_metadata(text: str, *, kind: str = "text", attachments: int = 0) -> str:
    """Content-free diagnostics. Short hashes correlate messages, not authenticate them.

    Hash the complete, untruncated text; previews leak arbitrary private text even when no
    credential pattern matches. A short unkeyed hash is not an anonymity guarantee.
    """
    digest = hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()[:8]
    return f"kind={kind} len={len(text)} attachments={attachments} sha256={digest}"


def update_metadata(update: dict) -> str:
    """Never format raw Telegram fields (including malformed IDs and quoted messages)."""
    if not isinstance(update, dict):
        return message_metadata("", kind="invalid_update")
    msg = update.get("message") or update.get("edited_message") or {}
    if not isinstance(msg, dict):
        msg = {}
    media = ("photo", "document", "video", "video_note", "animation", "voice", "audio", "sticker")
    count = sum(bool(msg.get(field)) for field in media)
    kind = ("voice" if msg.get("voice") or msg.get("audio") else "file" if count
            else "reaction" if update.get("message_reaction") else "text")
    text = msg.get("text") or msg.get("caption") or ""
    text = text if isinstance(text, str) else ""
    reply = msg.get("reply_to_message") or {}
    reply = reply if isinstance(reply, dict) else {}

    def numeric_id(value):
        return value if type(value) is int else None

    return (f"message_id={numeric_id(msg.get('message_id'))} "
            f"reply_to_id={numeric_id(reply.get('message_id'))} "
            + message_metadata(text, kind=kind, attachments=count))


class RedactingFilter:
    """logging filter: mask credentials in EVERY log line before it is written.

    Message call sites must use content-free metadata. This filter is defense in depth for
    structured credentials in other diagnostics; it cannot recognize arbitrary private text
    or truncated OAuth codes and must never be used to justify logging message previews.
    """

    def filter(self, record):
        try:
            message = record.getMessage()
        except Exception:
            # Unformattable record: never let its raw arguments reach the file.
            record.msg, record.args = MASK, None
            message = None
        if message is not None:
            clean = _text(message)
            if clean != message:
                record.msg, record.args = clean, None
        # Tracebacks are appended by the formatter AFTER filters run, so a credential inside an
        # exception message bypassed the mask (audit 2026-10-10, second run). Render it here,
        # mask it, and hand the formatter only the masked text.
        if record.exc_info:
            import logging
            try:
                record.exc_text = _text(logging.Formatter().formatException(record.exc_info))
            except Exception:
                record.exc_text = MASK
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = _text(record.exc_text)
        if record.stack_info:
            record.stack_info = _text(record.stack_info)
        return True


def install_log_redaction(logger=None):
    import logging
    root = logger or logging.getLogger()
    for handler in root.handlers:
        if not any(isinstance(f, RedactingFilter) for f in handler.filters):
            handler.addFilter(RedactingFilter())

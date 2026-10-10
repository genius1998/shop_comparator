"""Central structured logging for the web app.

Every record carries job id, shop, product id, stage and error kind so one
collection run can be traced end to end.  Records go to a rotating JSON-lines
file (logs/app.jsonl, git-ignored) and to an in-memory ring the web UI reads.
Secrets are masked before a record is stored anywhere.
"""
import json
import logging
import os
import re
import threading
from collections import deque
from contextvars import ContextVar
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler

from shop_comparator.core.utils import get_persistent_path

LOG_DIR = get_persistent_path("logs")
LOG_FILE = os.path.join(LOG_DIR, "app.jsonl")
MAX_BYTES = 5 * 1024 * 1024
BACKUP_COUNT = 5
RING_SIZE = 3000

_context = ContextVar("shop_log_context", default={})
_ring = deque(maxlen=RING_SIZE)
_ring_lock = threading.Lock()
_sequence = 0
_secrets = set()
_secrets_lock = threading.Lock()

_PATTERNS = [
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{8,}"), r"\1***"),
    (re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}"), "***jwt***"),
    (re.compile(r"(?i)\b(app_atk|app_rtk|access_token|refresh_token|password|passwd|pwd|cookie|authorization|secret)(\"?\s*[:=]\s*\"?)[^\s\"',;&}]+"), r"\1\2***"),
    (re.compile(r"https://(?:ptb\.|canary\.)?discord(?:app)?\.com/api/webhooks/\S+"), "https://discord.com/api/webhooks/***"),
    (re.compile(r"\b([A-Za-z0-9._%+-]{1,2})[A-Za-z0-9._%+-]*@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b"), r"\1***@\2"),
]


def register_secret(value):
    """Mask this exact value (a password the user typed) in every later record."""
    value = str(value or "")
    if len(value) >= 4:
        with _secrets_lock:
            _secrets.add(value)


def forget_secret(value):
    with _secrets_lock:
        _secrets.discard(str(value or ""))


def redact(text):
    text = str(text)
    with _secrets_lock:
        known = sorted(_secrets, key=len, reverse=True)
    for secret in known:
        text = text.replace(secret, "***")
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def mask_account(value):
    value = str(value or "").strip()
    if not value:
        return ""
    if "@" in value:
        name, domain = value.split("@", 1)
        return f"{name[:2]}***@{domain}"
    return f"{value[:2]}***"


class bind:
    """Context manager adding fields (job, shop, goods, stage) to nested records."""

    def __init__(self, **fields):
        self.fields = {k: v for k, v in fields.items() if v is not None}

    def __enter__(self):
        self.token = _context.set({**_context.get(), **self.fields})
        return self

    def __exit__(self, *exc):
        _context.reset(self.token)


def current_context():
    return dict(_context.get())


class _JsonFormatter(logging.Formatter):
    def format(self, record):
        return json.dumps(record.event, ensure_ascii=False)


_logger = None
_logger_lock = threading.Lock()


def _file_logger():
    global _logger
    with _logger_lock:
        if _logger is None:
            os.makedirs(LOG_DIR, exist_ok=True)
            logger = logging.getLogger("shop_comparator")
            logger.setLevel(logging.INFO)
            logger.propagate = False
            if not logger.handlers:
                handler = RotatingFileHandler(LOG_FILE, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8")
                handler.setFormatter(_JsonFormatter())
                logger.addHandler(handler)
            _logger = logger
        return _logger


def log(message, level="info", **fields):
    """Record one event. Extra keyword fields override the bound context."""
    global _sequence
    context = {**_context.get(), **{k: v for k, v in fields.items() if v is not None}}
    event = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "level": level,
        "job": context.get("job"),
        "shop": context.get("shop"),
        "goods": str(context["goods"]) if context.get("goods") is not None else None,
        "stage": context.get("stage"),
        "error": context.get("error"),
        "message": redact(message),
    }
    with _ring_lock:
        _sequence += 1
        event["seq"] = _sequence
        _ring.append(event)
    try:
        record = logging.LogRecord("shop_comparator", getattr(logging, level.upper(), logging.INFO), "", 0, "", (), None)
        record.event = event
        _file_logger().handle(record)
    except Exception:
        pass  # logging must never break a crawl
    return event


def error(message, error_kind="error", **fields):
    return log(message, level="error", error=error_kind, **fields)


def warn(message, **fields):
    return log(message, level="warning", **fields)


def recent(after=0, job=None, shop=None, level=None, query=None, limit=500):
    with _ring_lock:
        events = [e for e in _ring if e["seq"] > after]
    if job:
        events = [e for e in events if e["job"] == job]
    if shop:
        events = [e for e in events if e["shop"] == shop]
    if level:
        events = [e for e in events if e["level"] == level]
    if query:
        needle = query.lower()
        events = [e for e in events if needle in e["message"].lower() or needle == (e["goods"] or "")]
    return events[-limit:]


def last_seq():
    with _ring_lock:
        return _sequence


def callback(**fields):
    """Adapter for legacy functions that take log_callback(text)."""
    def _log(text):
        text = str(text)
        level = "error" if text.startswith(("[ERROR]", "[EXCEPT]", "[CRITICAL")) else "warning" if text.startswith("[WARN]") else "info"
        log(text, level=level, **fields)
    return _log

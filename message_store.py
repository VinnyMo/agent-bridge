"""Shared append implementation for the authenticated bridge and local agents."""
import fcntl
import hashlib
import json
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path

MAX_BODY = 64 * 1024
MAX_LOG_BYTES = 64 * 1024 * 1024


class MessageError(ValueError):
    def __init__(self, status, code):
        self.status, self.code = status, code
        super().__init__(code)


def validate(payload):
    if not isinstance(payload, dict) or set(payload) - {'agent', 'message'}:
        raise MessageError(400, 'expected_message_and_optional_agent')
    message, agent = payload.get('message'), payload.get('agent', 'AI agent')
    if not isinstance(message, str) or not message.strip():
        raise MessageError(400, 'message_must_not_be_empty')
    if not isinstance(agent, str) or not agent.strip() or len(agent) > 40:
        raise MessageError(400, 'agent_must_be_1_to_40_characters')
    if any(ord(c) < 32 and c not in '\n\t' for c in message) or any(ord(c) < 32 for c in agent):
        raise MessageError(400, 'control_characters_not_allowed')
    try:
        size = len(json.dumps(payload, ensure_ascii=False).encode('utf-8'))
    except UnicodeEncodeError:
        raise MessageError(400, 'invalid_unicode') from None
    if size > MAX_BODY:
        raise MessageError(413, 'body_too_large')
    return agent.strip(), message.strip()


def append_message(path, payload):
    agent, message = validate(payload)
    record = {'id': secrets.token_hex(12),
              'created_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
              'agent': agent, 'message': message}
    encoded = (json.dumps(record, ensure_ascii=False, separators=(',', ':')) + '\n').encode()
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        original_size = os.fstat(fd).st_size
        if original_size + len(encoded) > MAX_LOG_BYTES:
            raise MessageError(507, 'message_log_full')
        try:
            view = memoryview(encoded)
            while view:
                count = os.write(fd, view)
                if count <= 0:
                    raise OSError('short write')
                view = view[count:]
            os.fsync(fd)
        except OSError:
            os.ftruncate(fd, original_size)
            raise
    finally:
        os.close(fd)
    return {'status': 'appended', 'id': record['id'],
            'receipt': hashlib.sha256(record['id'].encode()).hexdigest()[:12]}

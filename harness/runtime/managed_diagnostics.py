"""Bounded operator diagnostics beside state, separate from conversation history."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import uuid

from managed_codex import ManagedCodexError

PROVIDER_CODES = frozenset({
    'http_error', 'transport_error', 'invalid_response', 'artifact_encoding',
    'artifact_size', 'invalid_event_stream', 'stream_disconnected', 'event_too_large',
    'invalid_event', 'incomplete_event', 'invalid_history_page',
    'invalid_pagination_cursor', 'pagination_limit_reached', 'recovery_buffer_overflow',
    'recovery_attempts_exhausted',
})


def _details(error, phase):
    name = type(error).__name__
    value = {'schema_version': 1, 'recorded_at': datetime.now(timezone.utc).isoformat(),
             'phase': phase, 'exception_type': name if re.fullmatch(r'[A-Za-z_]{1,80}', name) else 'Exception'}
    trace = error.__traceback__
    while trace is not None and trace.tb_next is not None:
        trace = trace.tb_next
    if trace is not None:
        code = trace.tb_frame.f_code
        source = Path(code.co_filename).name
        if re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', source):
            value.update(source=source, line=trace.tb_lineno)
        message = str(error)
        if isinstance(error, ValueError) and message in code.co_consts and len(message) <= 280:
            value['message'] = message
    if isinstance(error, ManagedCodexError):
        provider = {}
        if error.code in PROVIDER_CODES:
            provider['code'] = error.code
        if type(error.status_code) is int and 100 <= error.status_code <= 599:
            provider['status_code'] = error.status_code
        if isinstance(error.request_id, str) and re.fullmatch(r'req_[A-Za-z0-9_-]{1,160}', error.request_id):
            provider['request_id'] = error.request_id
        value['provider'] = provider
    return value


def record_failure(state, chat_id, error, *, phase):
    """Keep the latest private failure per hashed chat; failures here preserve UI.

    Only static local ValueError literals and stable transport fields are stored.
    The original exception, request, configuration and arguments are never dumped.
    """
    directory = None
    temporary = None
    try:
        if phase not in {'recovery', 'run'} or not isinstance(chat_id, str) or len(chat_id) > 512:
            return False
        data = json.dumps(_details(error, phase), ensure_ascii=False, separators=(',', ':')).encode()
        if len(data) > 4096:
            return False
        root = Path(state) / 'runtime-private'
        root.mkdir(mode=0o700, exist_ok=True)
        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        details = os.fstat(directory)
        if not stat.S_ISDIR(details.st_mode) or details.st_uid != os.geteuid() or details.st_mode & 0o077:
            return False
        name = 'managed-failure-' + hashlib.sha256(chat_id.encode()).hexdigest() + '.json'
        temporary = '.failure-' + uuid.uuid4().hex
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=directory)
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
        temporary = None
        os.fsync(directory)
        return True
    except (OSError, ValueError, TypeError):
        return False
    finally:
        if directory is not None:
            if temporary is not None:
                try:
                    os.unlink(temporary, dir_fd=directory)
                except OSError:
                    pass
            os.close(directory)

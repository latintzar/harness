"""Versioned dynamic-tool migration for the installed Codex app-server 0.153.4.

Only thread/start accepts dynamicTools in this version. The caller owns registry
updates and retains the original native thread and complete UI/audit history.
This helper copies bounded public messages, not native turns or hidden/tool state.
"""
from __future__ import annotations

import base64
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import stat

from workspace_identity import workspace_scope

MAX_MESSAGES = 200
MAX_TEXT_BYTES = 256 * 1024
MAX_INJECT_BYTES = 8 * 1024 * 1024
MAX_IMAGE_BYTES = 4 * 1024 * 1024


def _thread(response):
    thread = response.get('thread') if isinstance(response, dict) else None
    if not isinstance(thread, dict) or not isinstance(thread.get('id'), str) or not thread['id']:
        raise ValueError('Native RPC returned an invalid thread')
    return thread


def _image_mime(data):
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'image/png'
    if data.startswith(b'\xff\xd8\xff'):
        return 'image/jpeg'
    if data.startswith((b'GIF87a', b'GIF89a')):
        return 'image/gif'
    if data.startswith(b'RIFF') and data[8:12] == b'WEBP':
        return 'image/webp'
    return None


def _image(part, attachment_root, item_id):
    """Copy existing references or owned attachment bytes; never fetch a URL."""
    provenance = {'history_item_id':item_id, 'source_kind':part['type']}
    if part['type'] == 'localImage':
        raw_path = part.get('path')
        if not isinstance(raw_path, str) or not Path(raw_path).is_absolute():
            return None, None, 'local_image_invalid_path'
        path = Path(raw_path).resolve()
        if attachment_root is None or not path.is_relative_to(attachment_root):
            return None, None, 'local_image_outside_attachments'
        try:
            if not path.is_file() or not 0 < path.stat().st_size <= MAX_IMAGE_BYTES:
                return None, None, 'local_image_missing_or_size_limit'
            with path.open('rb') as stream:
                data = stream.read(MAX_IMAGE_BYTES + 1)
        except OSError:
            return None, None, 'local_image_unavailable'
        mime = _image_mime(data)
        if not mime or len(data) > MAX_IMAGE_BYTES:
            return None, None, 'local_image_invalid_or_size_limit'
        url = 'data:' + mime + ';base64,' + base64.b64encode(data).decode('ascii')
        provenance.update(source_path=str(path), sha256=hashlib.sha256(data).hexdigest(),
                          historical_bytes_verified=False,
                          verification='Current owned attachment bytes; original rollout records no content hash')
    else:
        url = part.get('url')
        if not isinstance(url, str) or not url.startswith(('data:image/', 'https://', 'http://')):
            return None, None, 'image_reference_unsupported'
        if len(url.encode()) > (MAX_IMAGE_BYTES * 4 // 3 + 256):
            return None, None, 'image_reference_size_limit'
        provenance.update(reference_sha256=hashlib.sha256(url.encode()).hexdigest(),
                          verification='Stored embedded image' if url.startswith('data:') else 'Stored URL; content not fetched or revalidated')
    content = {'type':'input_image', 'image_url':url}
    if part.get('detail') in {'auto','low','high','original'}:
        content['detail'] = part['detail']
    return content, provenance, None


def _public_history(thread, cwd):
    turns = thread.get('turns')
    if not isinstance(turns, list):
        raise ValueError('Read complete native turn history before tool migration')
    omitted = {'public_messages':0, 'image_parts':0, 'other_user_parts':0,
               'reasons':Counter(), 'excluded_item_types':Counter()}
    candidates = []
    for turn in turns:
        if not isinstance(turn, dict) or not isinstance(turn.get('items'), list):
            raise ValueError('Native turn history has an invalid shape')
        if turn.get('status') == 'inProgress':
            raise ValueError('Finish the active native turn before tool migration')
        for item in turn['items']:
            if not isinstance(item, dict):
                raise ValueError('Native history item has an invalid shape')
            kind = item.get('type', 'unknown')
            if kind not in {'userMessage','agentMessage'}:
                omitted['excluded_item_types'][kind] += 1
                continue
            if turn.get('status') != 'completed':
                omitted['public_messages'] += 1
                omitted['reasons']['incomplete_turn'] += 1
                continue
            if kind == 'agentMessage':
                if not isinstance(item.get('text'), str):
                    raise ValueError('Native assistant message text must be a string')
                text_bytes = len(item['text'].encode())
            else:
                if not isinstance(item.get('content'), list):
                    raise ValueError('Native user message content must be an array')
                text_bytes = 0
                for part in item['content']:
                    if not isinstance(part, dict):
                        raise ValueError('Native user content has an invalid shape')
                    if part.get('type') == 'text':
                        if not isinstance(part.get('text'), str):
                            raise ValueError('Native user message text must be a string')
                        text_bytes += len(part['text'].encode())
            candidates.append((item, text_bytes))
    root = None
    if isinstance(cwd, str) and Path(cwd).is_absolute():
        state_root = Path(cwd).resolve()
        candidate_root = (state_root / 'attachments').resolve()
        if candidate_root.is_relative_to(state_root):
            root = candidate_root
    selected, image_provenance = [], []
    total_text, total_bytes = 0, 0
    budget_exhausted = False
    for item, text_bytes in reversed(candidates):
        if budget_exhausted or len(selected) >= MAX_MESSAGES or total_text + text_bytes > MAX_TEXT_BYTES:
            budget_exhausted = True
            omitted['public_messages'] += 1
            omitted['reasons']['history_budget'] += 1
            continue
        proof = []
        if item['type'] == 'agentMessage':
            message = {'type':'message','role':'assistant','content':[{'type':'output_text','text':item['text']}]}
            if item.get('phase') in {'commentary','final_answer'}:
                message['phase'] = item['phase']
        else:
            content = []
            for part in item['content']:
                kind = part.get('type', 'unknown')
                if kind == 'text':
                    content.append({'type':'input_text','text':part['text']})
                elif kind in {'image','localImage'}:
                    image, provenance, reason = _image(part, root, item.get('id'))
                    if image is not None:
                        content.append(image)
                        proof.append(provenance)
                    else:
                        omitted['image_parts'] += 1
                        omitted['reasons'][reason] += 1
                else:
                    omitted['other_user_parts'] += 1
                    omitted['reasons']['unsupported_user_part:' + kind] += 1
            message = {'type':'message','role':'user','content':content}
        encoded_size = len(json.dumps(message, ensure_ascii=False).encode())
        if not message['content'] or total_bytes + encoded_size > MAX_INJECT_BYTES:
            if message['content']:
                budget_exhausted = True
            omitted['public_messages'] += 1
            omitted['reasons']['empty_public_message' if not message['content'] else 'history_budget'] += 1
            continue
        selected.append(message)
        image_provenance[0:0] = proof
        total_text += text_bytes
        total_bytes += encoded_size
    omitted['reasons'] = dict(omitted['reasons'])
    omitted['excluded_item_types'] = dict(omitted['excluded_item_types'])
    return list(reversed(selected)), omitted, image_provenance


def history_export(thread, *, state, chat_id, owner_principal_id):
    """Export only host-owned public messages; never export credentials/native internals."""
    if not owner_principal_id or not isinstance(chat_id,str) or not chat_id:
        raise ValueError('Bind native history to its existing chat owner.')
    if (not isinstance(thread,dict) or not isinstance(thread.get('id'),str)
            or Path(thread.get('cwd','')).resolve()!=Path(state).resolve()
            or isinstance(thread.get('status'),dict) and thread['status'].get('type')=='active'):
        raise ValueError('Export a completed native thread from this company workspace.')
    items,omissions,images=_public_history(thread,str(Path(state).resolve()))
    document={'schema_version':1,'company_scope':workspace_scope(state),'chat_id':chat_id,
        'owner_principal_id':owner_principal_id,'source_thread_id':thread['id'],
        'items':items,'omissions':omissions,'image_provenance':images}
    document['payload_sha256']=hashlib.sha256(json.dumps(document,sort_keys=True).encode()).hexdigest()
    return document


def history_export_path(state,chat_id):
    return Path(state)/'native-migration'/(hashlib.sha256(chat_id.encode()).hexdigest()+'.json')


def load_history_export(state, *, chat_id, existing_id, owner_principal_id):
    """Accept one private host export only for the exact company/chat/owner/native binding."""
    path=history_export_path(state,chat_id)
    if not path.exists() and not path.is_symlink():return None
    try:descriptor=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
    except OSError:raise ValueError('Use the private host-exported chat history.') from None
    try:
        info=os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid!=os.geteuid()
                or info.st_mode&0o077 or info.st_size>MAX_INJECT_BYTES+1_000_000):
            raise ValueError('Use the private host-exported chat history.')
        with os.fdopen(descriptor,'r',closefd=False) as stream:data=json.load(stream)
    finally:os.close(descriptor)
    if not isinstance(data,dict):raise ValueError('Verify the native history export.')
    digest=data.get('payload_sha256')
    body={key:value for key,value in data.items() if key!='payload_sha256'}
    if (data.get('schema_version')!=1 or digest!=hashlib.sha256(json.dumps(body,sort_keys=True).encode()).hexdigest()
            or data.get('company_scope')!=workspace_scope(state) or data.get('chat_id')!=chat_id
            or not owner_principal_id or data.get('owner_principal_id')!=owner_principal_id):
        raise ValueError('Verify this company and owner before continuing exported history.')
    if data.get('source_thread_id')!=existing_id:return None  # Already continued on this host.
    items=data.get('items')
    if (not isinstance(items,list) or len(items)>MAX_MESSAGES
            or len(json.dumps(items).encode())>MAX_INJECT_BYTES
            or any(not isinstance(item,dict) or item.get('type')!='message'
                   or item.get('role') not in {'user','assistant'} for item in items)):
        raise ValueError('Use bounded public messages for native continuation.')
    return data

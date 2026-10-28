"""Historical local-thread continuation; retained until its proof callers retire."""
import asyncio
import copy

from deploy.migration.history import (
    MAX_MESSAGES, MAX_TEXT_BYTES, MAX_INJECT_BYTES, MAX_IMAGE_BYTES,
    _thread, _public_history, history_export, history_export_path, load_history_export,
)


async def open_native_thread(connection, *, existing_id, known_toolset, toolset_digest, start_params,
                             migration_export=None):
    """Open current tools, or copy bounded public context into a new native thread.

    Registry changes belong to the caller, after this function returns. Propagate
    RPC failures; an unsuccessful injection never returns a replacement thread.
    Surface `omissions` to the caller/model when nonzero. The original thread is
    retained unchanged, including tools, images and private/native history.
    """
    if not isinstance(toolset_digest, str) or not toolset_digest:
        raise ValueError('Provide the current native toolset digest')
    params = copy.deepcopy(start_params)
    if not isinstance(params.get('dynamicTools'), list):
        raise ValueError('Provide the complete current dynamic tool definitions')
    empty = {'migrated_from':None, 'copied_messages':0, 'omissions':{}, 'image_provenance':[]}
    if not existing_id:
        return {'thread':_thread(await connection.rpc('thread/start', params)), **empty}
    if migration_export is not None:
        if migration_export.get('source_thread_id')!=existing_id:
            raise ValueError('Continue the exported native thread binding.')
        new=_thread(await connection.rpc('thread/start',params))
        if new['id']==existing_id:raise ValueError('Migration requires a distinct native thread.')
        items=migration_export['items']
        if items:await connection.rpc('thread/inject_items',{'threadId':new['id'],'items':items})
        return {'thread':new,'migrated_from':existing_id,'copied_messages':len(items),
            'omissions':migration_export['omissions'],'image_provenance':migration_export['image_provenance'],
            'migration_export_sha256':migration_export['payload_sha256']}
    if known_toolset == toolset_digest:
        resume = {'threadId':existing_id}
        resume.update({key:params[key] for key in ('baseInstructions','developerInstructions','config',
            'cwd','sandbox','approvalPolicy','model','modelProvider') if key in params})
        return {'thread':_thread(await connection.rpc('thread/resume', resume)), **empty}
    old = _thread(await connection.rpc('thread/read', {'threadId':existing_id, 'includeTurns':True}))
    if old['id'] != existing_id:
        raise ValueError('Native history belongs to a different thread')
    if isinstance(old.get('status'), dict) and old['status'].get('type') == 'active':
        raise ValueError('Finish the active native turn before tool migration')
    items, omissions, images = await asyncio.to_thread(_public_history, old, params.get('cwd'))
    new = _thread(await connection.rpc('thread/start', params))
    if new['id'] == existing_id:
        raise ValueError('Tool migration requires a distinct native thread')
    if items:
        await connection.rpc('thread/inject_items', {'threadId':new['id'], 'items':items})
    return {'thread':new, 'migrated_from':existing_id, 'copied_messages':len(items),
            'omissions':omissions, 'image_provenance':images}

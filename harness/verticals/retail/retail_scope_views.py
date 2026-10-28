"""Immutable retail index projections from admitted business scope."""
import copy
from business_scope import need, ids, identifier

def scoped_index_view(index,effective):
    """Narrow an ALREADY owner/Ready-filtered index; this never grants ownership.

    Bind the returned copy through ReadyIndex.bind and RETRIEVAL_VIEW. Concrete
    Qdrant searches retain the same collection and intersect its permitted IDs.
    Every subsequent tool/media read must refresh the effective scope first.
    """
    need(isinstance(effective,dict) and isinstance(effective.get('source_ids'),list))
    allowed=set(ids(effective['source_ids'],250000));collection=identifier(effective.get('collection'))
    positions=[i for i,row in enumerate(index.clips) if effective.get('can_query') is True
        and row.get('dataset')==collection and (row.get('video_id') in allowed or effective.get('includes_unassigned') is True)]
    view=selected_index_view(index,positions)
    view.business_scope=copy.deepcopy(effective)
    return view


def selected_index_view(index,positions):
    """Copy already-admitted row positions and rebuild the aligned public index."""
    from source_visibility import _report,_read_only
    import numpy as np
    view=copy.copy(index);view.clips=[copy.deepcopy(index.clips[i]) for i in positions]
    if hasattr(index,'embeddings'):
        matrix=np.asarray(index.embeddings);need(matrix.ndim==2 and len(matrix)==len(index.clips))
        view.embeddings=matrix[positions].copy();view.embeddings.flags.writeable=False
    view.report=_report(view.clips,getattr(index,'report',{}))
    if hasattr(index,'_encode_text'):view._encode_text=index._encode_text
    view.refresh=lambda:False;view._reload=_read_only;view._publish_loaded=_read_only;view.build=_read_only
    return view


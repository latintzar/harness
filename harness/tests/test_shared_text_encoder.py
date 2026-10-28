"""A new company shares model compute while retaining its own source index."""
from pathlib import Path
import sys

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from cosmos_retrieval import CosmosRetrievalIndex


def test_company_can_query_shared_encoder_without_receiving_sources(tmp_path):
    source = CosmosRetrievalIndex(tmp_path/'models')
    company = CosmosRetrievalIndex(tmp_path/'company')
    source.clips = [{'id': 'private-source'}]
    company.clips = [{'id': 'company-source'}]
    calls = []
    def encode(query):
        calls.append(query)
        return np.eye(1, 256)[0]
    source._encode_text = encode
    source._encoder_ready = lambda: True
    source._encoder_verified = lambda: True
    company.use_text_encoder(source)
    assert company._encoder_ready() and company._encoder_verified()
    assert company._encode_text('delivery').shape == (256,)
    assert calls == ['delivery']
    assert company.clips == [{'id': 'company-source'}]
    assert company.state == tmp_path/'company'
    assert company._model is None
    assert not (company.state/'text-encoder.pt').exists()


def test_shared_encoder_preserves_unready_state_and_rejects_cycles(tmp_path):
    source = CosmosRetrievalIndex(tmp_path/'models')
    company = CosmosRetrievalIndex(tmp_path/'company')
    company.use_text_encoder(source)
    assert not company._encoder_ready() and not company._encoder_verified()
    with pytest.raises(ValueError): source.use_text_encoder(company)
    with pytest.raises(ValueError): company.use_text_encoder(object())


def test_shared_encoder_keeps_one_host_model_owner(tmp_path):
    source = CosmosRetrievalIndex(tmp_path/'models')
    first = CosmosRetrievalIndex(tmp_path/'one')
    second = CosmosRetrievalIndex(tmp_path/'two')
    first.use_text_encoder(source)
    second.use_text_encoder(first)
    assert second._text_encoder_source is source

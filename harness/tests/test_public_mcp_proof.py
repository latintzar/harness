import importlib.util
import json
from pathlib import Path

import httpx
import pytest

path=Path(__file__).parents[1]/'deploy/mcp/prove_public.py'
spec=importlib.util.spec_from_file_location('public_mcp_proof',path);proof=importlib.util.module_from_spec(spec);spec.loader.exec_module(proof)


@pytest.fixture
def anyio_backend():return 'asyncio'


def test_private_proof_input_is_owned_exact_endpoint_and_not_followed(tmp_path):
    path=tmp_path/'private.json';record={'url':'https://video-demo.soyrocoto.com/mcp','token':'rmcp_'+'x'*64}
    path.write_text(json.dumps(record));path.chmod(0o600)
    assert proof.credential(path,'https://video-demo.soyrocoto.com')==record
    path.chmod(0o644)
    with pytest.raises(proof.ProofError):proof.credential(path,'https://video-demo.soyrocoto.com')
    path.chmod(0o600);linked=tmp_path/'linked';linked.symlink_to(path)
    with pytest.raises(OSError):proof.credential(linked,'https://video-demo.soyrocoto.com')
    with pytest.raises(proof.ProofError):proof.credential(path,'https://other.soyrocoto.com')
    with pytest.raises(proof.ProofError):proof.credential(path,'https://not-rocoto.invalid')


@pytest.mark.anyio
async def test_proof_transport_rejects_redirect_before_sending_key():
    transport=proof.ExactEndpoint('https://video-demo.soyrocoto.com/mcp')
    try:
        for url in ('https://external.invalid/mcp','https://video-demo.soyrocoto.com/other'):
            with pytest.raises(proof.ProofError):await transport.handle_async_request(httpx.Request('POST',url))
    finally:await transport.aclose()


def test_saved_proof_is_private_and_complete(tmp_path):
    target=tmp_path/'proof.json';proof.save(target,{'status':'passed'})
    assert target.stat().st_mode&0o777==0o600 and json.loads(target.read_text())=={'status':'passed'}
    assert list(tmp_path.iterdir())==[target]

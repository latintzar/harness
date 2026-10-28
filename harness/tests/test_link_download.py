import asyncio
from pathlib import Path
import sys
import os
import pytest
HERE=Path(__file__).parents[1];BASE=Path(os.environ.get('ROCOTO_SOURCE_TEST_BASE',HERE))
sys.path[:0]=[str(HERE),str(BASE),str(BASE/'tests')]
from link_download import PublicDownload,LinkError,metadata,media_magic
from custom_http import HTTPGatewayError
from link_tls import tls_server

@pytest.fixture
def anyio_backend():return'asyncio'
async def dns(host):return('93.184.216.34',)

@pytest.mark.anyio
async def test_real_tls_stream_pins_dns_sni_host_ignores_proxies(tmp_path,monkeypatch):
 monkeypatch.setenv('HTTPS_PROXY','http://127.0.0.1:1')
 async with tls_server(tmp_path)as(server,context,network):
  transport=PublicDownload(resolver=dns,network_backend=network,ssl_context=context)
  async with transport.open('https://inventory.example.com/clip?signature=secret',current=lambda:True)as result:
   chunks=[v async for v in result.chunks()]
  assert b''.join(chunks)==server['body']and media_magic(chunks[0])
  assert network.calls==[('93.184.216.34',443)]and server['sni']==['inventory.example.com']
  assert b'host: inventory.example.com'in server['requests'][0]
  assert b'cookie:'not in server['requests'][0]and b'authorization:'not in server['requests'][0]
  assert result.metadata['filename']=='Shared video.mp4'

@pytest.mark.anyio
@pytest.mark.parametrize('address',['127.0.0.1','10.0.0.1','169.254.169.254','::1','::ffff:10.0.0.1'])
async def test_dns_rebinding_private_target_is_rejected_before_connect(address):
 async def resolve(host):return[address]
 transport=PublicDownload(resolver=resolve)
 with pytest.raises(HTTPGatewayError):
  async with transport.open('https://public.example.com/clip.mp4',current=lambda:True):raise AssertionError

@pytest.mark.anyio
async def test_redirect_re_resolves_and_rejects_private_target_before_second_connect(tmp_path):
 async with tls_server(tmp_path)as(server,context,network):
  server['status']=302;server['headers']=[('Location','https://private.example.com/clip.mp4?token=secret')]
  seen=[]
  async def resolve(host):seen.append(host);return['93.184.216.34'if host=='inventory.example.com'else'127.0.0.1']
  transport=PublicDownload(resolver=resolve,network_backend=network,ssl_context=context)
  with pytest.raises(HTTPGatewayError):
   async with transport.open('https://inventory.example.com/link',current=lambda:True):raise AssertionError
  assert seen==['inventory.example.com','private.example.com']and len(network.calls)==1

@pytest.mark.anyio
async def test_redirect_loop_has_finite_hops_and_no_cookie_forwarding(tmp_path):
 async with tls_server(tmp_path)as(server,context,network):
  server['status']=302;server['headers']=[('Location','/next'),('Set-Cookie','credential=private')]
  transport=PublicDownload(resolver=dns,network_backend=network,ssl_context=context)
  with pytest.raises(LinkError,match='browser_download_required'):
   async with transport.open('https://inventory.example.com/link',current=lambda:True):raise AssertionError
  assert len(network.calls)==6 and all(b'cookie:'not in value.lower()for value in server['requests'])

@pytest.mark.anyio
@pytest.mark.parametrize('status,kind,code',[(403,'video/mp4','access_required'),(200,'text/html','video_file_required'),(200,'application/zip','video_file_required'),(500,'video/mp4','download_interrupted')])
async def test_provider_access_html_and_archives_are_actionable_not_media(tmp_path,status,kind,code):
 async with tls_server(tmp_path)as(server,context,network):
  server['status']=status;server['content_type']=kind
  transport=PublicDownload(resolver=dns,network_backend=network,ssl_context=context)
  with pytest.raises(LinkError,match=code):
   async with transport.open('https://inventory.example.com/link',current=lambda:True):raise AssertionError

@pytest.mark.anyio
async def test_tls_host_mismatch_and_declared_size_limits(tmp_path):
 async with tls_server(tmp_path,certificate_host='other.example.com')as(server,context,network):
  transport=PublicDownload(resolver=dns,network_backend=network,ssl_context=context)
  with pytest.raises(Exception):
   async with transport.open('https://inventory.example.com/link',current=lambda:True):raise AssertionError
  assert not server['requests']
 for headers,code in [([(b'content-length',b'268435457')],'file_size_limit'),([],'size_required'),
  ([(b'content-length',b'5'),(b'content-encoding',b'gzip')],'video_file_required'),
  ([(b'content-length',b'5'),(b'content-length',b'7')],'video_file_required')]:
  with pytest.raises(LinkError,match=code):metadata(headers,'https://host.example.com/a',256*1024**2)

@pytest.mark.anyio
async def test_stop_before_application_bytes_and_cancel_pending_response(tmp_path):
 async with tls_server(tmp_path)as(server,context,network):
  transport=PublicDownload(resolver=dns,network_backend=network,ssl_context=context)
  with pytest.raises(LinkError,match='acquisition_stopped'):
   async with transport.open('https://inventory.example.com/link',current=lambda:False):raise AssertionError
  assert not network.calls
  server['release'].clear()
  async def read():
   async with transport.open('https://inventory.example.com/link',current=lambda:True)as result:return[v async for v in result.chunks()]
  task=asyncio.create_task(read());await server['entered'].wait();task.cancel()
  with pytest.raises(asyncio.CancelledError):await task

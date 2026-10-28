import asyncio
import base64
import json
import ssl
import threading
from datetime import datetime,timedelta,timezone
from pathlib import Path
from contextlib import asynccontextmanager
import sys
import pytest
import httpx
from cryptography import x509
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives import hashes,serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from httpcore import AnyIOBackend
sys.path.insert(0,str(Path(__file__).parents[1]))





@pytest.fixture
def anyio_backend():return 'asyncio'

TOKEN='synthetic-private-customer-token'

class Network:
 def __init__(self,port):self.port=port;self.calls=[];self.base=AnyIOBackend()
 async def connect_tcp(self,host,port,**kwargs):
  self.calls.append((host,port));assert host=='93.184.216.34'and port==443
  return await self.base.connect_tcp('127.0.0.1',self.port,**kwargs)

@asynccontextmanager
async def tls_server(tmp_path,*,certificate_host='inventory.example.com'):
 key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
 subject=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,certificate_host)])
 cert=(x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
  .serial_number(x509.random_serial_number()).not_valid_before(datetime.now(timezone.utc)-timedelta(days=1))
  .not_valid_after(datetime.now(timezone.utc)+timedelta(days=1)).add_extension(x509.SubjectAlternativeName([x509.DNSName(certificate_host)]),critical=False).sign(key,hashes.SHA256()))
 cert_path=tmp_path/'cert.pem';key_path=tmp_path/'key.pem'
 cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM));key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()));key_path.chmod(0o600)
 server_ssl=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);server_ssl.load_cert_chain(cert_path,key_path)
 state={'requests':[],'sni':[],'body':b'\x00\x00\x00\x18ftypisom'+b'x'*4000,'status':200,'headers':[], 'entered':asyncio.Event(),'release':asyncio.Event(),'tasks':set()};state['release'].set()
 server_ssl.set_servername_callback(lambda sock,name,ctx:state['sni'].append(name))
 async def handler(reader,writer):
  state['tasks'].add(asyncio.current_task())
  try:
   head=await reader.readuntil(b'\r\n\r\n');state['requests'].append(head);state['entered'].set();await state['release'].wait()
   body=state['body'];headers=[('Content-Type',state.get('content_type','video/mp4')),('Content-Length',str(state.get('content_length',len(body)))),('Connection','close'),*state['headers']]
   wire=f"HTTP/1.1 {state['status']} Test\r\n"+''.join(f'{k}: {v}\r\n'for k,v in headers)+'\r\n'
   writer.write(wire.encode()+ (b''if head.startswith(b'HEAD ')else body));await writer.drain()
  except (asyncio.CancelledError,ConnectionError,asyncio.IncompleteReadError):pass
  finally:
   writer.close()
   try:await writer.wait_closed()
   except (ConnectionError,ssl.SSLError):pass
   state['tasks'].discard(asyncio.current_task())
 server=await asyncio.start_server(handler,'127.0.0.1',0,ssl=server_ssl)
 client_ssl=ssl.create_default_context(cafile=str(cert_path));network=Network(server.sockets[0].getsockname()[1])
 try:yield state,client_ssl,network
 finally:
  state['release'].set();server.close();await server.wait_closed()
  for task in tuple(state['tasks']):task.cancel()
  await asyncio.gather(*tuple(state['tasks']),return_exceptions=True)


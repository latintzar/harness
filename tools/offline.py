"""Offline first-result boundary; recipient-connected service deployment is separate."""
import ipaddress,socket,threading
_counts={'blocked_external_attempts':0};_lock=threading.Lock()
def counts():return dict(_counts)
def _allow(host):
 if host in {None,'','localhost','testserver'}:return
 try:
  if ipaddress.ip_address(str(host).split('%')[0]).is_loopback:return
 except ValueError:pass
 with _lock:_counts['blocked_external_attempts']+=1
 raise OSError('The local sample uses loopback services.')
def install():
 connect=socket.socket.connect;resolve=socket.getaddrinfo;sendto=socket.socket.sendto
 def local_connect(s,address):
  if isinstance(address,tuple):_allow(address[0])
  return connect(s,address)
 def local_resolve(host,*args,**kwargs):_allow(host);return resolve(host,*args,**kwargs)
 def local_sendto(s,data,*args):
  address=args[-1]
  if isinstance(address,tuple):_allow(address[0])
  return sendto(s,data,*args)
 socket.socket.connect=local_connect;socket.getaddrinfo=local_resolve;socket.socket.sendto=local_sendto

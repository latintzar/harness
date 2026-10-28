"""Strict public HTTPS URL syntax; DNS admission remains at connection time."""
import ipaddress
import re
from urllib.parse import urlsplit,urlunsplit
from custom_http import validated_addresses

MAX_URL=8192

def require(value):
 if not value:raise ValueError('invalid_link')

def validate_url(value):
 require(isinstance(value,str)and 0<len(value.encode())<=MAX_URL and value==value.strip())
 require(not any(ord(c)<33 or ord(c)==127 for c in value)and '\\'not in value)
 try:
  url=urlsplit(value);host=url.hostname;port=url.port
  require(url.scheme=='https'and host and url.username is None and url.password is None and port in(None,443)and not url.fragment)
  require(host==host.encode('idna').decode('ascii') and not host.endswith('.') and '%'not in host)
  try:validated_addresses([str(ipaddress.ip_address(host))])
  except ValueError:
   require(not re.fullmatch(r'[0-9.]+',host)and ':'not in host and re.fullmatch(r'[a-zA-Z0-9.-]+',host)and '.'in host)
 except (ValueError,UnicodeError):raise ValueError('invalid_link')from None
 return urlunsplit(('https',host.lower(),url.path or '/',url.query,''))


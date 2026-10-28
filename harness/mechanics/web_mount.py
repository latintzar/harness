"""Mount the real workspace and Orders HTTP service below one deployment path.

This adapter changes server-side URLs and cookie paths. Browser requests still reach
native handlers; session admission, data, mutations and pricing remain server-owned.
"""
from __future__ import annotations
import json,re
from urllib.parse import urlsplit
import httpx

MAX_RESPONSE=16*1024*1024
SAFE_QUOTE=re.compile(r'/[a-z0-9-]+/rocoto/api/quote\Z')

def base_path(value=''):
    if value in {'','/'}:return ''
    if not isinstance(value,str) or not re.fullmatch(r'/(?:[a-zA-Z0-9_-]+/)*[a-zA-Z0-9_-]+/?',value):
        raise ValueError('Use a slash-separated application base path.')
    return value.rstrip('/')

class MountedApp:
    def __init__(self,app,*,prefix='',orders_origin=None,company=None,read_only=False,public_origin=''):
        self.app=app;self.state=app.state;self.prefix=base_path(prefix);self.orders=orders_origin.rstrip('/') if orders_origin else None;self.company=company;self.read_only=read_only;self.public_origin=public_origin.rstrip('/')
        parsed=urlsplit(self.orders) if self.orders else None
        if parsed and (parsed.scheme!='http' or parsed.hostname not in {'127.0.0.1','localhost','::1'} or parsed.path or parsed.query or parsed.fragment or parsed.username):
            raise ValueError('The mounted Orders transport uses an owned loopback HTTP service.')
    def path(self,path):return self.prefix+path
    def origin(self,scope):
        host=dict(scope.get('headers',[])).get(b'host',b'localhost').decode('latin-1')
        return self.public_origin or scope.get('scheme','http')+'://'+host
    def text(self,text,origin=''):
        # Only the deployed URL prefixes change. No browser fetch implementation is replaced.
        text=text.replace(self.orders,origin+self.path('/orders')) if self.orders else text
        text=re.sub(r'''(["'`(])(/(?:api|static|auth|vendor|connection-brands|fonts|brand|assets|control-room|attachments)/)''',lambda m:m[1]+self.prefix+m[2],text)
        text=re.sub(r'''(["'`(])(/[^/\s"'`()?#]+\.(?:css|js|mjs|woff2|woff|svg|png|jpg|jpeg|webp)(?:\?[^\s"'`()#]*)?)''',lambda m:m[1]+self.prefix+m[2],text)
        # Native page routes keep their application root when deployed in a shell.
        text=re.sub(r'''(["'`])(/\?)''',lambda m:m[1]+self.prefix+m[2],text)
        text=re.sub(r'''(new\s+URL\(\s*["'])/(["']\s*,\s*(?:window\.)?location\.href)''',lambda m:m[1]+self.prefix+'/'+m[2],text)
        text=re.sub(r'''(\bhistory\.(?:replaceState|pushState)\([^;\n]*?,\s*["'])/(["']\s*\))''',lambda m:m[1]+self.prefix+'/'+m[2],text)
        if self.company:
            text=re.sub(r'''(["'`(])(/'''+re.escape(self.company)+r''')(?=/|["'`)?#\s]|$)''',lambda m:m[1]+self.path('/orders')+m[2],text)
        if '<!doctype' in text.lower():
            def attribute(match):
                path=match[2]
                return match[0] if self.prefix and (path==self.prefix or path.startswith(self.prefix+'/')) else match[1]+self.prefix+path
            text=re.sub(r'''((?:src|href|action)=["'])(/(?!/)[^"']*)''',attribute,text)
        return text
    def value(self,value,origin=''):
        if isinstance(value,dict):return {k:self.value(v,origin) for k,v in value.items()}
        if isinstance(value,list):return [self.value(v,origin) for v in value]
        if not isinstance(value,str):return value
        if self.orders and value.startswith(self.orders):return origin+self.path('/orders')+value[len(self.orders):]
        if self.company and value.startswith('/'+self.company+'/'):return origin+self.path('/orders')+value
        if value=='/' or value.startswith('/?'):return self.prefix+value
        if value.startswith(('/api/','/static/','/auth/','/vendor/','/connection-brands/','/fonts/','/brand/','/assets/','/control-room/','/attachments/')):return self.prefix+value
        if re.fullmatch(r'/[^/\s?#]+\.(?:css|js|mjs|woff2|woff|svg|png|jpg|jpeg|webp)(?:\?[^\s#]*)?',value):return self.prefix+value
        return value
    def unvalue(self,value,origin=''):
        if isinstance(value,dict):return {k:self.unvalue(v,origin) for k,v in value.items()}
        if isinstance(value,list):return [self.unvalue(v,origin) for v in value]
        if not isinstance(value,str):return value
        order_path=self.path('/orders')
        if self.orders and origin and value.startswith(origin+order_path+'/'):return value[len(origin+order_path):]
        if self.orders and value.startswith(order_path+'/'):return value[len(order_path):]
        if self.prefix and value.startswith(self.prefix+'/'):return value[len(self.prefix):]
        return value
    def headers(self,headers,path=''):
        result=[]
        for key,value in headers:
            name=key.lower()
            if name==b'set-cookie':
                value=re.sub(br'(?i)(;\s*path=)/(?=;|$)',lambda m:m[1]+(self.prefix+'/').encode(),value)
            elif name in {b'location',b'content-security-policy'}:
                text=value.decode('latin-1')
                if self.orders:text=text.replace(self.orders,"'self'" if name==b'content-security-policy' else self.path('/orders'))
                if name==b'location' and text.startswith('/') and not text.startswith('//') and not text.startswith(self.prefix+'/'):
                    text=self.prefix+text
                value=text.encode('latin-1')
            result.append((key,value))
        kind=dict(result).get(b'content-type',b'')
        if b'font' in kind or path.endswith(('.woff2','.woff','.ttf','.otf')):
            result=[(k,v) for k,v in result if k.lower() not in {b'access-control-allow-origin',b'cross-origin-resource-policy'}]
            result.extend([(b'access-control-allow-origin',b'*'),(b'cross-origin-resource-policy',b'cross-origin')])
        return result
    async def __call__(self,scope,receive,send):
        if scope['type']!='http':return await self.app(scope,receive,send)
        original=scope['path']
        if self.prefix and original==self.prefix:
            query=scope.get('query_string',b'');location=(self.prefix+'/').encode()+(b'?'+query if query else b'')
            await send({'type':'http.response.start','status':307,'headers':[(b'location',location)]});return await send({'type':'http.response.body','body':b''})
        if self.prefix and not original.startswith(self.prefix+'/'):
            await send({'type':'http.response.start','status':404,'headers':[]});return await send({'type':'http.response.body','body':b''})
        path=original[len(self.prefix):] if self.prefix else original
        child={**scope,'path':path,'raw_path':path.encode(),'root_path':''}
        safe_quote=bool(self.orders and path.startswith('/orders/') and scope['method']=='POST' and SAFE_QUOTE.fullmatch(path[len('/orders'):]))
        if self.read_only and scope['method'] not in {'GET','HEAD'} and not safe_quote and not (scope['method']=='DELETE' and path=='/api/session'):
            return await self.refuse(send)
        headers=dict(scope.get('headers',[]))
        if path.startswith('/api/') and scope['method'] in {'POST','PUT','PATCH'} and b'application/json' in headers.get(b'content-type',b''):
            body=bytearray()
            while True:
                message=await receive()
                if message['type']=='http.disconnect':return
                body.extend(message.get('body',b''))
                if len(body)>MAX_RESPONSE:return await self.refuse(send,413)
                if not message.get('more_body',False):break
            try:body=json.dumps(self.unvalue(json.loads(body),self.origin(scope)),ensure_ascii=False,separators=(',',':')).encode()
            except (ValueError,UnicodeError):body=bytes(body)
            child['headers']=[(k,v) for k,v in scope.get('headers',[]) if k.lower()!=b'content-length']+[(b'content-length',str(len(body)).encode())]
            original_receive=receive;sent=False
            async def normalized_receive():
                nonlocal sent
                if not sent:
                    sent=True;return {'type':'http.request','body':body,'more_body':False}
                return await original_receive()
            receive=normalized_receive
        if self.orders and path.startswith('/orders/'):
            return await self.proxy(child,receive,send,path[len('/orders'):])
        pending=None;chunks=[];size=0;rewrite=False
        async def wrapped(message):
            nonlocal pending,rewrite,size
            if message['type']=='http.response.start':
                pending={**message,'headers':self.headers(message.get('headers',[]),path)}
                kind=dict(pending['headers']).get(b'content-type',b'').split(b';')[0]
                rewrite=scope['method']!='HEAD' and kind in {b'text/html',b'text/css',b'application/javascript',b'text/javascript',b'application/json',b'image/svg+xml'}
                if not rewrite:await send(pending)
            elif message['type']=='http.response.body' and rewrite:
                chunks.append(message.get('body',b''));size+=len(chunks[-1])
                if size>MAX_RESPONSE:
                    rewrite=False;await send(pending);await send({**message,'body':b''.join(chunks)});chunks.clear()
                elif not message.get('more_body',False):
                    data=b''.join(chunks);kind=dict(pending['headers']).get(b'content-type',b'')
                    if b'application/json' in kind:
                        try:data=json.dumps(self.value(json.loads(data),self.origin(scope)),ensure_ascii=False,separators=(',',':')).encode()
                        except (ValueError,UnicodeError):pass
                    else:
                        try:data=self.text(data.decode(),self.origin(scope)).encode()
                        except UnicodeError:pass
                    pending['headers']=[(k,v) for k,v in pending['headers'] if k.lower() not in {b'content-length',b'etag'}]+[(b'content-length',str(len(data)).encode())]
                    await send(pending);await send({**message,'body':data})
            else:await send(message)
        await self.app(child,receive,wrapped)
    async def proxy(self,scope,receive,send,path):
        method=scope['method'];headers={k.decode('latin-1'):v.decode('latin-1') for k,v in scope.get('headers',[])}
        origin=headers.get('origin')
        if method not in {'GET','HEAD'} and origin and urlsplit(origin).netloc!=headers.get('host'):
            return await self.refuse(send)
        if self.read_only and method not in {'GET','HEAD'} and not(method=='POST' and SAFE_QUOTE.fullmatch(path)):
            return await self.refuse(send)
        if self.read_only and '/rocoto/api/' in path and method in {'GET','HEAD'} and not path.endswith('/rocoto/api/supply'):
            return await self.refuse(send)
        body=bytearray()
        while True:
            message=await receive()
            if message['type']=='http.disconnect':return
            body.extend(message.get('body',b''))
            if len(body)>16*1024*1024:return await self.refuse(send,413)
            if not message.get('more_body',False):break
        request_headers={k:v for k,v in headers.items() if k not in {'host','cookie','connection','content-length','transfer-encoding','authorization'}}
        if origin:request_headers['origin']=self.orders
        query=scope.get('query_string',b'').decode('ascii');target=self.orders+path+('?' +query if query else '')
        async with httpx.AsyncClient(trust_env=False,follow_redirects=False,timeout=30) as client:
            response=await client.request(method,target,headers=request_headers,content=bytes(body))
        data=response.content;kind=response.headers.get('content-type','')
        if any(x in kind for x in ['javascript','text/','json','svg']):
            try:data=self.text(data.decode(),self.origin(scope)).encode()
            except UnicodeError:pass
        result=self.headers([(k.encode('latin-1'),v.encode('latin-1')) for k,v in response.headers.items() if k.lower() not in {'content-length','connection','transfer-encoding','content-encoding','etag','set-cookie'}],path)
        result.append((b'content-length',str(len(data)).encode()))
        await send({'type':'http.response.start','status':response.status_code,'headers':result});await send({'type':'http.response.body','body':data})
    @staticmethod
    async def refuse(send,status=403):
        data=b'{"ok":false,"detail":"Use your own workspace to save changes."}'
        await send({'type':'http.response.start','status':status,'headers':[(b'content-type',b'application/json'),(b'content-length',str(len(data)).encode())]});await send({'type':'http.response.body','body':data})

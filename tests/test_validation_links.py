import socket
import pytest
from link_validation import BlockedLink,destination,check_link


def resolved(ip='93.184.216.34'):
    return lambda *a,**kw:[(socket.AF_INET,socket.SOCK_STREAM,6,'',(ip,443))]


@pytest.mark.parametrize('url',[
    'file:///etc/passwd','ftp://example.com/a','https://u:p@example.com',
    'http://127.0.0.1','http://[::1]','http://169.254.169.254',
    'http://10.1.2.3','http://192.168.1.1','http://172.16.0.1',
    'http://localhost','http://internal','http://2130706433','http://0x7f000001',
    'http://127.1','http://example.com:8000','https://%31%32%37.0.0.1',
    'https://example.com\\@127.0.0.1','https://example.com\r\nHost:x',
])
def test_unsafe_urls_fail_closed(url):
    def resolver(host,*a,**kw):
        if host in ('127.1','2130706433','0x7f000001'): return resolved('127.0.0.1')()
        return socket.getaddrinfo(host,*a,**kw)
    assert check_link(url,resolver)=='BLOCKED_SECURITY'


@pytest.mark.parametrize('ip',['127.0.0.1','10.0.0.1','169.254.169.254','::1','fc00::1','fe80::1','::ffff:127.0.0.1','2001:db8::1'])
def test_private_dns_response(ip):
    with pytest.raises(BlockedLink): destination('https://public.example',resolved(ip))


def test_mixed_public_private_dns_is_rejected():
    with pytest.raises(BlockedLink):
        destination('https://public.example',lambda *a,**kw:resolved()()+resolved('10.0.0.1')())


class Socket:
    def __init__(self,*args): self.address=None
    def settimeout(self,timeout): assert timeout==4
    def connect(self,address): self.address=address
    def close(self): pass


def fake_network(monkeypatch,status=200,location=None):
    import link_validation as module
    sockets=[]; requests=[]
    def create(*args):
        sock=Socket(); sockets.append(sock); return sock
    class Response:
        def __init__(self): self.status=status
        def getheader(self,name): return location
    class Connection:
        def __init__(self,*args,**kw): pass
        def request(self,method,path,**kw): requests.append((method,path))
        def getresponse(self): return Response()
        def close(self): pass
    monkeypatch.setattr(module.socket,'socket',create)
    monkeypatch.setattr(module.http.client,'HTTPConnection',Connection)
    return sockets,requests


def test_actual_connection_pinned_against_rebinding(monkeypatch):
    sockets,requests=fake_network(monkeypatch)
    calls=[]
    def dns(*a,**kw):
        calls.append(a)
        return resolved('93.184.216.34' if len(calls)==1 else '127.0.0.1')()
    assert check_link('http://public.example/path',dns)=='REACHABLE'
    assert len(calls)==1 and sockets[0].address==('93.184.216.34',443)
    assert requests==[('HEAD','/path')]


def test_redirect_to_private_rejected_before_connection(monkeypatch):
    sockets,_=fake_network(monkeypatch,302,'http://127.0.0.1/')
    dns=lambda host,*a,**kw:resolved('127.0.0.1' if host=='127.0.0.1' else '93.184.216.34')()
    assert check_link('http://public.example',dns)=='BLOCKED_SECURITY'
    assert len(sockets)==1


def test_redirect_loop_bounded(monkeypatch):
    sockets,_=fake_network(monkeypatch,302,'http://public.example')
    assert check_link('http://public.example',resolved())=='UNREACHABLE'
    assert len(sockets)==1


@pytest.mark.parametrize('status,outcome',[(503,'TEMPORARILY_UNAVAILABLE'),(429,'TEMPORARILY_UNAVAILABLE'),
    (404,'UNREACHABLE'),(403,'REACHABLE'),(405,'REACHABLE')])
def test_http_outcomes(monkeypatch,status,outcome):
    fake_network(monkeypatch,status)
    assert check_link('http://public.example',resolved())==outcome


def test_timeout_is_transient():
    def timeout(*a,**kw): raise TimeoutError()
    assert check_link('https://public.example',timeout)=='TEMPORARILY_UNAVAILABLE'

"""SSRF-resistant bounded reachability checks with connection address pinning."""
import http.client
import ipaddress
import socket
import ssl
from urllib.parse import urlsplit, urljoin


class BlockedLink(ValueError):
    pass


def destination(url, resolver=socket.getaddrinfo):
    try:
        if len(url)>2048 or any(ord(ch)<=32 for ch in url) or '\\' in url or '%' in urlsplit(url).netloc:
            raise BlockedLink()
        parsed = urlsplit(url)
        if (parsed.scheme not in ('https','http') or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.port not in (None,80,443)):
            raise BlockedLink()
        host = parsed.hostname.encode('idna').decode('ascii')
        if '.' not in host and ':' not in host:
            raise BlockedLink()
        port = parsed.port or (443 if parsed.scheme=='https' else 80)
        addresses = resolver(host,port,type=socket.SOCK_STREAM)
        if not addresses or len(addresses)>32:
            raise BlockedLink()
        for _,_,_,_,address in addresses:
            ip = ipaddress.ip_address(address[0])
            if not ip.is_global or (ip.version==6 and (ip.ipv4_mapped or ip.sixtofour or ip.teredo)):
                raise BlockedLink()
        return parsed,host,port,addresses[0]
    except (ValueError,UnicodeError):
        raise BlockedLink() from None


def check_link(url, resolver=socket.getaddrinfo):
    visited = set()
    for _ in range(4):
        if url in visited:
            return 'UNREACHABLE'
        visited.add(url)
        connection = None
        try:
            parsed,host,port,address = destination(url,resolver)
            family,socktype,proto,_,sockaddr = address
            # Connect to the approved numeric sockaddr directly. No second DNS
            # lookup, proxy environment, or automatic redirects are involved.
            sock = socket.socket(family,socktype,proto)
            try:
                sock.settimeout(4)
                sock.connect(sockaddr)
                if parsed.scheme=='https':
                    sock = ssl.create_default_context().wrap_socket(sock,server_hostname=host)
                connection = http.client.HTTPConnection(host,port,timeout=4)
                connection.sock = sock
                target = parsed.path or '/'
                if parsed.query:
                    target += '?'+parsed.query
                connection.request('HEAD',target,headers={'User-Agent':'DeadlineCrush/1','Connection':'close'})
                response = connection.getresponse()
                # http.client also bounds line length/header count. Never read
                # response body; HEAD is sufficient for v1 responsiveness.
                location = response.getheader('Location')
                status = response.status
                if status in (301,302,303,307,308):
                    if not location or len(location)>2048:
                        return 'INVALID'
                    url = urljoin(url,location)
                    continue
                if status in (408,425,429) or status>=500:
                    return 'TEMPORARILY_UNAVAILABLE'
                if 200<=status<400 or status in (401,403,405):
                    return 'REACHABLE'
                return 'UNREACHABLE'
            finally:
                sock.close()
        except BlockedLink:
            return 'BLOCKED_SECURITY'
        except (OSError,TimeoutError,http.client.HTTPException):
            return 'TEMPORARILY_UNAVAILABLE'
        finally:
            if connection:
                connection.close()
    return 'UNREACHABLE'

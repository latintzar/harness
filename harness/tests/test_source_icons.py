"""Real local TLS checks; external favicon acquisition never reaches the internet."""
import asyncio
import io
import sys
from pathlib import Path

from PIL import Image
import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from source_icons import SourceIcons, FALLBACK, MAX_BYTES, raster_icon
from test_custom_http import tls_server


def png(size=(24, 24)):
    out = io.BytesIO()
    Image.new('RGBA', size, (20, 70, 160, 255)).save(out, format='PNG')
    return out.getvalue()


@pytest.mark.parametrize('host', ['localhost', '127.0.0.1', '[::1]', 'example.com/path', 'example.com:8000',
                                 'user@example.com', 'example.com?token=x', 'EXAMPLE.COM', 'example.com.', ''])
def test_invalid_host_never_resolves(host):
    async def scenario():
        async def resolver(host):
            pytest.fail('Invalid hostname reached DNS')
        with pytest.raises(ValueError):
            await SourceIcons(resolver=resolver).get(host)
    asyncio.run(scenario())


def test_raster_validation_rejects_html_svg_and_large_dimensions():
    for data in (b'<html>private</html>', b'<svg onload="alert(1)"/>', png((513, 1))):
        with pytest.raises((ValueError, OSError)):
            raster_icon(data)
    assert raster_icon(png()).startswith(b'\x89PNG')


def test_icon_reencoding_discards_remote_metadata():
    original = io.BytesIO()
    Image.new('RGB', (16, 16)).save(original, format='PNG', icc_profile=b'untrusted-profile')
    with Image.open(io.BytesIO(raster_icon(original.getvalue()))) as icon:
        assert 'icc_profile' not in icon.info


def test_public_icon_pins_tls_host_strips_credentials_and_caches(tmp_path, monkeypatch):
    async def scenario():
        async with tls_server(tmp_path) as (server, tls, network):
            server['body'] = png()
            dns = []
            async def resolver(host):
                dns.append(host)
                return ['93.184.216.34']
            icons = SourceIcons(resolver=resolver, ssl_context=tls, network_backend=network)
            body, kind = await icons.get('inventory.example.com')
            assert kind == 'image/png' and body.startswith(b'\x89PNG')
            assert await icons.get('inventory.example.com') == (body, kind)
            assert dns == ['inventory.example.com'] and network.calls == [('93.184.216.34', 443)]
            request = server['requests'][0].lower()
            assert request.startswith(b'get /favicon.ico ') and b'host: inventory.example.com' in request
            assert not any(value in request for value in (b'authorization:', b'cookie:', b'referer:'))
            assert server['sni'] == ['inventory.example.com']
    monkeypatch.setenv('HTTPS_PROXY', 'http://127.0.0.1:1')
    asyncio.run(scenario())


@pytest.mark.parametrize('addresses', [['127.0.0.1'], ['169.254.169.254'], ['93.184.216.34', '10.0.0.1'], ['::ffff:127.0.0.1']])
def test_private_or_mixed_dns_returns_fallback_without_connecting(addresses):
    async def scenario():
        async def resolver(host):
            return addresses
        class ForbiddenNetwork:
            async def connect_tcp(self, *args, **kwargs):
                pytest.fail('Private DNS reached network')
        assert await SourceIcons(resolver=resolver, network_backend=ForbiddenNetwork()).get('example.com') == (FALLBACK, 'image/svg+xml')
    asyncio.run(scenario())


@pytest.mark.parametrize('case', ['private_redirect', 'oversized', 'wrong_certificate'])
def test_redirect_body_and_certificate_boundaries(tmp_path, case):
    async def scenario():
        async with tls_server(tmp_path, certificate_host='other.example.com' if case == 'wrong_certificate' else 'inventory.example.com') as (server, tls, network):
            server['body'] = png()
            if case == 'private_redirect':
                server['status'] = 302
                server['headers'] = [('Location', 'https://127.0.0.1/private')]
            if case == 'oversized':
                server['body'] = png() + b'x' * MAX_BYTES
            async def resolver(host):
                assert host == 'inventory.example.com'
                return ['93.184.216.34']
            assert await SourceIcons(resolver=resolver, network_backend=network, ssl_context=tls).get('inventory.example.com') == (FALLBACK, 'image/svg+xml')
            assert len(network.calls) == 1
    asyncio.run(scenario())

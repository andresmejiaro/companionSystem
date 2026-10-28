import pytest
from test_mcp_server import _mcp_client, _rpc, _bearer


@pytest.mark.parametrize('version', ['2025-03-26', '2025-06-18', '2025-11-25'])
def test_version_negotiation_and_tools(version):
    with _mcp_client() as client:
        headers = {**_bearer(), 'MCP-Protocol-Version': version}
        r = client.post('/mcp', json=_rpc('initialize', {
            'protocolVersion': version, 'capabilities': {},
            'clientInfo': {'name': 'compatibility-test', 'version': '1'},
        }), headers=headers)
        assert r.status_code == 200
        assert r.json()['result']['protocolVersion'] == version
        assert r.headers['mcp-protocol-version'] == version
        r = client.post('/mcp', json={'jsonrpc': '2.0', 'method': 'notifications/initialized'}, headers=headers)
        assert r.status_code == 202
        r = client.post('/mcp', json=_rpc('tools/list'), headers=headers)
        assert r.status_code == 200
        assert any(t['name'] == 'summon_companion' for t in r.json()['result']['tools'])
        assert r.headers['mcp-protocol-version'] == version
        r = client.get('/mcp', headers={**headers, 'Accept': 'text/event-stream'})
        assert r.status_code == 200
        assert r.headers['mcp-protocol-version'] == version


def test_initialize_can_negotiate_an_unsupported_header():
    with _mcp_client() as client:
        headers = {**_bearer(), 'MCP-Protocol-Version': '2099-01-01'}
        r = client.post('/mcp', json=_rpc('initialize', {'protocolVersion': '2099-01-01'}), headers=headers)
        assert r.status_code == 200
        assert r.json()['result']['protocolVersion'] == '2025-06-18'
        assert r.headers['mcp-protocol-version'] == '2025-06-18'
        # Unknown versions remain invalid after initialization.
        assert client.post('/mcp', json=_rpc('tools/list'), headers=headers).status_code == 400
        assert client.get('/mcp', headers=headers).status_code == 400


@pytest.mark.parametrize('method', ['GET', 'POST'])
def test_new_version_still_requires_authentication(method):
    with _mcp_client() as client:
        r = client.request(method, '/mcp', headers={'MCP-Protocol-Version': '2025-11-25'},
                           **({'json': _rpc('initialize')} if method == 'POST' else {}))
        assert r.status_code == 401


def test_rejection_logs_version_without_credentials(caplog):
    with _mcp_client() as client:
        r = client.get('/mcp', headers={**_bearer(), 'MCP-Protocol-Version': '2099-01-01'})
        assert r.status_code == 400
    assert '2099-01-01' in caplog.text
    assert _bearer()['Authorization'] not in caplog.text

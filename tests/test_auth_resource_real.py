"""HTTP authentication regressions against the installed SDK, isolated from stubs."""
import subprocess
import sys

import pytest


@pytest.mark.parametrize("resource", ["configured", None, "https://other.example/mcp"])
def test_static_token_resource_over_real_http(resource):
    script = r'''
import asyncio
import httpx
from dataclasses import replace
from config import load_config
from tests.fake_backend import FakeBackend
import server

cfg = replace(load_config(), server_url="https://example.com", api_key="test-bearer-key")
server.get_backend = lambda cfg: FakeBackend()
mcp = server.create_app(cfg)
mcp.settings.stateless_http = True
mcp.settings.json_response = True
resource = RESOURCE
if resource != "configured":
    verifier = mcp._token_verifier
    original = verifier.verify_token
    async def changed(token):
        access = await original(token)
        return access.model_copy(update={"resource": resource}) if access else None
    verifier.verify_token = changed
app = mcp.streamable_http_app()

async def main():
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url=cfg.server_url) as client:
            response = await client.post("/mcp", headers={
                "Authorization": "Bearer test-bearer-key",
                "Accept": "application/json, text/event-stream",
            }, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-03-26", "capabilities": {},
                "clientInfo": {"name": "resource-test", "version": "1"}}})
            expected = 200 if resource == "configured" else 401
            assert response.status_code == expected, (response.status_code, response.text)
            if expected == 200:
                assert "result" in response.json(), response.text
asyncio.run(main())
'''.replace("RESOURCE", repr(resource))
    result = subprocess.run([sys.executable, "-W", "error::DeprecationWarning", "-c", script],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr + result.stdout

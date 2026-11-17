"""OAUTH_PASSWORD gate at /authorize (audit E1).

Uses the stubbing approach of test_auth.py: the ``mcp.*`` modules are
replaced with minimal fakes before importing ``oauth_provider``, and the
gate is exercised as a plain ASGI application with hand-rolled scopes.
The real-SDK subprocess test at the bottom verifies the same wiring
against the installed mcp package without stubs.
"""

import asyncio
import importlib
import importlib.metadata
import subprocess
import sys
import types
import unittest
import urllib.parse
from pathlib import Path

from starlette.requests import Request
from starlette.responses import RedirectResponse


AUTHORIZE_PARAMS = {
    "client_id": "claude-client",
    "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
    "response_type": "code",
    "code_challenge": "challenge-4711",
    "code_challenge_method": "S256",
    "scope": "claudeai",
    "resource": "https://example.com/mcp",
}


def install_mcp_auth_stubs():
    provider = types.ModuleType("mcp.server.auth.provider")

    def kwargs_class(name):
        return type(name, (), {
            "__init__": lambda self, **kwargs: self.__dict__.update(kwargs),
        })

    provider.AccessToken = kwargs_class("AccessToken")
    provider.AuthorizationCode = kwargs_class("AuthorizationCode")
    provider.RefreshToken = kwargs_class("RefreshToken")
    provider.AuthorizationParams = kwargs_class("AuthorizationParams")

    class OAuthAuthorizationServerProvider:
        def __class_getitem__(cls, item):
            return cls

    provider.OAuthAuthorizationServerProvider = OAuthAuthorizationServerProvider

    def construct_redirect_uri(base, **params):
        extra = urllib.parse.urlencode(
            {k: v for k, v in params.items() if v is not None}
        )
        return f"{base}?{extra}" if extra else base

    provider.construct_redirect_uri = construct_redirect_uri

    shared = types.ModuleType("mcp.shared.auth")
    shared.OAuthClientInformationFull = kwargs_class(
        "OAuthClientInformationFull"
    )
    shared.OAuthToken = kwargs_class("OAuthToken")

    sys.modules["mcp"] = types.ModuleType("mcp")
    sys.modules["mcp.server"] = types.ModuleType("mcp.server")
    sys.modules["mcp.server.auth"] = types.ModuleType("mcp.server.auth")
    sys.modules["mcp.server.auth.provider"] = provider
    sys.modules["mcp.shared"] = types.ModuleType("mcp.shared")
    sys.modules["mcp.shared.auth"] = shared


def make_provider(password="gate-pass-7"):
    oauth_provider = importlib.import_module("oauth_provider")
    return oauth_provider.SimpleOAuthProvider(
        server_url="https://example.com",
        access_password=password,
        client_id="claude-client",
        client_secret="client-secret",
    )


def make_sdk_like_endpoint(provider):
    """Mimic the tail of the SDK /authorize handler: validate nothing,
    issue a code via provider.authorize() and redirect."""

    async def endpoint(scope, receive, send):
        request = Request(scope, receive)
        if request.method == "GET":
            params = request.query_params
        else:
            params = await request.form()
        client = await provider.get_client(params.get("client_id", ""))
        auth_params = types.SimpleNamespace(
            state=params.get("state"),
            scopes=[params.get("scope") or "claudeai"],
            code_challenge=params.get("code_challenge", ""),
            redirect_uri=params.get("redirect_uri"),
            redirect_uri_provided_explicitly=True,
            resource=None,
        )
        url = await provider.authorize(client, auth_params)
        await RedirectResponse(url, status_code=302)(scope, receive, send)

    return endpoint


def make_scope(method, query_string=b"", body=b""):
    headers = []
    if body:
        headers.append((b"content-type", b"application/x-www-form-urlencoded"))
        headers.append((b"content-length", str(len(body)).encode()))
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "https",
        "path": "/authorize",
        "raw_path": b"/authorize",
        "query_string": query_string,
        "root_path": "",
        "server": ("example.com", 443),
        "client": ("203.0.113.4", 54321),
        "headers": headers,
    }


async def drive(app, scope, body=b""):
    pending = [{"type": "http.request", "body": body, "more_body": False}]
    sent = []

    async def receive():
        return pending.pop(0) if pending else {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    await app(scope, receive, send)
    status = sent[0]["status"]
    headers = {
        key.decode(): value.decode()
        for key, value in sent[0].get("headers", [])
    }
    payload = b"".join(
        message.get("body", b"")
        for message in sent
        if message["type"] == "http.response.body"
    )
    return status, headers, payload.decode("utf-8", "replace")


class PasswordGateTests(unittest.TestCase):
    def setUp(self):
        install_mcp_auth_stubs()
        sys.modules.pop("oauth_provider", None)
        self.oauth_provider = importlib.import_module("oauth_provider")
        self.provider = make_provider()
        self.calls = []

        async def recording_inner(scope, receive, send):
            self.calls.append(scope)
            await make_sdk_like_endpoint(self.provider)(scope, receive, send)

        self.gate = self.oauth_provider.AuthorizePasswordGate(
            self.provider, recording_inner
        )

    def test_many_small_body_chunks_and_midstream_limit(self):
        import time

        class StreamingRequest:
            headers = {}
            consumed = 0

            async def stream(self):
                for _ in range(32768):
                    self.consumed += 1
                    yield b"x" * 256

        self.gate.max_body_bytes = 32768 * 256
        request = StreamingRequest()
        start = time.monotonic()
        body = asyncio.run(self.gate._read_limited_body(request))
        elapsed = time.monotonic() - start
        self.assertEqual(body, b"x" * self.gate.max_body_bytes)
        self.assertIsInstance(body, bytes)
        self.assertLess(elapsed, 2.0)

        self.gate.max_body_bytes = 512
        request = StreamingRequest()
        self.assertIsNone(asyncio.run(self.gate._read_limited_body(request)))
        self.assertEqual(request.consumed, 3)

    def test_get_returns_password_form_with_original_params(self):
        query = urllib.parse.urlencode({**AUTHORIZE_PARAMS, "state": "st'ate <x>"})

        status, headers, body = asyncio.run(
            drive(self.gate, make_scope("GET", query.encode()))
        )

        self.assertEqual(status, 200)
        self.assertTrue(headers["content-type"].startswith("text/html"))
        self.assertIn('method="POST"', body)
        self.assertIn('action="/authorize"', body)
        for name, value in AUTHORIZE_PARAMS.items():
            self.assertIn(f'name="{name}" value="{value}"', body)
        # HTML-escaped, attacker-controllable values must not break out.
        self.assertIn('name="state" value="st&#x27;ate &lt;x&gt;"', body)
        self.assertIn('type="password" name="password"', body)
        self.assertEqual(self.calls, [])

    def test_get_drops_password_query_param_from_hidden_fields(self):
        query = urllib.parse.urlencode(
            {**AUTHORIZE_PARAMS, "state": "s1", "password": "hax"}
        )

        status, _, body = asyncio.run(
            drive(self.gate, make_scope("GET", query.encode()))
        )

        self.assertEqual(status, 200)
        self.assertNotIn('<input type="hidden" name="password"', body)
        self.assertNotIn("hax", body)

    def test_post_wrong_password_returns_401_without_echo(self):
        body_bytes = urllib.parse.urlencode(
            {"password": "not-the-pass", **AUTHORIZE_PARAMS, "state": "s2"}
        ).encode()

        status, _, body = asyncio.run(
            drive(self.gate, make_scope("POST", body=body_bytes), body=body_bytes)
        )

        self.assertEqual(status, 401)
        self.assertIn('action="/authorize"', body)
        self.assertIn('name="client_id" value="claude-client"', body)
        self.assertNotIn("not-the-pass", body)
        self.assertEqual(self.provider.auth_codes, {})
        self.assertEqual(self.calls, [])

    def test_post_correct_password_issues_code_and_redirects(self):
        body_bytes = urllib.parse.urlencode(
            {"password": "gate-pass-7", **AUTHORIZE_PARAMS, "state": "s3"}
        ).encode()

        status, headers, _ = asyncio.run(
            drive(self.gate, make_scope("POST", body=body_bytes), body=body_bytes)
        )

        self.assertEqual(status, 302)
        location = headers["location"]
        self.assertIn("code=", location)
        self.assertIn("state=s3", location)
        self.assertEqual(len(self.provider.auth_codes), 1)
        self.assertEqual(len(self.calls), 1)

    def test_empty_password_keeps_passwordless_passthrough(self):
        provider = make_provider(password="")
        gate = self.oauth_provider.AuthorizePasswordGate(
            provider, make_sdk_like_endpoint(provider)
        )
        query = urllib.parse.urlencode({**AUTHORIZE_PARAMS, "state": "s4"})

        status, headers, body = asyncio.run(
            drive(gate, make_scope("GET", query.encode()))
        )

        self.assertEqual(status, 302)
        self.assertIn("code=", headers["location"])
        self.assertNotIn("<form", body)

    def test_head_and_other_methods_rejected_before_handler(self):
        # Starlette auto-permits HEAD on GET routes and the SDK parses
        # form data for every non-GET method, so a HEAD carrying valid
        # OAuth fields would otherwise obtain a code without the password.
        body_bytes = urllib.parse.urlencode(
            {"password": "gate-pass-7", **AUTHORIZE_PARAMS, "state": "s5"}
        ).encode()

        for method in ("HEAD", "PUT", "DELETE"):
            with self.subTest(method=method):
                status, headers, _ = asyncio.run(
                    drive(
                        self.gate,
                        make_scope(method, body=body_bytes),
                        body=body_bytes,
                    )
                )

                self.assertEqual(status, 405)
                self.assertEqual(headers.get("allow"), "GET, POST")
                self.assertEqual(self.provider.auth_codes, {})
        self.assertEqual(self.calls, [])

    def test_oversized_body_rejected_before_password_check(self):
        self.gate.max_body_bytes = 32
        filler = "x" * 100
        body_bytes = urllib.parse.urlencode(
            {"password": "gate-pass-7", "state": filler}
        ).encode()

        status, _, _ = asyncio.run(
            drive(self.gate, make_scope("POST", body=body_bytes), body=body_bytes)
        )
        self.assertEqual(status, 413)

        # Without a declared Content-Length the limit must still hold
        # while the body streams in.
        scope = make_scope("POST", body=body_bytes)
        scope["headers"] = [
            (k, v)
            for k, v in scope["headers"]
            if k != b"content-length"
        ]
        status, _, _ = asyncio.run(drive(self.gate, scope, body=body_bytes))
        self.assertEqual(status, 413)
        self.assertEqual(self.provider.auth_codes, {})
        self.assertEqual(self.calls, [])

    def test_repeated_query_params_preserved(self):
        query = urllib.parse.urlencode(
            [
                *AUTHORIZE_PARAMS.items(),
                ("state", "s6"),
                ("resource", "https://a.example/mcp"),
                ("resource", "https://b.example/mcp"),
            ]
        )

        status, _, body = asyncio.run(
            drive(self.gate, make_scope("GET", query.encode()))
        )

        self.assertEqual(status, 200)
        # One resource param in AUTHORIZE_PARAMS plus the two repeats.
        self.assertEqual(body.count('name="resource"'), 3)
        self.assertIn('name="resource" value="https://a.example/mcp"', body)
        self.assertIn('name="resource" value="https://b.example/mcp"', body)
        self.assertIn('name="resource" value="https://example.com/mcp"', body)


class FakeRoute:
    def __init__(self, path, app):
        self.path = path
        self.app = app


class PasswordGateInstallTests(unittest.TestCase):
    def setUp(self):
        install_mcp_auth_stubs()
        sys.modules.pop("oauth_provider", None)
        self.oauth_provider = importlib.import_module("oauth_provider")

    def tearDown(self):
        sys.modules.pop("mcp.server.auth.routes", None)

    def install_stub_routes(self, routes):
        module = types.ModuleType("mcp.server.auth.routes")

        # Fresh route objects per invocation, like the real SDK builder.
        module.create_auth_routes = lambda **kwargs: [
            FakeRoute(route.path, route.app) for route in routes
        ]
        sys.modules["mcp.server.auth.routes"] = module
        return module

    def test_install_wraps_only_authorize_route(self):
        authorize_inner = object()
        token_inner = object()

        module = self.install_stub_routes([
            FakeRoute("/authorize", authorize_inner),
            FakeRoute("/token", token_inner),
        ])

        provider = make_provider()

        routes = module.create_auth_routes(provider=provider)
        self.assertIsInstance(
            routes[0].app, self.oauth_provider.AuthorizePasswordGate
        )
        self.assertIs(routes[0].app.provider, provider)
        self.assertIs(routes[0].app.inner, authorize_inner)
        self.assertIs(routes[1].app, token_inner)

    def test_gate_binds_to_provider_passed_at_build(self):
        # The wrapper must bind per create_auth_routes invocation, not to
        # whichever provider happened to install the patch first.
        module = self.install_stub_routes([FakeRoute("/authorize", object())])

        provider_a = make_provider("pass-a")
        provider_b = make_provider("pass-b")
        empty_provider = make_provider("")

        routes_a = module.create_auth_routes(provider=provider_a)
        routes_b = module.create_auth_routes(provider=provider_b)
        routes_empty = module.create_auth_routes(provider=empty_provider)

        self.assertIs(routes_a[0].app.provider, provider_a)
        self.assertIs(routes_b[0].app.provider, provider_b)
        self.assertNotIsInstance(
            routes_empty[0].app, self.oauth_provider.AuthorizePasswordGate
        )

    def test_install_is_idempotent(self):
        module = self.install_stub_routes([FakeRoute("/authorize", object())])

        make_provider()
        patched = module.create_auth_routes
        make_provider()
        self.assertIs(module.create_auth_routes, patched)

    def test_install_fails_closed_without_authorize_route(self):
        # The gate is verified when FastMCP builds the auth routes, so a
        # missing /authorize route must abort the build, not pass silently.
        module = self.install_stub_routes([FakeRoute("/token", object())])

        provider = make_provider()

        with self.assertRaisesRegex(RuntimeError, "authorize"):
            module.create_auth_routes(provider=provider)


REAL_STACK_SCRIPT = r"""
import asyncio
import sys
import urllib.parse

sys.path.insert(0, ".")

from pydantic import AnyHttpUrl

from oauth_provider import AUTHORIZE_PATH, SimpleOAuthProvider

provider = SimpleOAuthProvider(
    server_url="https://example.com",
    access_password="real-stack-pw",
    client_id="claude-client",
    client_secret="client-secret",
)

from mcp.server.auth.routes import create_auth_routes
from mcp.server.auth.settings import (
    ClientRegistrationOptions,
    RevocationOptions,
)

routes = create_auth_routes(
    provider=provider,
    issuer_url=AnyHttpUrl("https://example.com"),
    client_registration_options=ClientRegistrationOptions(enabled=False),
    revocation_options=RevocationOptions(enabled=False),
)
route = next(r for r in routes if r.path == AUTHORIZE_PATH)
assert type(route.app).__name__ == "AuthorizePasswordGate", route.app

QUERY = urllib.parse.urlencode({
    "client_id": "claude-client",
    "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
    "response_type": "code",
    "code_challenge": "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
    "code_challenge_method": "S256",
    "state": "s",
    "scope": "claudeai",
})


async def drive(method, query=b"", body=b""):
    sent = []
    pending = [{"type": "http.request", "body": body, "more_body": False}]

    async def receive():
        return pending.pop(0) if pending else {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    headers = [(b"host", b"example.com")]
    if body:
        headers.append((b"content-type", b"application/x-www-form-urlencoded"))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "https",
        "path": AUTHORIZE_PATH,
        "raw_path": AUTHORIZE_PATH.encode(),
        "query_string": query,
        "root_path": "",
        "server": ("example.com", 443),
        "client": ("203.0.113.4", 54321),
        "headers": headers,
    }
    await route(scope, receive, send)
    status = sent[0]["status"]
    payload = b"".join(
        m.get("body", b"")
        for m in sent
        if m["type"] == "http.response.body"
    )
    return status, payload.decode("utf-8", "replace")


async def main():
    status, body = await drive("GET", QUERY.encode())
    assert status == 200 and 'action="/authorize"' in body, (status, body)

    head_body = ("password=real-stack-pw&" + QUERY).encode()
    status, _ = await drive("HEAD", QUERY.encode(), head_body)
    assert status == 405, status
    assert not provider.auth_codes, provider.auth_codes

    print("REAL-STACK-GATE-OK")


asyncio.run(main())
"""


def mcp_distribution_available() -> bool:
    try:
        importlib.metadata.version("mcp")
    except importlib.metadata.PackageNotFoundError:
        return False
    return True


REAL_FASTMCP_SCRIPT = r"""
import asyncio
import sys

sys.path.insert(0, ".")

import httpx
from pydantic import AnyHttpUrl

from oauth_provider import SimpleOAuthProvider

provider = SimpleOAuthProvider(
    server_url="https://example.com",
    access_password="fastmcp-pw",
    client_id="claude-client",
    client_secret="client-secret",
)

from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

# Assemble FastMCP the way server.py does in OAuth mode.
mcp = FastMCP(
    name="Obsidian MCP Server",
    host="127.0.0.1",
    port=8484,
    auth_server_provider=provider,
    auth=AuthSettings(
        issuer_url=AnyHttpUrl("https://example.com"),
        resource_server_url=AnyHttpUrl("https://example.com/mcp"),
        client_registration_options=ClientRegistrationOptions(enabled=False),
    ),
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=False,
    ),
)

# The same ASGI app that run.py serves via mcp.run("streamable-http").
app = mcp.streamable_http_app()

QUERY = {
    "client_id": "claude-client",
    "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
    "response_type": "code",
    "code_challenge": "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
    "code_challenge_method": "S256",
    "state": "fastmcp-wiring",
    "scope": "claudeai",
}


async def main():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="https://example.com"
    ) as client:
        response = await client.get("/authorize", params=QUERY)
        assert response.status_code == 200, response.status_code
        assert response.headers["content-type"].startswith("text/html")
        assert 'action="/authorize"' in response.text, response.text
        assert 'name="client_id" value="claude-client"' in response.text
        assert not provider.auth_codes, provider.auth_codes

        response = await client.head("/authorize", params=QUERY)
        assert response.status_code == 405, response.status_code
        assert response.headers.get("allow") == "GET, POST"
        assert not provider.auth_codes, provider.auth_codes
    print("REAL-FASTMCP-GATE-OK")


asyncio.run(main())
"""


@unittest.skipUnless(
    mcp_distribution_available(), "MCP SDK distribution not installed"
)
class PasswordGateRealStackTests(unittest.TestCase):
    def test_real_stack_authorize_route_is_gated(self):
        # Runs in a subprocess so the stubbed sys.modules entries used by
        # the other tests cannot leak into the real SDK import.
        result = subprocess.run(
            [sys.executable, "-c", REAL_STACK_SCRIPT],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=str(Path(__file__).resolve().parent.parent),
        )
        self.assertEqual(
            result.returncode, 0, f"stderr: {result.stderr}\nstdout: {result.stdout}"
        )
        self.assertIn("REAL-STACK-GATE-OK", result.stdout)

    def test_real_fastmcp_app_serves_gated_authorize(self):
        # End-to-end wiring: the FINAL FastMCP application (the app run.py
        # serves) must route /authorize through the password gate. If
        # FastMCP's construction ever stops going through the patched
        # create_auth_routes seam, the GET below returns the SDK's
        # auto-approve redirect (302), not the form, and this fails.
        result = subprocess.run(
            [sys.executable, "-c", REAL_FASTMCP_SCRIPT],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=str(Path(__file__).resolve().parent.parent),
        )
        self.assertEqual(
            result.returncode, 0, f"stderr: {result.stderr}\nstdout: {result.stdout}"
        )
        self.assertIn("REAL-FASTMCP-GATE-OK", result.stdout)

"""OAUTH_PASSWORD gate at /authorize (audit E1).

Uses the stubbing approach of test_auth.py: the ``mcp.*`` modules are
replaced with minimal fakes before importing ``oauth_provider``, and the
gate is exercised as a plain ASGI application with hand-rolled scopes.
"""

import asyncio
import importlib
import sys
import types
import unittest
import urllib.parse

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
        module.create_auth_routes = lambda **kwargs: list(routes)
        sys.modules["mcp.server.auth.routes"] = module
        return module

    def test_install_wraps_only_authorize_route(self):
        authorize_inner = object()
        token_inner = object()

        module = self.install_stub_routes([
            FakeRoute("/authorize", authorize_inner),
            FakeRoute("/token", token_inner),
        ])

        make_provider()
        routes = module.create_auth_routes()
        self.assertIsInstance(
            routes[0].app, self.oauth_provider.AuthorizePasswordGate
        )
        self.assertIs(routes[0].app.inner, authorize_inner)
        self.assertIs(routes[1].app, token_inner)

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

        make_provider()

        with self.assertRaisesRegex(RuntimeError, "authorize"):
            module.create_auth_routes()

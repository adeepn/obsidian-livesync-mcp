"""Simple OAuth 2.1 Authorization Server Provider for MCP."""

import functools
import hmac
import html
import secrets
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any, Mapping

from starlette.requests import Request
from starlette.responses import HTMLResponse
from starlette.types import ASGIApp, Receive, Scope, Send
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

AUTHORIZE_PATH = "/authorize"


@dataclass
class SimpleAuthCode:
    code: str
    client_id: str
    redirect_uri: str
    redirect_uri_provided_explicitly: bool
    code_challenge: str
    scopes: list[str]
    expires_at: float


@dataclass
class SimpleAccessToken:
    token: str
    client_id: str
    scopes: list[str]
    expires_at: float


@dataclass
class SimpleRefreshToken:
    token: str
    client_id: str
    scopes: list[str]
    expires_at: float


class AuthorizePasswordGate:
    """ASGI gate enforcing OAUTH_PASSWORD before /authorize issues a code.

    Wraps the /authorize endpoint built by the MCP SDK auth router. With no
    access password configured the wrapper is a transparent passthrough
    (the pre-gate behaviour). With a password set:

    * GET returns a minimal inline-HTML form that POSTs the password and
      the original request parameters back to the same path;
    * POST with a wrong or missing password returns 401 with the form
      again; the submitted value is never echoed back;
    * POST with the correct password (constant-time comparison) replays
      the request to the wrapped endpoint, which then validates the
      client, redirect URI and PKCE and issues the authorization code
      exactly as in the passwordless flow.

    CSRF protection beyond the OAuth ``state`` parameter passthrough is
    deliberately out of scope.
    """

    def __init__(self, provider: "SimpleOAuthProvider", inner: ASGIApp):
        self.provider = provider
        self.inner = inner

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not self.provider.access_password:
            await self.inner(scope, receive, send)
            return

        request = Request(scope, receive)
        if request.method == "GET":
            response = self._form_response(request.query_params)
            await response(scope, receive, send)
            return
        if request.method == "POST":
            body = await request.body()
            fields = dict(
                urllib.parse.parse_qsl(
                    body.decode("utf-8", "replace"), keep_blank_values=True
                )
            )
            submitted = fields.pop("password", "")
            if not self.provider.password_is_valid(submitted):
                response = self._form_response(fields, status_code=401)
                await response(scope, receive, send)
                return

            replay_messages = [
                {"type": "http.request", "body": body, "more_body": False}
            ]

            async def replay() -> Any:
                # The gate consumed the request body; re-feed it verbatim.
                # The SDK validates the OAuth fields and ignores the
                # password one (its request model drops unknown fields).
                if replay_messages:
                    return replay_messages.pop(0)
                return {"type": "http.disconnect"}

            await self.inner(scope, replay, send)
            return
        await self.inner(scope, receive, send)

    def _form_response(
        self, params: Mapping[str, str], status_code: int = 200
    ) -> HTMLResponse:
        return HTMLResponse(
            self.provider.build_password_form(params),
            status_code=status_code,
            headers={"Cache-Control": "no-store"},
        )


class SimpleOAuthProvider(
    OAuthAuthorizationServerProvider[SimpleAuthCode, SimpleAccessToken, SimpleRefreshToken]
):
    """In-memory OAuth provider gated by a password at the /authorize page."""

    def __init__(
        self,
        server_url: str,
        access_password: str,
        client_id: str = "",
        client_secret: str = "",
    ):
        self.server_url = server_url.rstrip("/")
        self.access_password = access_password
        self.clients: dict[str, OAuthClientInformationFull] = {}
        self.auth_codes: dict[str, SimpleAuthCode] = {}
        self.access_tokens: dict[str, SimpleAccessToken] = {}
        self.refresh_tokens: dict[str, SimpleRefreshToken] = {}

        # Pre-register fixed client
        if client_id and client_secret:
            self.clients[client_id] = OAuthClientInformationFull(
                client_id=client_id,
                client_secret=client_secret,
                redirect_uris=["https://claude.ai/api/mcp/auth_callback", "https://claude.com/api/mcp/auth_callback"],
                client_name="Claude",
                grant_types=["authorization_code", "refresh_token"],
                response_types=["code"],
                token_endpoint_auth_method="client_secret_post",
                scope="claudeai",
            )

        if access_password:
            self._install_authorize_password_gate()

    def build_password_form(self, params: Mapping[str, str]) -> str:
        """Minimal login form preserving ``params`` across the POST round-trip.

        Inline HTML only: no external assets, no new dependencies. Values
        are HTML-escaped because OAuth parameters are client-controlled.
        A ``password`` key is never turned into a hidden field.
        """
        hidden = "".join(
            f'    <input type="hidden" name="{html.escape(name)}" '
            f'value="{html.escape(str(value))}">\n'
            for name, value in sorted(params.items())
            if name != "password"
        )
        return (
            "<!DOCTYPE html>\n"
            '<html lang="en">\n'
            "<head><meta charset=\"utf-8\">"
            "<title>Obsidian MCP authorization</title></head>\n"
            "<body>\n"
            f'  <form method="POST" action="{AUTHORIZE_PATH}">\n'
            f"{hidden}"
            "    <label>Password "
            '<input type="password" name="password" '
            "autocomplete=\"current-password\" required></label>\n"
            "    <button type=\"submit\">Authorize</button>\n"
            "  </form>\n"
            "</body>\n"
            "</html>\n"
        )

    def password_is_valid(self, submitted: str) -> bool:
        """Constant-time comparison against the configured password."""
        if not self.access_password or not submitted:
            return False
        return hmac.compare_digest(
            submitted.encode("utf-8"), self.access_password.encode("utf-8")
        )

    def _install_authorize_password_gate(self) -> None:
        """Wrap the MCP SDK /authorize route with this provider's gate.

        FastMCP imports ``create_auth_routes`` lazily while building the
        HTTP app, so replacing the module attribute at construction time is
        sufficient. The patch is idempotent and fails closed at startup if
        the SDK no longer exposes the /authorize route — the gate must not
        silently disappear. Skipped when the MCP SDK is stubbed out (unit
        tests exercise the gate class directly).
        """
        try:
            from mcp.server.auth import routes as sdk_routes
        except ImportError:
            return
        create_auth_routes = sdk_routes.create_auth_routes
        if getattr(create_auth_routes, "_password_gate_installed", False):
            return

        provider = self

        @functools.wraps(create_auth_routes)
        def gated_create_auth_routes(*args: Any, **kwargs: Any):
            routes = create_auth_routes(*args, **kwargs)
            authorize_routes = [
                route
                for route in routes
                if getattr(route, "path", None) == AUTHORIZE_PATH
            ]
            if len(authorize_routes) != 1:
                raise RuntimeError(
                    "OAUTH_PASSWORD gate: expected exactly one "
                    f"{AUTHORIZE_PATH} route in the MCP SDK auth router."
                )
            route = authorize_routes[0]
            route.app = AuthorizePasswordGate(provider, route.app)
            return routes

        gated_create_auth_routes._password_gate_installed = True
        sdk_routes.create_auth_routes = gated_create_auth_routes

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return self.clients.get(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        self.clients[client_info.client_id] = client_info

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        # Auto-approve: generate authorization code immediately. With
        # OAUTH_PASSWORD set this is only reachable once the authorize
        # password gate (AuthorizePasswordGate) approved the request.
        code = secrets.token_hex(32)
        redirect_uri_str = str(params.redirect_uri)
        self.auth_codes[code] = SimpleAuthCode(
            code=code,
            client_id=client.client_id,
            redirect_uri=redirect_uri_str,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            code_challenge=params.code_challenge,
            scopes=params.scopes or [],
            expires_at=time.time() + 300,
        )
        return construct_redirect_uri(
            redirect_uri_str, code=code, state=params.state
        )

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> SimpleAuthCode | None:
        ac = self.auth_codes.get(authorization_code)
        if ac and ac.client_id == client.client_id:
            if time.time() > ac.expires_at:
                del self.auth_codes[authorization_code]
                return None
            return ac
        return None

    async def exchange_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: SimpleAuthCode,
    ) -> OAuthToken:
        self.auth_codes.pop(authorization_code.code, None)

        access_token = secrets.token_hex(32)
        refresh_token = secrets.token_hex(32)

        self.access_tokens[access_token] = SimpleAccessToken(
            token=access_token,
            client_id=client.client_id,
            scopes=authorization_code.scopes,
            expires_at=time.time() + 86400,
        )
        self.refresh_tokens[refresh_token] = SimpleRefreshToken(
            token=refresh_token,
            client_id=client.client_id,
            scopes=authorization_code.scopes,
            expires_at=time.time() + 86400 * 30,
        )

        return OAuthToken(
            access_token=access_token,
            token_type="Bearer",
            expires_in=86400,
            refresh_token=refresh_token,
            scope=" ".join(authorization_code.scopes) if authorization_code.scopes else None,
        )

    async def load_access_token(self, token: str) -> SimpleAccessToken | None:
        at = self.access_tokens.get(token)
        if at and time.time() > at.expires_at:
            del self.access_tokens[token]
            return None
        return at

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> SimpleRefreshToken | None:
        rt = self.refresh_tokens.get(refresh_token)
        if rt and rt.client_id == client.client_id:
            if time.time() > rt.expires_at:
                del self.refresh_tokens[refresh_token]
                return None
            return rt
        return None

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: SimpleRefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        self.refresh_tokens.pop(refresh_token.token, None)

        new_access_token = secrets.token_hex(32)
        new_refresh_token = secrets.token_hex(32)
        effective_scopes = scopes or refresh_token.scopes

        self.access_tokens[new_access_token] = SimpleAccessToken(
            token=new_access_token,
            client_id=client.client_id,
            scopes=effective_scopes,
            expires_at=time.time() + 86400,
        )
        self.refresh_tokens[new_refresh_token] = SimpleRefreshToken(
            token=new_refresh_token,
            client_id=client.client_id,
            scopes=effective_scopes,
            expires_at=time.time() + 86400 * 30,
        )

        return OAuthToken(
            access_token=new_access_token,
            token_type="Bearer",
            expires_in=86400,
            refresh_token=new_refresh_token,
            scope=" ".join(effective_scopes) if effective_scopes else None,
        )

    async def revoke_token(
        self, token: SimpleAccessToken | SimpleRefreshToken
    ) -> None:
        if isinstance(token, SimpleAccessToken):
            self.access_tokens.pop(token.token, None)
        elif isinstance(token, SimpleRefreshToken):
            self.refresh_tokens.pop(token.token, None)

"""Card-driven outbound auth (spec §19.3).

Two pieces, both deterministic code — never prompt-enforced:

- ``AgentCredentialService``: the SDK ``CredentialService`` contract
  (``get_credentials(scheme_name, context) -> str | None``). Resolves the
  agent's stored credential for a scheme, applying ``env:VAR_NAME``
  indirection; for oauth2 client_credentials it runs an authlib token
  fetch with a per-(agent, scheme) in-process cache refreshed on expiry
  skew. Returns the *string* the interceptor should place.

- ``ConciergeAuthInterceptor``: a standalone ``ClientCallInterceptor``
  covering all five placements in scope — http bearer, http basic,
  oauth2/oidc (bearer placement), apiKey in header/query/cookie. The
  SDK's own ``AuthInterceptor`` (0.3.26) skips basic and query/cookie
  apiKey, so we implement placement ourselves rather than delegating —
  one code path, contract-tested.

Supported scheme types (spec §19.3): apiKey, http bearer/basic, oauth2
client_credentials. Everything else is reported unsupported at card
fetch (manager.project_auth_schemes) and never silently unauthenticated.
"""

import base64
import os
import time
from dataclasses import dataclass
from typing import Any

import structlog
from a2a.client.middleware import ClientCallContext, ClientCallInterceptor
from a2a.types import (
    AgentCard,
    APIKeySecurityScheme,
    HTTPAuthSecurityScheme,
    In,
    OAuth2SecurityScheme,
    OpenIdConnectSecurityScheme,
)

logger = structlog.get_logger("a2a")

# refresh this many seconds before the token's stated expiry
_TOKEN_EXPIRY_SKEW_S = 60.0

# (agent_id, scheme_name) -> (access_token, expires_at_monotonic)
_TOKEN_CACHE: dict[tuple[str, str], tuple[str, float]] = {}


def clear_token_cache() -> None:
    _TOKEN_CACHE.clear()


def resolve_credential_value(value: Any) -> Any:
    """Apply the env:VAR_NAME indirection (spec §19.3) to stored values."""
    if isinstance(value, str) and value.startswith("env:"):
        return os.environ.get(value[4:], "")
    if isinstance(value, dict):
        return {k: resolve_credential_value(v) for k, v in value.items()}
    return value


def scheme_supported(scheme_def: Any) -> bool:
    """Is this card scheme one the §19.3 cut implements?"""
    match scheme_def:
        case APIKeySecurityScheme():
            return True
        case HTTPAuthSecurityScheme():
            return scheme_def.scheme.lower() in {"bearer", "basic"}
        case OAuth2SecurityScheme():
            flows = scheme_def.flows
            return flows is not None and flows.client_credentials is not None
        case _:
            return False


@dataclass
class AgentCredentialService:
    """SDK CredentialService bound to one registered agent's stored creds."""

    agent_id: str
    card: AgentCard
    credentials: dict[str, Any]

    async def get_credentials(
        self, security_scheme_name: str, context: ClientCallContext | None = None
    ) -> str | None:
        raw = self.credentials.get(security_scheme_name)
        if raw is None:
            return None
        value = resolve_credential_value(raw)
        scheme_def = self._scheme_def(security_scheme_name)
        if isinstance(scheme_def, OAuth2SecurityScheme):
            return await self._oauth2_token(security_scheme_name, scheme_def, value)
        if isinstance(value, str):
            return value or None
        return None

    def _scheme_def(self, scheme_name: str) -> Any:
        schemes = self.card.security_schemes or {}
        wrapper = schemes.get(scheme_name)
        return wrapper.root if wrapper is not None else None

    async def _oauth2_token(
        self, scheme_name: str, scheme_def: OAuth2SecurityScheme, value: Any
    ) -> str | None:
        cache_key = (self.agent_id, scheme_name)
        cached = _TOKEN_CACHE.get(cache_key)
        if cached is not None and cached[1] > time.monotonic():
            return cached[0]
        flows = scheme_def.flows
        flow = flows.client_credentials if flows is not None else None
        if flow is None or not isinstance(value, dict):
            return None
        client_id = str(value.get("client_id") or "")
        client_secret = str(value.get("client_secret") or "")
        if not client_id or not client_secret:
            return None
        from authlib.integrations.httpx_client import AsyncOAuth2Client

        scopes = " ".join((flow.scopes or {}).keys()) or None
        # Any: the published authlib stubs omit the httpx.AsyncClient base
        # (no __aexit__/aclose), so attribute checks are wrong there
        oauth: Any = AsyncOAuth2Client(
            client_id=client_id, client_secret=client_secret, scope=scopes
        )
        try:
            token = await oauth.fetch_token(flow.token_url, grant_type="client_credentials")
        finally:
            await oauth.aclose()
        access = str(token.get("access_token") or "")
        if not access:
            return None
        ttl = float(token.get("expires_in") or 3600.0)
        _TOKEN_CACHE[cache_key] = (
            access,
            time.monotonic() + max(ttl - _TOKEN_EXPIRY_SKEW_S, 30.0),
        )
        logger.info(
            "a2a_oauth2_token",
            tier="a2a",
            kind="send",
            agent_id=self.agent_id,
            scheme=scheme_name,
        )
        return access


class ConciergeAuthInterceptor(ClientCallInterceptor):
    """Applies the first satisfiable card security requirement per call."""

    def __init__(self, credential_service: AgentCredentialService):
        self._credentials = credential_service

    async def intercept(
        self,
        method_name: str,
        request_payload: dict[str, Any],
        http_kwargs: dict[str, Any],
        agent_card: AgentCard | None,
        context: ClientCallContext | None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if agent_card is None or not agent_card.security or not agent_card.security_schemes:
            return request_payload, http_kwargs
        for requirement in agent_card.security:
            for scheme_name in requirement:
                wrapper = agent_card.security_schemes.get(scheme_name)
                if wrapper is None:
                    continue
                credential = await self._credentials.get_credentials(scheme_name, context)
                if not credential:
                    continue
                if self._apply(wrapper.root, credential, http_kwargs):
                    return request_payload, http_kwargs
        return request_payload, http_kwargs

    def _apply(self, scheme_def: Any, credential: str, http_kwargs: dict[str, Any]) -> bool:
        headers = http_kwargs.setdefault("headers", {})
        match scheme_def:
            case HTTPAuthSecurityScheme() if scheme_def.scheme.lower() == "bearer":
                headers["Authorization"] = f"Bearer {credential}"
            case HTTPAuthSecurityScheme() if scheme_def.scheme.lower() == "basic":
                encoded = base64.b64encode(credential.encode()).decode()
                headers["Authorization"] = f"Basic {encoded}"
            case OAuth2SecurityScheme() | OpenIdConnectSecurityScheme():
                headers["Authorization"] = f"Bearer {credential}"
            case APIKeySecurityScheme(in_=In.header):
                headers[scheme_def.name] = credential
            case APIKeySecurityScheme(in_=In.query):
                params = http_kwargs.setdefault("params", {})
                params[scheme_def.name] = credential
            case APIKeySecurityScheme(in_=In.cookie):
                cookies = http_kwargs.setdefault("cookies", {})
                cookies[scheme_def.name] = credential
            case _:
                return False
        return True

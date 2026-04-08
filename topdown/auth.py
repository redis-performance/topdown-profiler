"""Authentication for the MCP server.

Supports two modes:
- API key: simple bearer token validation (good for internal/CI use)
- OAuth 2.1: JWT validation against an external authorization server (production)

Configuration via environment variables:
- TOPDOWN_AUTH_MODE: "none" (default), "api-key", or "oauth"
- TOPDOWN_API_KEY: the API key (for api-key mode)
- TOPDOWN_OAUTH_ISSUER: OAuth issuer URL (for oauth mode)
- TOPDOWN_OAUTH_AUDIENCE: expected audience claim (for oauth mode)
- TOPDOWN_OAUTH_JWKS_URI: JWKS endpoint (auto-discovered from issuer if not set)
"""

import hmac
import json
import logging
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

CONFIG_DIR = Path.home() / ".topdown"
API_KEY_FILE = CONFIG_DIR / "api_key"


@dataclass
class AuthConfig:
    mode: str = "none"  # "none", "api-key", "oauth"
    api_key: str | None = None
    oauth_issuer: str | None = None
    oauth_audience: str | None = None
    oauth_jwks_uri: str | None = None

    @classmethod
    def from_env(cls) -> "AuthConfig":
        mode = os.environ.get("TOPDOWN_AUTH_MODE", "none")
        api_key = os.environ.get("TOPDOWN_API_KEY")

        # If no env var, try reading from file
        if mode == "api-key" and not api_key:
            api_key = _read_api_key_file()

        return cls(
            mode=mode,
            api_key=api_key,
            oauth_issuer=os.environ.get("TOPDOWN_OAUTH_ISSUER"),
            oauth_audience=os.environ.get("TOPDOWN_OAUTH_AUDIENCE"),
            oauth_jwks_uri=os.environ.get("TOPDOWN_OAUTH_JWKS_URI"),
        )

    @property
    def is_enabled(self) -> bool:
        return self.mode != "none"


# ──────────────────────────── API Key management ────────────────────────────


def generate_api_key() -> str:
    """Generate a cryptographically secure API key."""
    return f"td_{secrets.token_urlsafe(32)}"


def save_api_key(api_key: str) -> Path:
    """Save API key to ~/.topdown/api_key with restricted permissions."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    API_KEY_FILE.write_text(api_key)
    API_KEY_FILE.chmod(0o600)
    return API_KEY_FILE


def _read_api_key_file() -> str | None:
    """Read API key from file if it exists."""
    try:
        return API_KEY_FILE.read_text().strip()
    except (FileNotFoundError, PermissionError):
        return None


def verify_api_key(provided: str, expected: str) -> bool:
    """Constant-time comparison to prevent timing attacks."""
    return hmac.compare_digest(provided.encode(), expected.encode())


# ──────────────────────────── OAuth JWT verification ────────────────────────


def _get_jwks_uri(issuer: str) -> str:
    """Discover JWKS URI from OAuth issuer's well-known endpoint."""
    import urllib.request

    well_known = f"{issuer.rstrip('/')}/.well-known/oauth-authorization-server"
    try:
        with urllib.request.urlopen(well_known, timeout=5) as resp:
            metadata = json.loads(resp.read())
            return metadata["jwks_uri"]
    except Exception:
        # Fallback to OpenID Connect discovery
        well_known = f"{issuer.rstrip('/')}/.well-known/openid-configuration"
        with urllib.request.urlopen(well_known, timeout=5) as resp:
            metadata = json.loads(resp.read())
            return metadata["jwks_uri"]


# ──────────────────────────── MCP Token Verifier ────────────────────────────


class TopdownTokenVerifier:
    """Implements the MCP TokenVerifier protocol.

    Validates tokens based on the configured auth mode:
    - api-key: checks bearer token against stored API key
    - oauth: validates JWT signature, issuer, audience, expiry
    """

    def __init__(self, auth_config: AuthConfig):
        self.config = auth_config
        self._jwks_client = None

    async def verify_token(self, token: str) -> dict[str, Any] | None:
        """Verify a bearer token. Returns token info dict or None if invalid."""
        if self.config.mode == "api-key":
            return self._verify_api_key(token)
        elif self.config.mode == "oauth":
            return await self._verify_oauth(token)
        return None

    def _verify_api_key(self, token: str) -> dict[str, Any] | None:
        """Verify against stored API key."""
        if not self.config.api_key:
            logger.error("API key auth enabled but no key configured")
            return None

        if verify_api_key(token, self.config.api_key):
            return {
                "sub": "api-key-user",
                "scope": "topdown:read topdown:write",
                "auth_mode": "api-key",
            }

        logger.warning("Invalid API key presented")
        return None

    async def _verify_oauth(self, token: str) -> dict[str, Any] | None:
        """Verify a JWT against the OAuth issuer's JWKS."""
        try:
            import jwt as pyjwt
        except ImportError:
            logger.error("PyJWT not installed. Run: pip install 'topdown-profiler[oauth]'")
            return None

        try:
            jwks_uri = self.config.oauth_jwks_uri
            if not jwks_uri and self.config.oauth_issuer:
                jwks_uri = _get_jwks_uri(self.config.oauth_issuer)

            if not jwks_uri:
                logger.error("No JWKS URI configured or discoverable")
                return None

            if self._jwks_client is None:
                self._jwks_client = pyjwt.PyJWKClient(jwks_uri, cache_keys=True)

            signing_key = self._jwks_client.get_signing_key_from_jwt(token)

            decode_options = {
                "algorithms": ["RS256", "ES256"],
                "options": {"verify_exp": True, "verify_aud": True},
            }
            if self.config.oauth_audience:
                decode_options["audience"] = self.config.oauth_audience
            if self.config.oauth_issuer:
                decode_options["issuer"] = self.config.oauth_issuer

            claims = pyjwt.decode(
                token,
                signing_key.key,
                **decode_options,
            )

            return {
                "sub": claims.get("sub", "unknown"),
                "scope": claims.get("scope", ""),
                "auth_mode": "oauth",
                "claims": claims,
            }

        except Exception as e:
            logger.warning("OAuth token verification failed: %s", e)
            return None


# ──────────────────────────── Setup helpers ────────────────────────────


def setup_api_key_auth() -> tuple[str, Path]:
    """Generate and save a new API key. Returns (key, path)."""
    key = generate_api_key()
    path = save_api_key(key)
    return key, path


def get_mcp_auth_kwargs(auth_config: AuthConfig | None = None) -> dict:
    """Get kwargs to pass to FastMCP constructor for auth.

    Returns empty dict if auth is disabled, otherwise returns
    the token_verifier and auth settings.
    """
    if auth_config is None:
        auth_config = AuthConfig.from_env()

    if not auth_config.is_enabled:
        return {}

    verifier = TopdownTokenVerifier(auth_config)

    return {
        "token_verifier": verifier,
    }


def get_client_config_snippet(
    host: str = "localhost",
    port: int = 8000,
    auth_mode: str = "api-key",
    api_key: str | None = None,
) -> dict:
    """Generate MCP client config snippet for Claude Code/Desktop."""
    if auth_mode == "api-key":
        return {
            "mcpServers": {
                "topdown": {
                    "type": "streamable-http",
                    "url": f"http://{host}:{port}/mcp",
                    "headers": {
                        "Authorization": f"Bearer {api_key or '<your-api-key>'}"
                    },
                }
            }
        }
    elif auth_mode == "oauth":
        return {
            "mcpServers": {
                "topdown": {
                    "type": "streamable-http",
                    "url": f"http://{host}:{port}/mcp",
                    "note": "OAuth tokens are managed automatically by the MCP client",
                }
            }
        }
    else:
        return {
            "mcpServers": {
                "topdown": {
                    "command": "topdown",
                    "args": ["mcp-serve"],
                }
            }
        }

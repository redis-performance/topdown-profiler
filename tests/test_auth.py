"""Tests for authentication module."""

import pytest

from topdown.auth import (
    AuthConfig,
    TopdownTokenVerifier,
    generate_api_key,
    save_api_key,
    verify_api_key,
    setup_api_key_auth,
    get_mcp_auth_kwargs,
    get_client_config_snippet,
)


class TestGenerateApiKey:
    def test_format(self):
        key = generate_api_key()
        assert key.startswith("td_")
        assert len(key) > 20

    def test_unique(self):
        keys = {generate_api_key() for _ in range(10)}
        assert len(keys) == 10


class TestVerifyApiKey:
    def test_valid(self):
        key = generate_api_key()
        assert verify_api_key(key, key) is True

    def test_invalid(self):
        assert verify_api_key("wrong", "correct") is False

    def test_empty(self):
        assert verify_api_key("", "") is True

    def test_timing_safe(self):
        # Just verify it doesn't crash with different lengths
        assert verify_api_key("short", "a-much-longer-key-value") is False


class TestSaveApiKey:
    def test_saves_to_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr("topdown.auth.CONFIG_DIR", tmp_path)
        monkeypatch.setattr("topdown.auth.API_KEY_FILE", tmp_path / "api_key")

        key = generate_api_key()
        path = save_api_key(key)
        assert path.exists()
        assert path.read_text() == key

    def test_file_permissions(self, tmp_path, monkeypatch):
        monkeypatch.setattr("topdown.auth.CONFIG_DIR", tmp_path)
        monkeypatch.setattr("topdown.auth.API_KEY_FILE", tmp_path / "api_key")

        key = generate_api_key()
        path = save_api_key(key)
        mode = oct(path.stat().st_mode)[-3:]
        assert mode == "600"


class TestAuthConfig:
    def test_defaults(self):
        config = AuthConfig()
        assert config.mode == "none"
        assert config.is_enabled is False

    def test_api_key_mode(self):
        config = AuthConfig(mode="api-key", api_key="td_test123")
        assert config.is_enabled is True

    def test_from_env(self, monkeypatch):
        monkeypatch.setenv("TOPDOWN_AUTH_MODE", "api-key")
        monkeypatch.setenv("TOPDOWN_API_KEY", "td_envkey123")
        config = AuthConfig.from_env()
        assert config.mode == "api-key"
        assert config.api_key == "td_envkey123"

    def test_from_env_defaults(self, monkeypatch):
        monkeypatch.delenv("TOPDOWN_AUTH_MODE", raising=False)
        monkeypatch.delenv("TOPDOWN_API_KEY", raising=False)
        config = AuthConfig.from_env()
        assert config.mode == "none"

    def test_oauth_mode(self):
        config = AuthConfig(
            mode="oauth",
            oauth_issuer="https://auth.example.com",
            oauth_audience="topdown-api",
        )
        assert config.is_enabled is True


class TestTopdownTokenVerifier:
    @pytest.mark.asyncio
    async def test_api_key_valid(self):
        config = AuthConfig(mode="api-key", api_key="td_testkey123")
        verifier = TopdownTokenVerifier(config)
        result = await verifier.verify_token("td_testkey123")
        assert result is not None
        assert result["auth_mode"] == "api-key"

    @pytest.mark.asyncio
    async def test_api_key_invalid(self):
        config = AuthConfig(mode="api-key", api_key="td_testkey123")
        verifier = TopdownTokenVerifier(config)
        result = await verifier.verify_token("wrong_key")
        assert result is None

    @pytest.mark.asyncio
    async def test_api_key_no_key_configured(self):
        config = AuthConfig(mode="api-key", api_key=None)
        verifier = TopdownTokenVerifier(config)
        result = await verifier.verify_token("any_key")
        assert result is None


class TestGetMcpAuthKwargs:
    def test_no_auth(self):
        config = AuthConfig(mode="none")
        kwargs = get_mcp_auth_kwargs(config)
        assert kwargs == {}

    def test_api_key_auth(self):
        config = AuthConfig(mode="api-key", api_key="td_test")
        kwargs = get_mcp_auth_kwargs(config)
        assert "token_verifier" in kwargs


class TestGetClientConfigSnippet:
    def test_api_key_config(self):
        config = get_client_config_snippet(
            host="10.0.0.1", port=9000, auth_mode="api-key", api_key="td_abc"
        )
        assert "topdown" in config["mcpServers"]
        server = config["mcpServers"]["topdown"]
        assert "http://10.0.0.1:9000/mcp" in server["url"]
        assert "Bearer td_abc" in server["headers"]["Authorization"]

    def test_oauth_config(self):
        config = get_client_config_snippet(auth_mode="oauth")
        server = config["mcpServers"]["topdown"]
        assert "url" in server

    def test_stdio_config(self):
        config = get_client_config_snippet(auth_mode="none")
        server = config["mcpServers"]["topdown"]
        assert server["command"] == "topdown"


class TestSetupApiKeyAuth:
    def test_generates_and_saves(self, tmp_path, monkeypatch):
        monkeypatch.setattr("topdown.auth.CONFIG_DIR", tmp_path)
        monkeypatch.setattr("topdown.auth.API_KEY_FILE", tmp_path / "api_key")

        key, path = setup_api_key_auth()
        assert key.startswith("td_")
        assert path.exists()
        assert path.read_text() == key

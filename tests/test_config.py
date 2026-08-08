"""Tests for athenaeum.config.Settings (env parsing and validation)."""

import pytest
from pydantic import ValidationError

from athenaeum.config import Settings

STRONG_KEY = "ab" * 32  # 64 hex chars


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Isolate from any ATHENAEUM_* vars in the developer's shell."""
    monkeypatch.setenv("ATHENAEUM_SECRET_KEY", STRONG_KEY)
    monkeypatch.delenv("ATHENAEUM_FORWARDED_ALLOW_IPS", raising=False)
    return monkeypatch


# --- secret_key strength (SERVER-03) --------------------------------------------


def test_secret_key_placeholder_rejected(_clean_env):
    _clean_env.setenv("ATHENAEUM_SECRET_KEY", "change-me-generate-a-random-hex-key")
    with pytest.raises(ValidationError, match="placeholder"):
        Settings()


def test_secret_key_short_rejected(_clean_env):
    _clean_env.setenv("ATHENAEUM_SECRET_KEY", "s" * 31)
    with pytest.raises(ValidationError, match="at least 32"):
        Settings()


@pytest.mark.parametrize("weak", ["", "   "])
def test_secret_key_empty_or_whitespace_rejected(_clean_env, weak):
    _clean_env.setenv("ATHENAEUM_SECRET_KEY", weak)
    with pytest.raises(ValidationError, match="must not be empty"):
        Settings()


def test_secret_key_strong_accepted(_clean_env):
    assert Settings().secret_key == STRONG_KEY
    _clean_env.setenv("ATHENAEUM_SECRET_KEY", "x" * 32)  # exactly the minimum
    assert len(Settings().secret_key) == 32


# --- forwarded_allow_ips (SERVER-02) ---------------------------------------------


def test_forwarded_allow_ips_defaults_to_none(_clean_env):
    assert Settings().forwarded_allow_ips is None


def test_forwarded_allow_ips_env_parsing(_clean_env):
    _clean_env.setenv("ATHENAEUM_FORWARDED_ALLOW_IPS", "10.0.0.0/8, 172.16.0.0/12")
    assert Settings().forwarded_allow_ips == "10.0.0.0/8, 172.16.0.0/12"

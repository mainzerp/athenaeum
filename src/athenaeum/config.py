"""Server-level configuration from environment variables (12-factor).

Contract: plan §3.6. All server-level configuration comes from the
environment; nothing secret is baked into code or image. LLM provider
keys stay per-user in the DB (Fernet-encrypted), never in env.
"""

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# The placeholder shipped in .env.example; refusing it forces a real key.
SECRET_KEY_PLACEHOLDER = "change-me-generate-a-random-hex-key"
SECRET_KEY_MIN_LENGTH = 32
_SECRET_KEY_FIX = 'python -c "import secrets; print(secrets.token_hex(32))"'


class Settings(BaseSettings):
    """Athenaeum server settings, read from ATHENAEUM_* environment variables."""

    model_config = SettingsConfigDict(env_prefix="ATHENAEUM_")

    # Single persistence root: app.db + users/<user_id>/library/ both live under it.
    data_root: str = "./data"

    # Uvicorn bind address / port (also the EXPOSEd container port).
    host: str = "127.0.0.1"
    port: int = 8000

    # Required, no default: signs session cookies; derives the Fernet key
    # for LLM API keys at rest.
    secret_key: str

    log_level: str = "INFO"

    # Optional first-run pre-seed of the owner account; only consumed when the
    # users table is empty, never logged, ignored once any user exists.
    bootstrap_admin_username: str | None = None
    bootstrap_admin_password: str | None = None

    # Reverse-proxy trust boundary for X-Forwarded-* headers (uvicorn
    # forwarded_allow_ips). None keeps the uvicorn default (127.0.0.1): only
    # a proxy on the loopback may set client IPs. Set explicitly (e.g. the
    # proxy's IP or subnet) when the proxy runs in a separate container.
    forwarded_allow_ips: str | None = None

    @field_validator("secret_key")
    @classmethod
    def _secret_key_strength(cls, value: str) -> str:
        """Hard boot error on weak session/Fernet keys (SERVER-03)."""
        if not value or not value.strip():
            raise ValueError(
                f"ATHENAEUM_SECRET_KEY must not be empty; generate one with: {_SECRET_KEY_FIX}"
            )
        if value == SECRET_KEY_PLACEHOLDER:
            raise ValueError(
                "ATHENAEUM_SECRET_KEY is still the shipped placeholder; "
                f"generate a real one with: {_SECRET_KEY_FIX}"
            )
        if len(value) < SECRET_KEY_MIN_LENGTH:
            raise ValueError(
                f"ATHENAEUM_SECRET_KEY must be at least {SECRET_KEY_MIN_LENGTH} "
                f"characters; generate one with: {_SECRET_KEY_FIX}"
            )
        return value


def get_settings() -> Settings:
    """Construct settings from the current environment."""
    return Settings()

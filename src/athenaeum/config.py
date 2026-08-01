"""Server-level configuration from environment variables (12-factor).

Contract: plan §3.6. All server-level configuration comes from the
environment; nothing secret is baked into code or image. LLM provider
keys stay per-user in the DB (Fernet-encrypted), never in env.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


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


def get_settings() -> Settings:
    """Construct settings from the current environment."""
    return Settings()

"""All settings come from environment variables. Nothing secret lives in the repo."""
import os


def env(name: str, default: str | None = None) -> str | None:
    return os.environ.get(name, default)


DATABASE_URL = env("DATABASE_URL")
SERVICE_ROLE = env("SERVICE_ROLE", "web")

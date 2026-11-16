import psycopg

from taw import config


def connect() -> psycopg.Connection:
    if not config.DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not set")
    return psycopg.connect(config.DATABASE_URL)

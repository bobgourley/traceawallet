"""What every chain adapter returns, and where every raw provider response goes.

Design rule 3: every HTTP response an adapter receives is handed to a RawStore
(saved, keyed by its sha256) before anything parses it.
Design rule 7: each chain sits behind the ChainAdapter interface below.

Adapters return plain observations. Prices, labels and report wording happen
elsewhere.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import pathlib
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping, Protocol, runtime_checkable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


# ---------------------------------------------------------------------------
# What an adapter returns
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TokenBalance:
    contract: str                 # the identity of the token
    amount_raw: int               # in the token's smallest unit
    decimals: int | None          # None when the token contract could not be asked
    symbol: str | None            # display only, never a key: anyone can deploy a "USDT"

    def amount(self) -> Decimal | None:
        if self.decimals is None:
            return None
        return Decimal(self.amount_raw).scaleb(-self.decimals)


@dataclass(frozen=True)
class AddressSnapshot:
    """Balances and account type at one block height."""
    chain: str
    address: str
    as_of_block: int
    as_of_time: dt.datetime       # timestamp of as_of_block, UTC
    native_symbol: str            # e.g. TRX
    native_decimals: int
    native_balance_raw: int
    token_balances: tuple[TokenBalance, ...]
    is_contract: bool
    truncated: bool               # True when token_balances is incomplete (see the adapter)
    source_slug: str
    raw_sha256: tuple[str, ...]   # every response this snapshot was built from; balance response first

    def native_balance(self) -> Decimal:
        return Decimal(self.native_balance_raw).scaleb(-self.native_decimals)


@dataclass(frozen=True)
class TransferRecord:
    """One value movement. Mirrors the transfer table.

    Addresses are strings here; the database writer turns them into
    address ids. raw_sha256 is the response the record was read from; the
    writer maps it to raw_response.id.
    """
    chain: str
    tx_hash: str
    kind: str                     # transfer_kind: native, token, internal, utxo_in, utxo_out
    event_index: int
    from_address: str | None
    to_address: str | None
    token_contract: str | None    # NULL for native and internal
    token_symbol: str | None      # display only
    token_decimals: int
    amount_raw: int
    block_number: int
    block_time: dt.datetime
    source_slug: str
    raw_sha256: str
    usd_value: Decimal | None = None          # filled in later by the price step
    price_source_slug: str | None = None

    def amount(self) -> Decimal:
        return Decimal(self.amount_raw).scaleb(-self.token_decimals)


@dataclass(frozen=True)
class TransferBatch:
    """Transfers for one address, newest first.

    truncated is True when the address has more history than `limit`; the
    batch then holds the newest `limit` transfers and older ones were not
    fetched.
    """
    chain: str
    address: str
    transfers: tuple[TransferRecord, ...]
    truncated: bool
    limit: int


@runtime_checkable
class ChainAdapter(Protocol):
    chain: str

    def fetch_snapshot(self, address: str) -> AddressSnapshot: ...

    def fetch_transfers(self, address: str, limit: int = 2000) -> TransferBatch: ...


# ---------------------------------------------------------------------------
# Raw response storage
# ---------------------------------------------------------------------------

class SecretLeak(RuntimeError):
    """Raised instead of storing a request that still contains a secret."""


class RawIntegrityError(RuntimeError):
    """A stored body no longer matches the sha256 it was saved under."""


@runtime_checkable
class RawStore(Protocol):
    def save(self, source_slug: str, request_url: str, params: Mapping[str, Any] | None,
             status: int, body_bytes: bytes) -> str:
        """Store one response body and its request. Returns the body's sha256 (hex)."""
        ...


@runtime_checkable
class ReusableRawStore(RawStore, Protocol):
    """A RawStore that can hand back a response it saved earlier.

    Adapters use this only for responses that cannot change, such as the
    events of a confirmed transaction.
    """

    def find(self, source_slug: str, request_url: str, params: Mapping[str, Any] | None) -> str | None:
        """sha256 of the newest saved HTTP 200 response to exactly this request, or None."""
        ...

    def body(self, sha: str) -> bytes:
        """The stored body, checked against its sha256."""
        ...


# Query or body fields whose names suggest a credential are dropped before storage.
_SECRET_NAME = re.compile(r"key|token|secret|password|passwd|signature|auth|credential", re.I)
# Environment variables whose values must never be stored anywhere.
_SECRET_ENV = re.compile(r"(KEY|SECRET|TOKEN|PASSWORD|SALT)", re.I)


def _env_secrets() -> list[str]:
    return [v for k, v in os.environ.items() if _SECRET_ENV.search(k) and v and len(v) >= 8]


def _drop_secret_fields(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {k: _drop_secret_fields(v) for k, v in value.items() if not _SECRET_NAME.search(str(k))}
    if isinstance(value, (list, tuple)):
        return [_drop_secret_fields(v) for v in value]
    return value


def clean_request(request_url: str, params: Mapping[str, Any] | None) -> tuple[str, dict | None]:
    """Strip credential-looking fields from the URL's query string and from params,
    then refuse outright if any secret value from the environment is still present."""
    parts = urlsplit(request_url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if not _SECRET_NAME.search(k)]
    url = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))
    clean = _drop_secret_fields(dict(params)) if params is not None else None

    blob = url + json.dumps(clean, sort_keys=True, default=str)
    for secret in _env_secrets():
        if secret in blob:
            raise SecretLeak("a secret value from the environment is in the request; not storing it")
    return url, clean


def sha256_hex(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def bucket_key_for(sha: str) -> str:
    return f"raw/{sha[:2]}/{sha}"


class LocalRawStore:
    """Saves bodies under <root>/raw/ab/<sha256> and appends one line per fetch to
    <root>/index.jsonl. For scripts and tests; the folder is gitignored."""

    def __init__(self, root: str | os.PathLike = ".raw"):
        self.root = pathlib.Path(root)

    def save(self, source_slug: str, request_url: str, params: Mapping[str, Any] | None,
             status: int, body_bytes: bytes) -> str:
        url, clean = clean_request(request_url, params)
        sha = sha256_hex(body_bytes)
        path = self.root / bucket_key_for(sha)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body_bytes)
        entry = {
            "sha256": sha, "source": source_slug, "request_url": url, "request_params": clean,
            "http_status": status, "byte_size": len(body_bytes),
            "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        }
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / "index.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, sort_keys=True) + "\n")
        return sha

    def find(self, source_slug: str, request_url: str, params: Mapping[str, Any] | None) -> str | None:
        index = self.root / "index.jsonl"
        if not index.exists():
            return None
        url, clean = clean_request(request_url, params)
        found = None
        for line in index.read_text(encoding="utf-8").splitlines():
            e = json.loads(line)
            if (e["source"] == source_slug and e["request_url"] == url
                    and e["request_params"] == clean and e["http_status"] == 200):
                found = e["sha256"]
        return found

    def body(self, sha: str) -> bytes:
        data = (self.root / bucket_key_for(sha)).read_bytes()
        if sha256_hex(data) != sha:
            raise RawIntegrityError(f"stored body {sha} does not match its hash")
        return data


class BucketRawStore:
    """Saves bodies to the S3-compatible bucket (BUCKET_* variables) and inserts one
    raw_response row per fetch. Identical bodies share one object.

    Uses the caller's database connection and does not commit: the rows land with
    whatever the caller writes from them. After save(), row_ids[sha] holds the
    raw_response.id of the most recent fetch with that body.
    """

    def __init__(self, conn, s3_client=None, bucket: str | None = None):
        self.conn = conn
        self.bucket = bucket or os.environ.get("BUCKET_NAME")
        if not self.bucket:
            raise RuntimeError("BUCKET_NAME is not set")
        self.s3 = s3_client or self._client_from_env()
        self.row_ids: dict[str, int] = {}
        self._source_ids: dict[str, int] = {}

    @staticmethod
    def _client_from_env():
        import boto3  # only needed where the bucket is used

        missing = [n for n in ("BUCKET_ENDPOINT", "BUCKET_ACCESS_KEY_ID", "BUCKET_SECRET_ACCESS_KEY")
                   if not os.environ.get(n)]
        if missing:
            raise RuntimeError(f"bucket settings missing: {', '.join(missing)}")
        return boto3.client(
            "s3",
            endpoint_url=os.environ["BUCKET_ENDPOINT"],
            aws_access_key_id=os.environ["BUCKET_ACCESS_KEY_ID"],
            aws_secret_access_key=os.environ["BUCKET_SECRET_ACCESS_KEY"],
            region_name=os.environ.get("BUCKET_REGION", "auto"),
        )

    def _source_id(self, slug: str) -> int:
        if slug not in self._source_ids:
            row = self.conn.execute("SELECT id FROM source WHERE slug = %s", (slug,)).fetchone()
            if row is None:
                raise RuntimeError(f"no source row with slug {slug!r}")
            self._source_ids[slug] = row[0]
        return self._source_ids[slug]

    def save(self, source_slug: str, request_url: str, params: Mapping[str, Any] | None,
             status: int, body_bytes: bytes) -> str:
        url, clean = clean_request(request_url, params)
        sha = sha256_hex(body_bytes)
        key = bucket_key_for(sha)
        source_id = self._source_id(source_slug)
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=body_bytes)
        row = self.conn.execute(
            "INSERT INTO raw_response (source_id, sha256, bucket_key, request_url, request_params,"
            " http_status, byte_size) VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (source_id, sha, key, url, json.dumps(clean, sort_keys=True) if clean is not None else None,
             status, len(body_bytes)),
        ).fetchone()
        self.row_ids[sha] = row[0]
        return sha

    def find(self, source_slug: str, request_url: str, params: Mapping[str, Any] | None) -> str | None:
        url, clean = clean_request(request_url, params)
        if clean is None:
            sql, args = ("SELECT id, sha256 FROM raw_response WHERE source_id = %s AND request_url = %s"
                         " AND request_params IS NULL AND http_status = 200 ORDER BY id DESC LIMIT 1",
                         (self._source_id(source_slug), url))
        else:
            sql, args = ("SELECT id, sha256 FROM raw_response WHERE source_id = %s AND request_url = %s"
                         " AND request_params = %s::jsonb AND http_status = 200 ORDER BY id DESC LIMIT 1",
                         (self._source_id(source_slug), url, json.dumps(clean, sort_keys=True)))
        row = self.conn.execute(sql, args).fetchone()
        if row is None:
            return None
        self.row_ids[row[1]] = row[0]
        return row[1]

    def body(self, sha: str) -> bytes:
        data = self.s3.get_object(Bucket=self.bucket, Key=bucket_key_for(sha))["Body"].read()
        if sha256_hex(data) != sha:
            raise RawIntegrityError(f"bucket object {bucket_key_for(sha)} does not match its hash")
        return data

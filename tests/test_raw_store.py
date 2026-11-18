import hashlib
import io
import json

import pytest

from taw.adapters.base import (BucketRawStore, LocalRawStore, RawIntegrityError, RawStore,
                               ReusableRawStore, SecretLeak, clean_request)

BODY = b'{"hello": "world"}'
SHA = hashlib.sha256(BODY).hexdigest()


def test_local_store_writes_body_and_index(tmp_path):
    store = LocalRawStore(tmp_path)
    assert isinstance(store, RawStore)
    assert store.save("trongrid", "https://api.trongrid.io/x", {"query": {"a": 1}}, 200, BODY) == SHA
    assert store.save("trongrid", "https://api.trongrid.io/x", None, 200, BODY) == SHA
    assert store.body(SHA) == BODY
    lines = [json.loads(l) for l in (tmp_path / "index.jsonl").read_text().splitlines()]
    assert len(lines) == 2                                   # two fetches, one object
    assert lines[0]["request_params"] == {"query": {"a": 1}} and lines[0]["byte_size"] == len(BODY)


def test_credential_fields_are_dropped():
    url, params = clean_request(
        "https://api.example.com/v1/x?apikey=abc&module=account&token=zzz",
        {"query": {"api_key": "abc", "address": "T1"}, "headers": {"Authorization": "Bearer q"}},
    )
    assert url == "https://api.example.com/v1/x?module=account"
    assert params == {"query": {"address": "T1"}, "headers": {}}


def test_secret_value_anywhere_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("SOME_PROVIDER_KEY", "s3cr3t-value-123")
    store = LocalRawStore(tmp_path)
    with pytest.raises(SecretLeak):
        store.save("x", "https://api.example.com/v1/s3cr3t-value-123/balance", None, 200, BODY)
    with pytest.raises(SecretLeak):
        store.save("x", "https://api.example.com/v1", {"query": {"q": "s3cr3t-value-123"}}, 200, BODY)
    assert not (tmp_path / "index.jsonl").exists()


def test_local_store_finds_saved_requests(tmp_path):
    store = LocalRawStore(tmp_path)
    assert isinstance(store, ReusableRawStore)
    url, q = "https://api.trongrid.io/v1/transactions/aa/events", {"query": {"only_confirmed": "true"}}
    assert store.find("trongrid", url, q) is None
    store.save("trongrid", url, q, 503, b"busy")
    assert store.find("trongrid", url, q) is None                  # only HTTP 200 counts
    store.save("trongrid", url, q, 200, BODY)
    assert store.find("trongrid", url, q) == SHA
    assert store.find("trongrid", url, {"query": {"only_confirmed": "false"}}) is None
    assert store.find("other", url, q) is None
    assert store.body(SHA) == BODY


def test_tampered_body_is_refused(tmp_path):
    store = LocalRawStore(tmp_path)
    store.save("trongrid", "https://x.y/z", None, 200, BODY)
    (tmp_path / "raw" / SHA[:2] / SHA).write_bytes(b"changed")
    with pytest.raises(RawIntegrityError):
        store.body(SHA)


class FakeS3:
    def __init__(self):
        self.puts = []
        self.objects = {}

    def put_object(self, **kw):
        self.puts.append(kw)
        self.objects[kw["Key"]] = kw["Body"]

    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(self.objects[Key])}


class FakeConn:
    def __init__(self):
        self.calls = []

    def execute(self, sql, params=()):
        self.calls.append((sql, params))
        result = (7,) if sql.startswith("SELECT id FROM source") else (99,)

        class R:
            def fetchone(self_inner):
                return result
        return R()


def test_bucket_store_puts_object_and_inserts_row():
    s3, conn = FakeS3(), FakeConn()
    store = BucketRawStore(conn, s3_client=s3, bucket="taw-raw")
    assert isinstance(store, RawStore)
    sha = store.save("trongrid", "https://api.trongrid.io/v1/accounts/T1?apikey=no", {"query": {"limit": 200}}, 200, BODY)
    assert sha == SHA
    assert s3.puts == [{"Bucket": "taw-raw", "Key": f"raw/{SHA[:2]}/{SHA}", "Body": BODY}]
    insert_sql, values = conn.calls[-1]
    assert insert_sql.startswith("INSERT INTO raw_response")
    assert values == (7, SHA, f"raw/{SHA[:2]}/{SHA}", "https://api.trongrid.io/v1/accounts/T1",
                      '{"query": {"limit": 200}}', 200, len(BODY))
    assert store.row_ids[SHA] == 99
    store.save("trongrid", "https://api.trongrid.io/y", None, 200, BODY)
    assert sum(1 for sql, _ in conn.calls if sql.startswith("SELECT id FROM source")) == 1   # cached


def test_bucket_store_find_and_body():
    class FindConn(FakeConn):
        def execute(self, sql, params=()):
            self.calls.append((sql, params))
            result = (7,) if sql.startswith("SELECT id FROM source") else (42, SHA)

            class R:
                def fetchone(self_inner):
                    return result
            return R()
    s3, conn = FakeS3(), FindConn()
    s3.objects[f"raw/{SHA[:2]}/{SHA}"] = BODY
    store = BucketRawStore(conn, s3_client=s3, bucket="b")
    assert isinstance(store, ReusableRawStore)
    assert store.find("trongrid", "https://api.trongrid.io/v1/x", {"query": {"only_confirmed": "true"}}) == SHA
    sql, args = conn.calls[-1]
    assert "request_params = %s::jsonb" in sql and "http_status = 200" in sql
    assert args == (7, "https://api.trongrid.io/v1/x", '{"query": {"only_confirmed": "true"}}')
    assert store.row_ids[SHA] == 42
    assert store.body(SHA) == BODY
    s3.objects[f"raw/{SHA[:2]}/{SHA}"] = b"changed"
    with pytest.raises(RawIntegrityError):
        store.body(SHA)


def test_missing_bodies_raise_raw_missing(tmp_path):
    from taw.adapters.base import RawMissing
    with pytest.raises(RawMissing):
        LocalRawStore(tmp_path).body(SHA)

    class Missing(Exception):
        response = {"Error": {"Code": "NoSuchKey"}}

    class Other(Exception):
        response = {"Error": {"Code": "AccessDenied"}}

    class S3:
        def __init__(self, exc):
            self.exc = exc

        def get_object(self, **kw):
            raise self.exc
    with pytest.raises(RawMissing):
        BucketRawStore(FakeConn(), s3_client=S3(Missing()), bucket="b").body(SHA)
    with pytest.raises(Other):                                         # anything else is not hidden
        BucketRawStore(FakeConn(), s3_client=S3(Other()), bucket="b").body(SHA)


def test_bucket_store_unknown_source():
    class NoSource(FakeConn):
        def execute(self, sql, params=()):
            class R:
                def fetchone(self_inner):
                    return None
            return R()
    with pytest.raises(RuntimeError, match="no source row"):
        BucketRawStore(NoSource(), s3_client=FakeS3(), bucket="b").save("nope", "https://x.y/z", None, 200, BODY)

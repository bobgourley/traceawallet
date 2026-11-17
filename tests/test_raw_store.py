import hashlib
import json

import pytest

from taw.adapters.base import BucketRawStore, LocalRawStore, RawStore, SecretLeak, clean_request

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


class FakeS3:
    def __init__(self):
        self.puts = []

    def put_object(self, **kw):
        self.puts.append(kw)


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


def test_bucket_store_unknown_source():
    class NoSource(FakeConn):
        def execute(self, sql, params=()):
            class R:
                def fetchone(self_inner):
                    return None
            return R()
    with pytest.raises(RuntimeError, match="no source row"):
        BucketRawStore(NoSource(), s3_client=FakeS3(), bucket="b").save("nope", "https://x.y/z", None, 200, BODY)

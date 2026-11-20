"""GoPlus: which fields are flags, the wording, the access token, the ledger, and never Bitcoin."""
import json

import httpx
import pytest

from taw.adapters import registry
from taw.adapters.base import LocalRawStore
from taw.adapters.labels import goplus
from taw.budget import BudgetExhausted, check_allowance, next_reset
from tests.test_budget import Ledger

ADDR = "0x408e41876cccdc0f92210600ef50372656052a38"
# The reply shape GoPlus gave on 2026-11-20 (all flags "0").
CLEAN = {"cybercrime": "0", "money_laundering": "0", "number_of_malicious_contracts_created": "0", "gas_abuse": "0",
         "financial_crime": "0", "darkweb_transactions": "0", "reinit": "0", "phishing_activities": "0",
         "contract_address": "1", "fake_kyc": "0", "blacklist_doubt": "0", "fake_standard_interface": "0",
         "data_source": "", "stealing_attack": "0", "blackmail_activities": "0", "sanctioned": "0",
         "malicious_mining_activities": "0", "mixer": "0", "fake_token": "0", "honeypot_related_address": "0"}


def test_contract_address_is_not_a_flag():
    assert goplus.flags(CLEAN) == []
    flagged = {**CLEAN, "phishing_activities": "1", "number_of_malicious_contracts_created": "3"}
    assert goplus.flags(flagged) == [("number_of_malicious_contracts_created", "3"), ("phishing_activities", "1")]


def test_wording_quotes_the_field_and_names_goplus_sources():
    result = {**CLEAN, "phishing_activities": "1", "data_source": "SlowMist,BlockSec"}
    [claim] = goplus.claims_from("eth", ADDR, result, "a" * 64)
    assert claim.label == 'flagged by GoPlus: phishing_activities = "1" (data source: SlowMist,BlockSec)'
    assert claim.category == "malicious_flag" and claim.claim_key == "goplus:phishing_activities"


class Server:
    def __init__(self, result=CLEAN, code=1):
        self.result, self.code, self.requests = result, code, []

    def handler(self, request):
        self.requests.append(request)
        if request.url.path == "/api/v1/token":
            return httpx.Response(200, json={"code": 1, "message": "ok",
                                             "result": {"access_token": "tok-not-real", "expires_in": 7200}})
        return httpx.Response(200, json={"code": self.code, "message": "ok" if self.code == 1 else "too many",
                                         "result": self.result if self.code == 1 else None})


def client(tmp_path, server, ledger=None, keys=("app-key-not-real", "app-secret-not-real")):
    return goplus.GoPlus(LocalRawStore(tmp_path), *keys, budget=ledger,
                         http=httpx.Client(transport=httpx.MockTransport(server.handler)), clock=lambda: 1_763_600_000)


def test_token_once_then_bearer_and_every_call_metered(tmp_path):
    server, ledger = Server({**CLEAN, "mixer": "1"}), Ledger()
    gp = client(tmp_path, server, ledger)
    claims, check, sha = gp.claims_for("tron", "TC66d59uus8AWhYDvAgWM55T8NRa7KNWAN")
    gp.claims_for("eth", ADDR)
    paths = [r.url.path for r in server.requests]
    assert paths == ["/api/v1/token", "/api/v1/address_security/TC66d59uus8AWhYDvAgWM55T8NRa7KNWAN",
                     f"/api/v1/address_security/{ADDR}"]
    assert server.requests[1].headers["authorization"] == "tok-not-real"              # bare: Bearer is refused
    assert dict(server.requests[1].url.params) == {"chain_id": "tron"} and server.requests[2].url.params["chain_id"] == "1"
    assert len(ledger.calls) == 3 and all(c == 1 for c, _ in ledger.calls)
    assert [c.label for c in claims] == ['flagged by GoPlus: mixer = "1"'] and check["flags"] == ["mixer"]
    stored = (tmp_path / "index.jsonl").read_text()
    assert "tok-not-real" not in stored and "app-secret" not in stored               # neither token nor key kept
    body = LocalRawStore(tmp_path).body(sha)
    assert json.loads(body)["result"] == {**CLEAN, "mixer": "1"}                       # the reply, unmodified


def test_without_keys_no_token_is_asked_for(tmp_path):
    server = Server()
    client(tmp_path, server, keys=(None, None)).claims_for("eth", ADDR)
    assert [r.url.path for r in server.requests] == [f"/api/v1/address_security/{ADDR}"]
    assert "authorization" not in server.requests[0].headers


def test_an_error_code_is_an_error_and_still_stored(tmp_path):
    with pytest.raises(goplus.GoPlusError, match="code 4029"):
        client(tmp_path, Server(code=4029)).claims_for("eth", ADDR)
    assert "address_security" in (tmp_path / "index.jsonl").read_text()


def test_never_bitcoin(tmp_path):
    assert not registry.goplus_allowed("btc")
    with pytest.raises(ValueError):
        client(tmp_path, Server()).claims_for("btc", "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa")


def test_goplus_has_its_own_ledger_wording():
    with pytest.raises(BudgetExhausted, match="GoPlus's daily allowance is used up") as err:
        check_allowance("worker", 1, {"worker": 1000}, None, 1000, next_reset(), "goplus")
    assert err.value.provider == "GoPlus"
    with pytest.raises(BudgetExhausted, match=r"300 calls"):
        check_allowance("scripts", 1, {"scripts": 300}, None, 1000, next_reset(), "goplus")

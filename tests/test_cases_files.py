"""The ground-truth case files hold on-chain facts only, with their source, and valid addresses."""
import json
import pathlib
import re

import pytest

from taw.adapters.chains.btc_address import normalize as btc_normalize
from taw.adapters.chains.evm_address import normalize as evm_normalize
from taw.adapters.chains.tron_address import to_base58 as tron_normalize

CASES = sorted((pathlib.Path(__file__).parent / "cases").glob("*/expected.json"))
CHECK_KINDS = {"token_transfer", "token_transfers_total", "token_balance_at", "token_balance_before",
               "token_balance_reached", "balance_now", "btc_balance_in_month", "activated_on",
               "activated_by_one_address", "token_received_run", "received_share"}


def test_there_are_cases():
    assert len(CASES) == 5


@pytest.mark.parametrize("path", CASES, ids=[p.parent.name for p in CASES])
def test_case_file(path):
    case = json.loads(path.read_text())
    for key in ("court", "docket", "filed", "links", "document_sha256", "addresses", "facts"):
        assert case[key], key
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", case["filed"])
    assert re.fullmatch(r"[0-9a-f]{64}", case["document_sha256"])
    for a in case["addresses"]:
        if a["chain"] == "tron":
            assert tron_normalize(a["address"]) == a["address"]                     # base58check holds
        else:
            (btc_normalize if a["chain"] == "btc" else evm_normalize)(a["address"])  # checksum holds
        assert a["paragraphs"]
    printed = {a["address"].lower() for a in case["addresses"]}
    ids = [f["id"] for f in case["facts"]]
    assert len(ids) == len(set(ids))
    for fact in case["facts"]:
        assert fact["paragraph"] and fact["quote"].strip(), fact["id"]
        assert (fact["check"] is None) == bool(fact.get("not_checkable_reason")), fact["id"]
        if fact["check"]:
            c = fact["check"]
            assert c["kind"] in CHECK_KINDS, fact["id"]
            for role in ("from", "to", "address"):
                if c.get(role):          # every address a check uses is one the complaint prints in full
                    assert c[role].lower() in printed, (fact["id"], role)
            for a in c.get("addresses", []):
                assert a.lower() in printed, (fact["id"], a)
            if c.get("from_prefix"):     # a partly printed address is matched by its prefix, never completed
                assert not any(p.startswith(c["from_prefix"].lower()) for p in printed), fact["id"]
            for ref in (c.get("after"), c.get("before")):
                if ref:
                    assert ids.index(ref) < ids.index(fact["id"]), fact["id"]
    for d in case.get("discrepancies", []):
        # A figure no on-chain measure reproduces: both numbers, what was tried, no verdict on the complaint.
        assert d["fact"] in ids and d["address"].lower() in printed, d["id"]
        assert d["complaint_figure_btc"] and d["closest_chain_figure_btc"] and d["measures_tested"], d["id"]
        assert not re.search(r"\b(wrong|error|mistake|incorrect)", json.dumps(d).lower()), d["id"]


@pytest.mark.parametrize("path", CASES, ids=[p.parent.name for p in CASES])
def test_no_file_asserts_a_scam(path):
    """Complaints state allegations; these files hold on-chain facts only."""
    folder = path.parent
    for f in (folder / "expected.json", folder / "case.md"):
        assert "scam" not in f.read_text().lower(), f

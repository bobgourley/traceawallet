"""Record the report builder's fixtures: one address per ground-truth case.

Step 1, chain data, from a laptop with the worker's settings (no database needed):

    railway run --service traceawallet-worker -- python scripts/record_report_fixtures.py chain [case ...]

  Tron through TronGrid, Bitcoin through mempool.space, Ethereum through Etherscan
  (tests and cross-checks only, docs/DECISIONS.md 2026-11-19). No Blockscout calls.
  GoPlus is asked about the address itself (not Bitcoin), as the worker would.
  Responses go to .raw/report-fixtures/<case>/ (not committed); what the adapters
  returned goes to tests/fixtures/reports/<case>/chain.json.gz.

Step 2, our own tables, inside Railway (reads only):

    python scripts/record_report_fixtures.py stored-script > /tmp/export.py
    railway ssh --service traceawallet-worker -- 'python -' < /tmp/export.py > /tmp/stored.json
    python scripts/record_report_fixtures.py stored /tmp/stored.json

  Labels on each address and on its first-hop counterparties (the local sources
  only, as the worker applies them), the blacklist index's events and coverage for
  each address, and the source rows. Nothing about who looked anything up.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests import report_fixtures as rf  # noqa: E402

RAW = ROOT / ".raw" / "report-fixtures"


def adapter_for(chain: str, raw_store):
    if chain == "tron":
        from taw.adapters.chains.tron_trongrid import TronGridAdapter
        return TronGridAdapter(raw_store, os.environ["TRONGRID_API_KEY"])
    if chain == "btc":
        from taw.adapters.chains.btc_esplora import BtcEsploraAdapter
        return BtcEsploraAdapter(raw_store)
    from taw.adapters.chains.evm_etherscan import EtherscanAdapter
    return EtherscanAdapter(chain, raw_store, os.environ["ETHERSCAN_API_KEY"])


def freeze_status(chain: str, address: str, block: int, adapter) -> list[dict]:
    """The contracts' own answer. The date is filled in later from the stored index."""
    from taw.adapters.labels import freeze
    out = []
    for c in freeze.contracts_for(chain):
        if chain == "tron":
            out_hex, sha = adapter._constant_call(c.contract, f"{c.function}(address)",
                                                  freeze._pad(freeze.to_hex(address)[2:]))
            block_read = None
        else:
            out_hex, sha = adapter._eth_call(c.contract, freeze._SEL[c.function] + freeze._pad(address), hex(block))
            block_read = block
        out.append({"contract": c.contract, "token": c.token, "frozen": freeze._word_bool(out_hex),
                    "block": block_read, "raw_sha256": sha})
    return out


def record_chain(case: str) -> None:
    from taw.adapters.base import LocalRawStore
    from taw.adapters.registry import goplus_allowed
    chain, address = rf.CASES[case]
    raw_store = LocalRawStore(RAW / case)
    adapter = adapter_for(chain, raw_store)
    snap = adapter.fetch_snapshot(address)
    batch = adapter.fetch_transfers(snap.address, limit=2000)
    data = {"case": case, "chain": chain, "address": snap.address,
            "snapshot": rf.snapshot_json(snap), "batch": rf.batch_json(batch),
            "freeze": freeze_status(chain, snap.address, snap.as_of_block, adapter) if chain != "btc" else [],
            "goplus": None, "transactions": []}
    if goplus_allowed(chain):
        from taw.adapters.labels.goplus import GoPlus
        gp = GoPlus(raw_store, os.environ.get("GOPLUS_APP_KEY"), os.environ.get("GOPLUS_APP_SECRET"))
        claims, check, sha = gp.claims_for(chain, snap.address)
        data["goplus"] = {"result": "flags" if claims else "no_flags", "detail": check, "raw_sha256": sha,
                          "claims": [{"category": c.category, "label": c.label, "claim_url": c.claim_url}
                                     for c in claims]}
    for tx in rf.TXS.get(case, []):
        data["transactions"].append(rf.fetch_json(adapter.fetch_transaction(tx)))
    path = rf.write_chain(case, data)
    print(f"{case}: block {snap.as_of_block:,}, {len(batch.transfers)} transfers"
          f"{' (truncated)' if batch.truncated else ''}; {path.stat().st_size:,} bytes; "
          f"{getattr(adapter, 'request_count', '?')} requests", flush=True)


EXPORT = r'''
import json, os, psycopg
WANT = __WANT__
LOCAL = ("ofac-sdn", "exchange-por", "okx-por")
c = psycopg.connect(os.environ["DATABASE_URL"])
def labels(chain, addresses, sources):
    if not addresses:
        return {}
    sql = ("SELECT a.address, s.slug, l.category, l.label, l.claim_url, l.source_date::text,"
           " to_char(l.observed_at AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"'), l.status::text"
           " FROM label_current l JOIN address a ON a.id = l.address_id JOIN source s ON s.id = l.source_id"
           " WHERE a.chain = %s AND a.address = ANY(%s::text[])")
    params = [chain, addresses]
    if sources:
        sql += " AND s.slug = ANY(%s::text[])"
        params.append(list(sources))
    out = {}
    for addr, slug, cat, lab, url, sdate, obs, status in c.execute(sql + " ORDER BY a.address, s.slug, l.id", params):
        out.setdefault(addr, []).append({"source": slug, "category": cat, "label": lab, "claim_url": url,
                                         "source_date": sdate, "observed_at": obs})
    return out
result = {}
for case, (chain, address, parties) in WANT.items():
    own = labels(chain, [address], None).get(address, [])
    own = [l for l in own if l["source"] in LOCAL]
    events = [dict(zip(("contract", "token", "action", "event_name", "block", "time", "tx_hash", "log_index",
                        "amount_raw"), r)) for r in c.execute(
        "SELECT contract, token, action::text, event_name, block_number, to_char(block_time AT TIME ZONE 'UTC',"
        " 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"'), tx_hash, log_index, amount_raw::text FROM blacklist_event"
        " WHERE chain = %s AND address = %s ORDER BY block_number, log_index", (chain, address))]
    coverage = {r[0]: r[1] for r in c.execute(
        "SELECT contract, max(to_block) FROM blacklist_scan WHERE chain = %s GROUP BY contract", (chain,))}
    result[case] = {"labels": own, "counterparty_labels": labels(chain, parties, LOCAL),
                    "blacklist_events": events, "blacklist_coverage": coverage}
result["_list_dates"] = {r[0]: str(r[1]) for r in c.execute(
    "SELECT s.slug, max(l.source_date) FROM label_load l JOIN source s ON s.id = l.source_id GROUP BY s.slug")}
result["_sources"] = {r[0]: {"name": r[1], "attribution": r[2], "url": r[3], "kind": r[4]} for r in c.execute(
    "SELECT slug, name, attribution, url, kind::text FROM source ORDER BY slug")}
print(json.dumps(result, sort_keys=True))
'''


def stored_script() -> str:
    from taw.labels import counterparties
    want = {}
    for case, (chain, address) in rf.CASES.items():
        snap, batch, _ = rf.chain_parts(case)
        parties = sorted(counterparties(chain, snap.address, batch.transfers))
        if chain != "tron":
            parties = [p.lower() if p.startswith("0x") or p.lower().startswith("bc1") else p for p in parties]
        want[case] = (chain, snap.address, parties)
    return EXPORT.replace("__WANT__", repr(want))


def write_stored(path: str) -> None:
    data = json.loads(pathlib.Path(path).read_text())
    sources = data.pop("_sources")
    list_dates = data.pop("_list_dates")
    for case, d in data.items():
        d["sources"], d["list_dates"] = sources, list_dates
        out = rf.FIXTURES / case / "stored.json"
        out.write_text(json.dumps(d, sort_keys=True, indent=1) + "\n")
        print(f"{case}: {len(d['labels'])} label(s) on the address, {len(d['counterparty_labels'])} labelled "
              f"counterpart(ies), {len(d['blacklist_events'])} blacklist event(s)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("step", choices=("chain", "stored-script", "stored"))
    ap.add_argument("args", nargs="*")
    a = ap.parse_args()
    if a.step == "chain":
        for case in a.args or list(rf.CASES):
            record_chain(case)
    elif a.step == "stored-script":
        print(stored_script())
    else:
        write_stored(a.args[0])
    return 0


if __name__ == "__main__":
    sys.exit(main())

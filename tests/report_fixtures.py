"""Fixtures for the report builder: one address per ground-truth case.

Each case folder under tests/fixtures/reports/ holds:

  chain.json.gz   what the chain adapters returned (snapshot, transfers, freeze
                  status, GoPlus's answer, transactions read by hash), written by
                  scripts/record_report_fixtures.py. Every record keeps the sha256 of
                  the provider response it came from; the bodies stay out of the repo.
  stored.json     what our own tables held for the address and its first-hop
                  counterparties (labels, blacklist index events and coverage,
                  source rows), exported from production by the same script.
  report.json     the golden report data, and report.txt the golden rendered text.

`case_input(name)` turns the first two into a ReportInput exactly as the database
loader would, running the same completeness checks the worker runs.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import gzip
import json
import pathlib
from decimal import Decimal

from taw.adapters.base import (AddressSnapshot, CheckResult, TokenBalance, TransactionFetch, TransferBatch,
                               TransferRecord)

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "reports"

CASES = {
    "sdny-1-26-cv-08010-usdt": ("tron", "TGuqbYqJ4m1J8dKWB3NVnsuXtgXCQkgdyf"),
    "ddc-1-25-cv-02967-usdt": ("tron", "TC66d59uus8AWhYDvAgWM55T8NRa7KNWAN"),
    "edny-1-25-cv-05745-btc": ("btc", "39B6oSa58qNpFMGpuowtRHAYp3fM4ghXRq"),
    "wdpa-2-26-cv-00087-usdt": ("eth", "0xfa0a50dc366e2f0a5381e4d4b5fede589ab633a9"),
    "ddc-1-25-cv-01907-usdt-gb": ("eth", "0xc76afbf0f69ae9be5d855239c50673252cf3f26b"),
    # Not a court case: a very busy Bitcoin address from live testing (2026-11-20), 86,570
    # transactions, which gets a summary-only report.
    "busy-btc-summary-only": ("btc", "bc1q8yja3gw33ngd8aunmfr4hj820adc9nlsv0syvz"),
}

# Transactions read directly by hash, for the matching tests. W.D. Pa. paragraph 16:
# A.T.'s Coinbase transaction of 242,209.53 USDC to 0xFA0a...; the chain shows
# 242,209.413589 USDC arriving in this transaction.
CASE3_VICTIM_TX = "0x60b23beb484818bbfa920db5e2846cf4746dd60e7b4b5c98092aae8fcb340781"
TXS = {"wdpa-2-26-cv-00087-usdt": [CASE3_VICTIM_TX]}


# -- dataclasses to JSON and back ---------------------------------------------------------

def _enc(v):
    if isinstance(v, dt.datetime):
        return {"$dt": v.isoformat()}
    if isinstance(v, Decimal):
        return {"$dec": str(v)}
    if isinstance(v, int) and not isinstance(v, bool) and abs(v) > 2 ** 53:
        return {"$int": str(v)}
    if isinstance(v, (list, tuple)):
        return [_enc(x) for x in v]
    if isinstance(v, dict):
        return {k: _enc(x) for k, x in v.items()}
    return v


def _dec(v):
    if isinstance(v, dict):
        if set(v) == {"$dt"}:
            return dt.datetime.fromisoformat(v["$dt"])
        if set(v) == {"$dec"}:
            return Decimal(v["$dec"])
        if set(v) == {"$int"}:
            return int(v["$int"])
        return {k: _dec(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_dec(x) for x in v]
    return v


def _plain(obj) -> dict:
    return {f.name: getattr(obj, f.name) for f in dataclasses.fields(obj)}


def snapshot_json(s: AddressSnapshot) -> dict:
    d = _plain(s)
    d["token_balances"] = [_plain(b) for b in s.token_balances]
    return _enc(d)


def snapshot_from(d: dict) -> AddressSnapshot:
    d = _dec(d)
    d["token_balances"] = tuple(TokenBalance(**b) for b in d["token_balances"])
    for k in ("raw_sha256", "balances_read"):
        if d.get(k) is not None:
            d[k] = tuple(d[k])
    return AddressSnapshot(**d)


def record_json(r: TransferRecord) -> dict:
    return _enc(_plain(r))


def record_from(d: dict) -> TransferRecord:
    return TransferRecord(**_dec(d))


def batch_json(b: TransferBatch) -> dict:
    d = _plain(b)
    d["transfers"] = [_plain(t) for t in b.transfers]
    d["checks"] = [_plain(c) for c in b.checks]
    return _enc(d)


def batch_from(d: dict) -> TransferBatch:
    d = _dec(d)
    d["transfers"] = tuple(TransferRecord(**t) for t in d["transfers"])
    d["checks"] = tuple(CheckResult(**c) for c in d["checks"])
    if d.get("internal_range") is not None:
        d["internal_range"] = tuple(d["internal_range"])
    return TransferBatch(**d)


def fetch_json(f: TransactionFetch) -> dict:
    d = _plain(f)
    d["records"] = [_plain(r) for r in f.records]
    return _enc(d)


def fetch_from(d: dict) -> TransactionFetch:
    d = _dec(d)
    d["records"] = tuple(TransferRecord(**r) for r in d["records"])
    d["raw_sha256"] = tuple(d["raw_sha256"])
    return TransactionFetch(**d)


def write_chain(case: str, data: dict) -> pathlib.Path:
    folder = FIXTURES / case
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "chain.json.gz"
    raw = json.dumps(data, sort_keys=True, indent=0).encode()
    path.write_bytes(gzip.compress(raw, mtime=0))
    return path


def read_chain(case: str) -> dict:
    return json.loads(gzip.decompress((FIXTURES / case / "chain.json.gz").read_bytes()))


def read_stored(case: str) -> dict:
    return json.loads((FIXTURES / case / "stored.json").read_text())


def chain_parts(case: str) -> tuple[AddressSnapshot, TransferBatch, dict]:
    d = read_chain(case)
    return snapshot_from(d["snapshot"]), batch_from(d["batch"]), d


# -- a ReportInput from the fixtures, as the worker and the loader would build it ----------------

def _when(s: str | None) -> dt.datetime | None:
    return None if s is None else dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def freeze_checks(chain: str, address: str, recorded: list[dict], stored: dict):
    """The worker's freeze checks: the contract's answer from chain.json, the date from
    the blacklist index as exported in stored.json (freeze._from_index)."""
    from taw.adapters.labels import freeze
    from taw.blacklist import FreezeFound, IndexedEvent

    def index(contract, addr, block):
        cov = stored["blacklist_coverage"].get(contract.contract)
        if cov is None:
            return FreezeFound(None, None)
        last = None
        for e in stored["blacklist_events"]:
            if e["contract"] != contract.contract or (block is not None and e["block"] > block):
                continue
            if e["action"] == "freeze":
                last = IndexedEvent("freeze", e["block"], _when(e["time"]), e["tx_hash"], e["log_index"], None)
            elif e["action"] == "unfreeze":
                last = None
        return FreezeFound(last, cov)

    out = []
    by_contract = {c.contract: c for c in freeze.contracts_for(chain)}
    for r in recorded:
        c = by_contract[r["contract"]]
        if r["frozen"] and not c.date_readable:
            chk = freeze._from_index(c, address, True, r["block"], r["raw_sha256"], index, None)
        else:
            chk = freeze.FreezeCheck(c, address, r["frozen"], r["block"], r["raw_sha256"],
                                     date_note="" if not r["frozen"] else "not read in this fixture")
        out.append(chk)
    return out


def case_input(case: str):
    """(ReportInput, snapshot, batch, Completeness) for a case."""
    from taw.adapters.registry import stablecoins_for
    from taw.reconcile import StablecoinContext, run_checks
    from taw.report.build import CHAIN_NAMES
    from taw.report.inputs import ReportInput, snapshot_dict

    snap, batch, chain = chain_parts(case)
    stored = read_stored(case)
    fetched = snap.as_of_time + dt.timedelta(seconds=30)        # the worker saves right after the read
    checks = freeze_checks(snap.chain, snap.address, chain["freeze"], stored)
    burns: dict = {}
    for e in stored["blacklist_events"]:
        if e["action"] == "destroy" and e["block"] <= snap.as_of_block:
            burns.setdefault(e["contract"], []).append(
                {"tx_hash": e["tx_hash"], "log_index": e["log_index"], "block": e["block"], "time": e["time"],
                 "amount_raw": e["amount_raw"]})
    ctx = StablecoinContext({c.contract.contract: c.frozen for c in checks},
                            {k: tuple(v) for k, v in burns.items()},
                            {k: v for k, v in stored["blacklist_coverage"].items()})
    recon = run_checks(snap, batch, stablecoins_for(snap.chain), ctx if checks else None)

    observed = fetched + dt.timedelta(seconds=5)
    label_checks = [{"source": c.contract.source, "subject": c.contract.contract, "block": c.block,
                     "result": c.result, "detail": {**c.detail(), "also_read": list(c.extra_sha256)},
                     "observed_at": observed} for c in checks]
    labels = [{**l, "observed_at": _when(l["observed_at"]),
               "source_date": dt.date.fromisoformat(l["source_date"]) if l["source_date"] else None}
              for l in stored["labels"]]
    for c in checks:
        claim = c.claim()
        if claim:
            labels.append({"source": c.contract.source, "category": claim.category, "label": claim.label,
                           "claim_url": claim.claim_url, "source_date": claim.source_date, "observed_at": observed})
    gp = chain.get("goplus")
    if gp:
        label_checks.append({"source": "goplus", "subject": "address_security", "block": None,
                             "result": gp["result"], "detail": gp["detail"], "observed_at": observed})
        for cl in gp["claims"]:
            labels.append({"source": "goplus", "category": cl["category"], "label": cl["label"],
                           "claim_url": cl["claim_url"], "source_date": None, "observed_at": observed})
    parties = {a: tuple({**l, "observed_at": _when(l["observed_at"]),
                         "source_date": dt.date.fromisoformat(l["source_date"]) if l["source_date"] else None}
                        for l in ls) for a, ls in stored["counterparty_labels"].items()}
    tokens = [{"contract": b.contract, "amount_raw": b.amount_raw, "decimals": b.decimals, "symbol": b.symbol}
              for b in snap.token_balances]
    snapshot = snapshot_dict(
        as_of_block=snap.as_of_block, as_of_time=snap.as_of_time, fetched_at=fetched,
        native_balance_raw=snap.native_balance_raw, native_decimals=snap.native_decimals, token_balances=tokens,
        is_contract=snap.is_contract, tokens_truncated=snap.truncated, transfer_count=len(batch.transfers),
        transfers_truncated=batch.truncated, token_reconciliation=[t.as_json() for t in recon.tokens],
        history_incomplete=recon.history_incomplete, incomplete_reasons=list(recon.incomplete_reasons),
        checks=[c.as_json() for c in recon.checks],
        internal_from_block=batch.internal_range[0] if batch.internal_range else None,
        internal_to_block=batch.internal_range[1] if batch.internal_range else None)
    explorer = {"eth": "https://etherscan.io", "tron": "https://tronscan.org", "btc": "https://mempool.space"}
    family = {"eth": "evm"}.get(snap.chain, snap.chain)
    inp = ReportInput(
        chain=snap.chain, chain_name=CHAIN_NAMES[snap.chain], family=family, explorer_url=explorer[snap.chain],
        address=snap.address, snapshot=snapshot, source=snap.source_slug,
        transfers=tuple(t for t in batch.transfers if t.block_number <= snap.as_of_block),
        labels=tuple(labels), counterparty_labels=parties, label_checks=tuple(label_checks), prices={},
        sources=stored["sources"], labels_as_of=observed + dt.timedelta(seconds=5), snapshot_id=None,
        list_dates={k: dt.date.fromisoformat(v) for k, v in stored["list_dates"].items()})
    return inp, snap, batch, recon

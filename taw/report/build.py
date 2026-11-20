"""report.data from a ReportInput: structured facts, codes and numbers, no prose.

The wording lives in templates/ (render.py). Everything here is deterministic: the
same input gives the same data, ordered the same way, so the hash is stable.

Values: amounts are exact decimal strings with their raw integer beside them, USD
figures are strings rounded to cents, times are ISO 8601 UTC strings. A missing
price is null with a reason, never zero. Nothing here judges an address.

Sets apart, and lists separately (neutral, not "spam"):
  zero-value transfers      amount 0
  tiny transfers            a recognised asset below TINY (1 USDT, USDC or DAI; 1 TRX;
                            0.0005 ETH; 0.00001 BTC)
  unrecognised tokens       any token contract not in our list of priced or freezable
                            contracts: its name and symbol are whatever its deployer set
These are left out of the first-hop lists so the largest real movements lead.
"""
from __future__ import annotations

import datetime as dt
import unicodedata
from collections import defaultdict
from decimal import ROUND_HALF_UP, Decimal

from taw.adapters.labels import freeze
from taw.adapters.prices.assets import TOKENS
from taw.adapters.prices.chainlink import bucket_for
from taw.adapters.registry import EVM_NOT_CHECKED, PHASE1_EVM_CHECKED
from taw.report.inputs import ReportInput, asset_of

CHAIN_NAMES = {"eth": "Ethereum", "bsc": "BNB Smart Chain", "polygon": "Polygon", "arbitrum": "Arbitrum One",
               "optimism": "OP Mainnet", "base": "Base", "tron": "Tron", "btc": "Bitcoin"}
EVM_ORDER = ("eth", "arbitrum", "optimism", "bsc", "polygon", "base")
TINY = {"USDT": Decimal("1"), "USDC": Decimal("1"), "DAI": Decimal("1"), "TRX": Decimal("1"),
        "ETH": Decimal("0.0005"), "BTC": Decimal("0.00001")}
ASSET_ORDER = ("USDT", "USDC", "DAI", "BTC", "ETH", "TRX")
FIRST_HOP_ROWS = 20
SET_APART_ROWS = 25
UNRECOGNISED_ROWS = 15
NEXT_ROWS = 10

# What each chain's adapter does not read at all (worded in the template).
NOT_READ = {
    "tron": ["tron_trc10", "tron_internal"],
    "btc": ["btc_unconfirmed"],
    "evm": ["evm_nft"],
}


# -- small helpers ------------------------------------------------------------------------------

def iso(t: dt.datetime | None) -> str | None:
    if t is None:
        return None
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def amount_str(raw: int, decimals: int | None) -> str | None:
    if decimals is None:
        return None
    d = Decimal(raw).scaleb(-decimals)
    s = format(d, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


def usd_str(v: Decimal | None) -> str | None:
    return None if v is None else str(v.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def odd_characters(text: str | None) -> list[str]:
    """Characters in a token's name or symbol a reader could mistake or not see:
    anything outside printable ASCII, as U+XXXX codes."""
    if not text:
        return []
    out = []
    for ch in text:
        if not (" " <= ch <= "~"):
            code = f"U+{ord(ch):04X}"
            if code not in out:
                out.append(code)
    return out


def visible(text: str | None) -> str | None:
    """The text with invisible and control characters written as <U+XXXX>."""
    if text is None:
        return None
    return "".join(f"<U+{ord(c):04X}>" if unicodedata.category(c) in ("Cc", "Cf", "Co", "Cn", "Zl", "Zp")
                   else c for c in text)


class Ctx:
    """Per-build lookups: address comparison, recognised tokens, prices."""

    def __init__(self, inp: ReportInput):
        self.inp = inp
        self.chain = inp.chain
        self.now = inp.snapshot["fetched_at"]
        self.me = self.norm(inp.address)
        self.freezable = {c.contract if self.chain == "tron" else c.contract.lower(): c
                          for c in freeze.contracts_for(self.chain)}
        self.unpriced: set[str] = set()

    def norm(self, a: str | None) -> str | None:
        if a is None:
            return None
        return a if self.chain == "tron" else a.lower()

    def is_me(self, a: str | None) -> bool:
        return a is not None and self.norm(a) == self.me

    def key(self, contract: str | None) -> str:
        return "native" if contract is None else self.norm(contract)

    def recognised(self, contract: str | None) -> bool:
        if contract is None:
            return True
        k = self.norm(contract)
        return (self.chain, k) in TOKENS or k in self.freezable

    def asset(self, contract: str | None) -> str | None:
        return asset_of(self.chain, contract)

    def price(self, contract: str | None, when: dt.datetime) -> tuple[Decimal | None, str]:
        """(USD per unit, reason code). Reason: priced, no_feed, not_read, stale, invalid."""
        a = self.asset(contract)
        if a is None:
            return None, "no_feed"
        start, size = bucket_for(when, self.now)
        row = self.inp.prices.get((a, start, size))
        if row is None:
            self.unpriced.add(a)
            return None, "not_read"
        if row["status"] != "ok" or row["usd"] is None:
            return None, row["status"]
        return row["usd"], "priced"


def _labels_out(labels, sources) -> list[dict]:
    out = []
    for l in labels:
        src = sources.get(l["source"], {})
        out.append({"source": l["source"], "source_name": src.get("name", l["source"]),
                    "attribution": src.get("attribution"), "category": l["category"], "label": visible(l["label"]),
                    "claim_url": l.get("claim_url"),
                    "source_date": str(l["source_date"]) if l.get("source_date") else None,
                    "observed_at": iso(l["observed_at"]) if isinstance(l.get("observed_at"), dt.datetime)
                    else l.get("observed_at")})
    return sorted(out, key=lambda x: (x["source"], x["category"], x["label"]))


# -- sections -----------------------------------------------------------------------------------

def used_for(slug: str, kind: str | None, inp: ReportInput) -> str:
    """A code for what a source contributed (worded in the template)."""
    if slug == inp.source:
        return "chain_data"
    if slug in ("usdt-contract", "usdc-contract"):
        return "freeze_status"
    if slug == "goplus":
        return "goplus_flags"
    if slug == "ofac-sdn":
        return "sanctions_list"
    if slug in ("exchange-por", "okx-por"):
        return "exchange_list"
    if slug == "chainlink":
        return "prices"
    return kind or "other"


def _header(inp: ReportInput, c: Ctx, used_sources: set[str]) -> dict:
    snap = inp.snapshot
    src = inp.sources.get(inp.source, {})
    coverage = None
    if inp.family == "evm":
        coverage = {
            "this_chain": inp.chain,
            "checked": [{"chain": ch, "name": CHAIN_NAMES[ch]} for ch in EVM_ORDER if ch in PHASE1_EVM_CHECKED],
            "not_checked": [{"chain": ch, "name": CHAIN_NAMES[ch], "reason": EVM_NOT_CHECKED[ch]}
                            for ch in EVM_ORDER if ch in EVM_NOT_CHECKED],
        }
    sources = []
    for slug in sorted(used_sources | {inp.source}, key=lambda x: (x != inp.source, x)):
        s = inp.sources.get(slug, {})
        sources.append({"slug": slug, "name": s.get("name", slug), "used_for": used_for(slug, s.get("kind"), inp),
                        "url": s.get("url")})
    return {"chain": inp.chain, "chain_name": inp.chain_name, "address": inp.address,
            "as_of_block": snap["as_of_block"], "as_of_time": iso(snap["as_of_time"]),
            "fetched_at": iso(snap["fetched_at"]), "labels_as_of": iso(inp.labels_as_of),
            "chain_source": {"slug": inp.source, "name": src.get("name", inp.source),
                             "attribution": src.get("attribution")},
            "sources": sources, "evm_coverage": coverage, "is_contract": snap["is_contract"]}


def _freeze_status(inp: ReportInput, c: Ctx) -> dict[str, dict]:
    """Per freezable contract: the newest label_check answer, with the freeze date."""
    out = {}
    checks = {(k["source"], c.norm(k["subject"])): k for k in inp.label_checks}
    recon = {c.norm(t["contract"]): t for t in inp.snapshot.get("token_reconciliation") or []}
    for key, fc in c.freezable.items():
        k = checks.get((fc.source, key))
        det = (k or {}).get("detail") or {}
        t = recon.get(key) or {}
        out[key] = {
            "token": fc.token, "issuer": fc.issuer, "contract": fc.contract, "function": fc.function,
            "status": k["result"] if k else "not_checked",
            "block": k.get("block") if k else None,
            "read_at": iso(k["observed_at"]) if k and isinstance(k.get("observed_at"), dt.datetime) else None,
            "since": det.get("frozen_since"), "since_block": det.get("frozen_since_block"),
            "since_tx": det.get("frozen_since_tx"), "date_note": det.get("date_note") or None,
            "destructions": [{"tx_hash": d["tx_hash"], "block": d["block"], "time": d.get("time"),
                              "amount_raw": str(d["amount_raw"]), "amount": amount_str(int(d["amount_raw"]), 6)}
                             for d in t.get("destructions") or []],
        }
    return out


def _holdings(inp: ReportInput, c: Ctx, freezes: dict) -> dict:
    snap = inp.snapshot
    when = snap["as_of_time"]
    native_symbol = {"tron": "TRX", "btc": "BTC"}.get(inp.chain, "ETH")
    price, why = c.price(None, when)
    native_amount = Decimal(snap["native_balance_raw"]).scaleb(-snap["native_decimals"])
    native = {"symbol": native_symbol, "amount_raw": str(snap["native_balance_raw"]),
              "amount": amount_str(snap["native_balance_raw"], snap["native_decimals"]),
              "decimals": snap["native_decimals"], "usd": usd_str(native_amount * price) if price else None,
              "price_status": why}
    tokens = []
    for b in sorted(snap["token_balances"], key=lambda b: (not c.recognised(b["contract"]), c.norm(b["contract"]))):
        key = c.norm(b["contract"])
        dec = b.get("decimals")
        p, pwhy = c.price(b["contract"], when) if dec is not None else (None, "no_decimals")
        amt = Decimal(b["amount_raw"]).scaleb(-dec) if dec is not None else None
        fz = freezes.get(key)
        tokens.append({"contract": b["contract"], "symbol": visible(b.get("symbol")),
                       "asset": c.asset(b["contract"]), "recognised": c.recognised(b["contract"]),
                       "odd_characters": odd_characters(b.get("symbol")),
                       "amount_raw": str(b["amount_raw"]), "decimals": dec, "amount": amount_str(b["amount_raw"], dec),
                       "usd": usd_str(amt * p) if (p is not None and amt is not None) else None, "price_status": pwhy,
                       "freezable": fz is not None, "issuer": fz["issuer"] if fz else None})
    held = [native] if snap["native_balance_raw"] > 0 else []
    held += [t for t in tokens if t["recognised"] and int(t["amount_raw"]) > 0]
    freezable = [t for t in tokens if t["freezable"] and int(t["amount_raw"]) > 0]
    share = None
    share_status = "nothing_held"
    if held:
        if all(h["usd"] is not None for h in held):
            total = sum(Decimal(h["usd"]) for h in held)
            frz = sum(Decimal(t["usd"]) for t in freezable)
            share = str((frz * 100 / total).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)) if total else None
            share_status = "computed" if total else "zero_value"
        else:
            share_status = "missing_prices"
    ofac = [l for l in inp.labels if l["source"] == "ofac-sdn"]
    return {"native": native, "tokens": tokens, "tokens_truncated": snap["tokens_truncated"],
            "unrecognised_count": sum(1 for t in tokens if not t["recognised"]),
            "freezable": {"held": [{"token": t["symbol"], "contract": t["contract"], "amount": t["amount"],
                                    "issuer": t["issuer"]} for t in freezable],
                          "share_percent": share, "share_status": share_status,
                          "unpriced": sorted({h.get("asset") or h["symbol"] for h in held if h["usd"] is None})},
            "freeze": list(freezes.values()),
            "ofac": {"listed": bool(ofac), "labels": _labels_out(ofac, inp.sources),
                     "list_date": str(inp.list_dates["ofac-sdn"]) if inp.list_dates.get("ofac-sdn") else None}}


def _classify(t, c: Ctx) -> str:
    """normal | zero | tiny | unrecognised"""
    if t.amount_raw == 0:
        return "zero"
    if not c.recognised(t.token_contract):
        return "unrecognised"
    a = c.asset(t.token_contract)
    if a in TINY and Decimal(t.amount_raw).scaleb(-t.token_decimals) < TINY[a]:
        return "tiny"
    return "normal"


def _usd(t, c: Ctx) -> Decimal | None:
    p, _ = c.price(t.token_contract, t.block_time)
    return None if p is None else Decimal(t.amount_raw).scaleb(-t.token_decimals) * p


def _sides(inp: ReportInput, c: Ctx):
    """(inbound, outbound) value movements of the address. Account chains: transfers to
    and from it. Bitcoin: outputs paying it, and inputs it spent."""
    if inp.chain == "btc":
        inbound = [t for t in inp.transfers if t.kind == "utxo_out" and c.is_me(t.to_address)]
        outbound = [t for t in inp.transfers if t.kind == "utxo_in" and c.is_me(t.from_address)]
    else:
        inbound = [t for t in inp.transfers if c.is_me(t.to_address) and t.kind != "utxo_in"]
        outbound = [t for t in inp.transfers if c.is_me(t.from_address) and t.kind != "utxo_out"]
    return inbound, outbound


def _activity(inp: ReportInput, c: Ctx, inbound, outbound) -> dict:
    groups: dict[str, dict] = {}
    for direction, rows in (("in", inbound), ("out", outbound)):
        for t in rows:
            k = c.key(t.token_contract)
            g = groups.setdefault(k, {"key": k, "contract": t.token_contract, "symbol": visible(t.token_symbol),
                                      "asset": c.asset(t.token_contract), "recognised": c.recognised(t.token_contract),
                                      "odd_characters": odd_characters(t.token_symbol), "decimals": t.token_decimals,
                                      "in": [0, 0, Decimal(0), 0], "out": [0, 0, Decimal(0), 0]})
            acc = g[direction]
            acc[0] += 1
            acc[1] += t.amount_raw
            u = _usd(t, c)
            if u is None:
                acc[3] += 1
            else:
                acc[2] += u
    rows = []
    for g in groups.values():
        def side(acc):
            return {"count": acc[0], "total_raw": str(acc[1]), "total": amount_str(acc[1], g["decimals"]),
                    "usd": usd_str(acc[2]) if acc[0] and acc[3] == 0 else None,
                    "usd_partial": usd_str(acc[2]) if acc[3] and acc[0] > acc[3] else None, "unpriced": acc[3]}
        rows.append({k: v for k, v in g.items() if k not in ("in", "out")} | {"in": side(g["in"]),
                                                                                "out": side(g["out"])})
    order = {a: i for i, a in enumerate(ASSET_ORDER)}
    rows.sort(key=lambda r: (not r["recognised"], r["key"] != "native" and r["asset"] is None,
                             order.get(r["asset"], 99), -(r["in"]["count"] + r["out"]["count"]), r["key"]))
    recognised = [r for r in rows if r["recognised"]]
    unrec = [r for r in rows if not r["recognised"]]
    every = inbound + outbound
    times = [t.block_time for t in every]
    return {"assets": recognised, "unrecognised_tokens": unrec[:UNRECOGNISED_ROWS],
            "unrecognised_more": max(0, len(unrec) - UNRECOGNISED_ROWS),
            "unrecognised_transfers": sum(r["in"]["count"] + r["out"]["count"] for r in unrec),
            "first_activity": iso(min(times)) if times else None, "last_activity": iso(max(times)) if times else None,
            "transfers_in": len(inbound), "transfers_out": len(outbound),
            "unpriced_assets": sorted(c.unpriced)}


def _hop_rows(pairs, c: Ctx, inp: ReportInput) -> tuple[list[dict], int]:
    """pairs: (counterparty, transfer). One entry per address with one line per asset,
    addresses ordered by their largest line."""
    agg: dict[tuple[str, str], dict] = {}
    for party, t in pairs:
        k = (party, c.key(t.token_contract))
        a = agg.setdefault(k, {"address": party, "key": k[1], "contract": t.token_contract,
                               "symbol": visible(t.token_symbol), "asset": c.asset(t.token_contract),
                               "decimals": t.token_decimals, "count": 0, "raw": 0, "usd": Decimal(0), "unpriced": 0,
                               "first": t.block_time, "last": t.block_time})
        a["count"] += 1
        a["raw"] += t.amount_raw
        u = _usd(t, c)
        if u is None:
            a["unpriced"] += 1
        else:
            a["usd"] += u
        a["first"], a["last"] = min(a["first"], t.block_time), max(a["last"], t.block_time)
    order = {x: i for i, x in enumerate(ASSET_ORDER)}
    rank = lambda a: (-(a["usd"] if a["unpriced"] == 0 else Decimal(0)), order.get(a["asset"], 99), -a["raw"],  # noqa: E731
                      a["address"])
    by_address: dict[str, list[dict]] = defaultdict(list)
    for a in sorted(agg.values(), key=rank):
        by_address[a["address"]].append(a)
    addresses = sorted(by_address, key=lambda addr: rank(by_address[addr][0]))
    out = []
    for addr in addresses[:FIRST_HOP_ROWS]:
        lines = [{"contract": a["contract"], "symbol": a["symbol"], "asset": a["asset"], "count": a["count"],
                  "total_raw": str(a["raw"]), "total": amount_str(a["raw"], a["decimals"]),
                  "usd": usd_str(a["usd"]) if a["unpriced"] == 0 else None,
                  "first": iso(a["first"]), "last": iso(a["last"])} for a in by_address[addr]]
        out.append({"address": addr, "assets": lines,
                    "labels": _labels_out(inp.counterparty_labels.get(addr) or (), inp.sources)})
    return out, max(0, len(addresses) - FIRST_HOP_ROWS)


def lookalike_groups(addresses, chain: str) -> dict[str, list[str]]:
    """{address: [other addresses that begin and end with the same characters]}.
    Compared: the first 2 characters after the fixed prefix (0x, T, bc1, 1 or 3) and
    the last 4. Wallets show addresses shortened, and addresses made to share those
    characters with another are easy to copy by mistake."""
    def shape(a: str) -> tuple[str, str]:
        if a.startswith("0x") or a.lower().startswith("bc1"):
            cut = 2 if a.startswith("0x") else 3
        else:
            cut = 1
        head, tail = a[:cut + 2], a[-4:]
        return (head, tail) if chain == "tron" else (head.lower(), tail.lower())
    groups: dict[tuple[str, str], set[str]] = defaultdict(set)
    for a in addresses:
        if a:
            groups[shape(a)].add(a)
    return {a: sorted(g - {a}) for g in groups.values() if len(g) > 1 for a in g}


def _mark_lookalikes(hops: dict, set_apart: dict, chain: str) -> list[dict]:
    """Give each lookalike group a letter, mark its rows, and return the groups so the
    report can describe each one once."""
    rows = hops["destinations"] + hops["funders"]
    others = [r["counterparty"] for k in ("zero", "tiny") for r in set_apart[k]["rows"]]
    shown = {r["address"] for r in rows} | {o for o in others if o}
    look = lookalike_groups(shown, chain)
    groups: list[list[str]] = []
    for addr in [r["address"] for r in rows] + others:
        if addr not in look:
            continue
        members = sorted({addr, *look[addr]})
        if members not in groups:
            groups.append(members)
    letter = {}
    for i, members in enumerate(groups):
        for m in members:
            letter[m] = chr(ord("A") + i) if i < 26 else str(i + 1)
    for r in rows:
        r["lookalike_group"] = letter.get(r["address"])
    for k in ("zero", "tiny"):
        for r in set_apart[k]["rows"]:
            r["lookalike_group"] = letter.get(r["counterparty"])
    return [{"group": letter[m[0]], "addresses": m} for m in groups]


def _first_hop(inp: ReportInput, c: Ctx, inbound, outbound) -> dict:
    counted = lambda t: _classify(t, c) == "normal"                     # noqa: E731
    if inp.chain == "btc":
        by_tx = defaultdict(list)
        for t in inp.transfers:
            by_tx[t.tx_hash].append(t)
        paid_txs = {t.tx_hash for t in inbound}
        spent_txs = {t.tx_hash for t in outbound}
        funders = [(t.from_address, t) for h in sorted(paid_txs) for t in by_tx[h]
                   if t.kind == "utxo_in" and not c.is_me(t.from_address) and counted(t)]
        outputs = [(t.to_address, t) for h in sorted(spent_txs) for t in by_tx[h]
                   if t.kind == "utxo_out" and not c.is_me(t.to_address) and counted(t)]
        back = [t for h in sorted(spent_txs) for t in by_tx[h] if t.kind == "utxo_out" and c.is_me(t.to_address)]
        dest_rows, dest_more = _hop_rows(outputs, c, inp)
        fund_rows, fund_more = _hop_rows(funders, c, inp)
        senders = {t.from_address for h in paid_txs for t in by_tx[h] if t.kind == "utxo_in"
                   and not c.is_me(t.from_address)}
        first = min(inbound, key=lambda t: (t.block_number, t.tx_hash, t.event_index), default=None)
        first_funders = sorted({t.from_address for t in by_tx[first.tx_hash] if t.kind == "utxo_in"
                                and not c.is_me(t.from_address)}) if first else []
        return {"destinations": dest_rows, "destinations_more": dest_more, "funders": fund_rows,
                "funders_more": fund_more, "distinct_senders": len(senders),
                "first_funder": None if first is None else {
                    "addresses": first_funders, "tx_hash": first.tx_hash, "time": iso(first.block_time),
                    "block": first.block_number, "amount": amount_str(first.amount_raw, first.token_decimals),
                    "symbol": "BTC",
                    "labels": {a: _labels_out(inp.counterparty_labels.get(a) or (), inp.sources)
                               for a in first_funders if inp.counterparty_labels.get(a)}},
                "btc_returned": {"count": len(back), "total_raw": str(sum(t.amount_raw for t in back)),
                                 "total": amount_str(sum(t.amount_raw for t in back), 8),
                                 "transactions": len({t.tx_hash for t in back})},
                "btc_spending_transactions": len(spent_txs)}
    dests = [(c.norm(t.to_address), t) for t in outbound if counted(t) and t.to_address and not c.is_me(t.to_address)]
    funds = [(c.norm(t.from_address), t) for t in inbound if counted(t) and t.from_address
             and not c.is_me(t.from_address)]
    dest_rows, dest_more = _hop_rows(dests, c, inp)
    fund_rows, fund_more = _hop_rows(funds, c, inp)
    first = min(inbound, key=lambda t: (t.block_number, t.tx_hash, t.event_index), default=None)
    first_out = None
    if first is not None:
        party = c.norm(first.from_address)
        first_out = {"addresses": [party], "tx_hash": first.tx_hash, "time": iso(first.block_time),
                     "block": first.block_number, "amount": amount_str(first.amount_raw, first.token_decimals),
                     "symbol": visible(first.token_symbol), "contract": first.token_contract,
                     "recognised": c.recognised(first.token_contract),
                     "labels": {party: _labels_out(inp.counterparty_labels.get(party) or (), inp.sources)}
                     if inp.counterparty_labels.get(party) else {}}
    return {"destinations": dest_rows, "destinations_more": dest_more, "funders": fund_rows,
            "funders_more": fund_more,
            "distinct_senders": len({c.norm(t.from_address) for t in inbound if t.from_address}),
            "first_funder": first_out}


def _set_apart(inp: ReportInput, c: Ctx, inbound, outbound) -> dict:
    groups = {"zero": [], "tiny": []}
    for direction, rows in (("in", inbound), ("out", outbound)):
        for t in rows:
            k = _classify(t, c)
            if k in groups:
                other = t.from_address if direction == "in" else t.to_address
                groups[k].append((t, direction, c.norm(other)))
    out = {}
    for k, rows in groups.items():
        rows.sort(key=lambda r: (r[0].block_number, r[0].tx_hash, r[0].event_index), reverse=True)
        out[k] = {"count": len(rows),
                  "rows": [{"direction": d, "counterparty": o, "tx_hash": t.tx_hash, "time": iso(t.block_time),
                            "symbol": visible(t.token_symbol), "contract": t.token_contract,
                            "recognised": c.recognised(t.token_contract),
                            "amount": amount_str(t.amount_raw, t.token_decimals)} for t, d, o in rows]}
    return out


def _labels_section(inp: ReportInput, c: Ctx) -> dict:
    goplus = next((k for k in inp.label_checks if k["source"] == "goplus"), None)
    if inp.chain == "btc":
        gp = {"status": "never_asked_btc"}
    elif goplus is None:
        gp = {"status": "not_asked"}
    else:
        det = goplus.get("detail") or {}
        gp = {"status": goplus["result"], "flags": sorted(det.get("flags") or []),
              "data_source": det.get("data_source") or None,
              "read_at": iso(goplus["observed_at"]) if isinstance(goplus.get("observed_at"), dt.datetime) else None}
    return {"address": _labels_out(inp.labels, inp.sources), "goplus": gp,
            "list_dates": {k: str(v) for k, v in sorted(inp.list_dates.items())},
            "counterparties_checked_against": ["ofac-sdn", "exchange-por", "okx-por"]}


def _completeness(inp: ReportInput, c: Ctx, inbound, outbound) -> dict:
    snap = inp.snapshot
    checks = []
    before = None
    for ch in snap.get("checks") or []:
        checks.append({"name": ch["name"], "status": ch["status"], "reason": ch.get("reason"),
                       "marks_incomplete": ch.get("marks_incomplete", True)})
        det = ch.get("details") or {}
        if ch["name"] == "tx_count" and "history_start_block" in det:
            before = {"history_start_block": det["history_start_block"],
                      "transactions_before_history": det.get("transactions_before_history")}
    every = inbound + outbound
    oldest = min((t.block_time for t in every), default=None)
    internal = None
    if inp.family == "evm":
        lo, hi = snap.get("internal_from_block"), snap.get("internal_to_block")
        internal = {"from_block": lo, "to_block": hi,
                    "full": lo == 0 and hi == snap["as_of_block"] if lo is not None else False}
    not_read = list(NOT_READ.get(inp.chain if inp.chain in NOT_READ else inp.family, []))
    recon = [{"contract": t["contract"], "symbol": visible(t.get("symbol")), "status": t["status"],
              "stablecoin": t["stablecoin"], "reason": t.get("reason"),
              "destroyed": bool(int(t.get("destroyed_raw") or 0))}
             for t in snap.get("token_reconciliation") or [] if t["stablecoin"]]
    return {"checked": snap.get("history_incomplete") is not None,
            "history_incomplete": snap.get("history_incomplete"),
            "incomplete_reasons": list(snap.get("incomplete_reasons") or []),
            "checks": checks, "stablecoins": recon,
            "transfers": {"read": snap["transfer_count"], "truncated": snap["transfers_truncated"],
                          "oldest_read": iso(oldest)},
            "tokens_truncated": snap["tokens_truncated"], "internal": internal, "before_history": before,
            "not_read": not_read}


def _links(inp: ReportInput) -> dict:
    a = inp.address
    path = {"tron": f"{inp.explorer_url}/#/address/{a}"}.get(inp.chain, f"{inp.explorer_url}/address/{a}")
    return {"explorer": {"name": inp.explorer_url.split("//", 1)[-1], "url": path},
            "chainabuse": f"https://www.chainabuse.com/address/{a}",
            "arkham": f"https://intel.arkm.com/explorer/address/{a}"}


def _lists(inp: ReportInput, c: Ctx) -> dict:
    """How many first-hop addresses were checked against the lists, and which matched.
    Every counterparty counts, small and zero-value transfers included, as the worker
    applies the lists (taw/labels.py)."""
    from taw.labels import counterparties
    parties = counterparties(inp.chain, inp.address, inp.transfers)
    dests = {c.norm(a) for a, roles in parties.items() if "destination" in roles}
    funds = {c.norm(a) for a, roles in parties.items() if "funder" in roles}
    matched = []
    for a in sorted(dests | funds):
        ls = inp.counterparty_labels.get(a) or ()
        if ls:
            matched.append({"address": a, "roles": sorted(r for r, group in (("destination", dests), ("funder", funds))
                                                          if a in group),
                            "labels": _labels_out(ls, inp.sources)})
    return {"destinations_checked": len(dests), "funders_checked": len(funds), "matched": matched,
            "list_dates": {k: str(v) for k, v in sorted(inp.list_dates.items())}}


def _summary(holdings: dict, activity: dict, hops: dict, lists: dict, completeness: dict, c: Ctx) -> dict:
    held = []
    n = holdings["native"]
    if int(n["amount_raw"]) > 0:
        held.append({"name": n["symbol"], "amount": n["amount"], "freezable": False})
    for t in holdings["tokens"]:
        if t["recognised"] and int(t["amount_raw"]) > 0:
            held.append({"name": t["asset"] or t["symbol"], "amount": t["amount"], "freezable": t["freezable"]})
    frozen = [{"token": f["token"], "issuer": f["issuer"], "since": f["since"]}
              for f in holdings["freeze"] if f["status"] == "frozen"]
    destroyed = [{"token": f["token"], "issuer": f["issuer"], "amount": x["amount"], "time": x["time"]}
                 for f in holdings["freeze"] for x in f["destructions"]]
    largest = None
    if hops["destinations"]:
        top = hops["destinations"][0]
        line = top["assets"][0]
        key = line["contract"] if line["contract"] is None else c.norm(line["contract"])
        out_total = next((int(a["out"]["total_raw"]) for a in activity["assets"]
                          if (a["contract"] is None and key is None) or (a["contract"] and c.norm(a["contract"]) == key)),
                         0)
        share = (Decimal(int(line["total_raw"])) * 100 / out_total).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP) \
            if out_total else None
        largest = {"address": top["address"], "asset": line["asset"] or line["symbol"], "total": line["total"],
                   "share_percent": str(share) if share is not None else None, "listed": bool(top["labels"])}
    if not completeness["checked"]:
        verdict = "not_checked"
    elif completeness["history_incomplete"]:
        verdict = "incomplete"
    elif completeness["transfers"]["truncated"]:
        verdict = "truncated"
    else:
        verdict = "complete"
    return {"held": held, "frozen": frozen, "destroyed": destroyed, "largest_destination": largest,
            "list_matches": len(lists["matched"]),
            "checked": lists["destinations_checked"] + lists["funders_checked"], "history": verdict}


def build(inp: ReportInput) -> dict:
    """report.data for one snapshot. Pure: no database, no clock."""
    c = Ctx(inp)
    inbound, outbound = _sides(inp, c)
    freezes = _freeze_status(inp, c)
    holdings = _holdings(inp, c, freezes)
    activity = _activity(inp, c, inbound, outbound)
    hops = _first_hop(inp, c, inbound, outbound)
    labels = _labels_section(inp, c)
    set_apart = _set_apart(inp, c, inbound, outbound)
    used = {l["source"] for l in inp.labels}
    used |= {l["source"] for ls in inp.counterparty_labels.values() for l in ls}
    used |= {k["source"] for k in inp.label_checks}
    if any(r.get("usd") for r in holdings["tokens"]) or holdings["native"]["usd"] or any(
            a["in"]["usd"] or a["out"]["usd"] for a in activity["assets"]):
        used.add("chainlink")
    if inp.chain == "btc":
        used.discard("goplus")
    completeness = _completeness(inp, c, inbound, outbound)
    lists = _lists(inp, c)
    groups = _mark_lookalikes(hops, set_apart, inp.chain)
    return {
        "summary": _summary(holdings, activity, hops, lists, completeness, c),
        "header": _header(inp, c, used),
        "holdings": holdings,
        "activity": activity,
        "first_hop": hops,
        "lookalikes": groups,
        "set_apart": set_apart,
        "labels": labels,
        "lists": lists,
        "completeness": completeness,
        "links": _links(inp),
        "prices_available": any(r.get("status") == "ok" for r in inp.prices.values()),
    }

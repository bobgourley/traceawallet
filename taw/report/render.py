"""Plain text from report.data, through the templates in templates/.

All report prose is in the templates; this module only adds number and time
formatting and the wording tables for codes in the data. Two sections are fixed
text, the same in every report: the recovery-scam warning and the IC3 filing
checklist (templates/fixed/). They are drafts until the owner approves them, and
they are the only place assessment words may appear (tests/test_report.py).

Overlays are read when a report is shown and are not part of its hash: the
bucketed count of prior lookups (from lookup_count only), the report's public id
and fingerprint, and, for the lookup's owner only, their matched transaction.

Token names are written by whoever deployed the token. They are printed with dots
and "://" broken up ("bio[.]link"), so no reader or later web page turns one into
a link.
"""
from __future__ import annotations

import datetime as dt
import pathlib

from jinja2 import Environment, FileSystemLoader, StrictUndefined

TEMPLATES = pathlib.Path(__file__).parent / "templates"
FIXED_START = "<<< fixed section: {name} >>>"
FIXED_END = "<<< end of fixed section >>>"

CHECK_TITLES = {
    "stablecoin_reconciliation": "Stablecoin balances match the transfers read",
    "other_token_reconciliation": "Other token balances match the transfers read (for information only)",
    "native_reconciliation": "The native coin balance matches the transfers read",
    "tx_count": "The number of transactions sent matches the address's nonce",
    "token_lists_agree": "Blockscout's two lists of token transfers agree",
    "token_list_matches_logs": "Etherscan's token list matches the token transfer logs",
    "other_party_rows_capped": "Every input and output of the transactions read was stored",
    "earliest_transaction": "The earliest transaction was found",
}
STATUS_WORDS = {"passed": "passed", "failed": "did not pass", "not_run": "not run"}
NOT_CHECKED_WORDS = {
    "no_source_with_suitable_terms": "no data source we may use for a public site covers this chain yet",
    "needs_paid_blockscout_plan": "it needs a paid Blockscout plan",
}
NOT_READ_WORDS = {
    "tron_trc10": "TRC-10 tokens and their balances are not read.",
    "tron_internal": "TRX moved by smart contracts (internal transactions) is not read.",
    "btc_unconfirmed": "Transactions not yet in a block are not counted.",
    "evm_nft": "NFT transfers are not read.",
}
RECON_WORDS = {
    "reconciled": "matches the transfers read",
    "not_reconciled": "does not match the transfers read",
    "not_checkable": "could not be checked",
    "frozen_zero_no_destruction": "is zero while the address is frozen, and no destruction by the issuer was found",
}
USED_FOR_WORDS = {
    "chain_data": "balances and transfers",
    "freeze_status": "freeze status, read from the token contract",
    "goplus_flags": "address flags",
    "sanctions_list": "sanctions list matching",
    "exchange_list": "exchange wallet matching",
    "prices": "US dollar prices",
}
# Credits a source's terms require next to its data (not label wording).
CREDITS = {
    "etherscan": "Powered by Etherscan.io APIs",
    "goplus": "Powered by GoPlus",
    "chainlink": "approximate, not an exchange price; not affiliated with or endorsed by Chainlink",
}
LIST_NAMES = {
    "ofac-sdn": "OFAC's SDN sanctions list",
    "okx-por": "OKX's published list of its wallets",
    "exchange-por": "our file of the wallets Bitfinex, Bybit, Crypto.com, HTX and KuCoin publish",
}
HISTORY_WORDS = {
    "complete": "complete as far as we can check",
    "truncated": "partial: only the newest transfers were read",
    "incomplete": "may be incomplete",
    "not_checked": "not checked",
}
SKIP_CHECKS = {("tron", "tx_count"), ("btc", "tx_count")}     # nothing to check on that chain
PRIOR_BUCKETS = (
    (0, 0, "none"), (1, 1, "1"), (2, 5, "2 to 5"), (6, 20, "6 to 20"), (21, None, "more than 20"),
)


def bucket(count: int | None) -> str | None:
    """lookup_count.account_count as a bucket label; None when there is no row."""
    if count is None:
        return None
    for lo, hi, label in PRIOR_BUCKETS:
        if count >= lo and (hi is None or count <= hi):
            return label
    return None


def num(value) -> str:
    """'1234567.891' -> '1,234,567.891'. Exact: works on the string, never a float."""
    if value is None:
        return "?"
    s = str(value)
    neg = s.startswith("-")
    s = s.lstrip("-")
    whole, _, frac = s.partition(".")
    grouped = f"{int(whole):,}" if whole.isdigit() else whole
    return ("-" if neg else "") + grouped + (f".{frac}" if frac else "")


def when(value) -> str:
    if value is None:
        return "unknown"
    t = value if isinstance(value, dt.datetime) else dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def day(value) -> str:
    if value is None:
        return "unknown"
    return when(value)[:10]


def plural(n: int, one: str, many: str | None = None) -> str:
    return f"{num(n)} {one if n == 1 else (many or one + 's')}"


def usd(value) -> str:
    return "no price available" if value is None else f"about ${num(value)}"


def defang(name) -> str:
    """A token name, made safe to print: no link can be formed from it."""
    if name is None:
        return "(no name)"
    return str(name).replace("://", "[://]").replace(".", "[.]")


def when_span(first, last) -> str:
    return f"on {day(first)}" if day(first) == day(last) else f"from {day(first)} to {day(last)}"


def _env() -> Environment:
    env = Environment(loader=FileSystemLoader(str(TEMPLATES)), undefined=StrictUndefined, trim_blocks=True,
                      lstrip_blocks=True, keep_trailing_newline=True, autoescape=False)
    env.filters.update(num=num, when=when, day=day, usd=usd, defang=defang)
    env.globals.update(when_span=when_span, USED_FOR_WORDS=USED_FOR_WORDS, CREDITS=CREDITS, LIST_NAMES=LIST_NAMES,
                       HISTORY_WORDS=HISTORY_WORDS, SKIP_CHECKS=SKIP_CHECKS)
    env.globals.update(plural=plural, CHECK_TITLES=CHECK_TITLES, STATUS_WORDS=STATUS_WORDS,
                       NOT_CHECKED_WORDS=NOT_CHECKED_WORDS, NOT_READ_WORDS=NOT_READ_WORDS, RECON_WORDS=RECON_WORDS,
                       FIXED_START=FIXED_START, FIXED_END=FIXED_END)
    return env


def render_text(data: dict, *, prior_lookups: str | None = None, public_id: str | None = None,
                sha256: str | None = None, match: dict | None = None) -> str:
    """The whole report as plain text. prior_lookups: a bucket label, or None when the
    count has not been written for this address. match: the lookup's private match
    result, shown to its owner only; never stored in the report."""
    text = _env().get_template("report.txt.j2").render(d=data, prior_lookups=prior_lookups, public_id=public_id,
                                                       sha256=sha256, match=match)
    lines = [ln.rstrip() for ln in text.splitlines()]
    out: list[str] = []
    for ln in lines:                                   # no runs of blank lines
        if ln == "" and out and out[-1] == "":
            continue
        out.append(ln)
    return "\n".join(out).strip() + "\n"


def outside_fixed_sections(text: str) -> str:
    """The rendered text with the fixed sections removed (for the wording test)."""
    out, inside = [], False
    for ln in text.splitlines():
        if ln.startswith("<<< fixed section:"):
            inside = True
        elif ln == FIXED_END:
            inside = False
        elif not inside:
            out.append(ln)
    return "\n".join(out)

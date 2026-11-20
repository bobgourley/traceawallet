"""Build data/exchange_wallets.csv from wallet lists the exchanges published themselves.

    .venv/bin/python scripts/build_exchange_wallets.py [--from-dir DIR]

Each list is fetched from the exchange's own page or file (URLs below; the
research is in DECISIONS.md, 2026-11-20 "Labels: exchange wallets"). OKX is not
here: its monthly file holds about 500,000 addresses and is loaded straight from
OKX by taw/adapters/labels/exchanges.py.

Rules:
- An address goes in only for a chain we cover (eth, arbitrum, optimism, tron,
  btc), and only when the exchange's own words put it on that chain. Every
  address must pass its checksum.
- description is the exchange's own words for the row (its coin/network column,
  or its label). Several rows for one address on one chain are merged, with
  their descriptions joined by "; ".
- published is the date the exchange gives for the list (YYYY-MM-DD, or YYYY-MM
  when it gives only a month). Nothing is inferred about who uses an address.

--from-dir reads files saved earlier (htx.csv, bybit.csv, bitfinex.txt,
kucoin.html, cryptocom.html) instead of fetching.
"""
from __future__ import annotations

import argparse
import csv
import html
import io
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from taw.adapters.chains import btc_address, evm_address, tron_address  # noqa: E402

OUT = pathlib.Path(__file__).resolve().parent.parent / "data" / "exchange_wallets.csv"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"

SOURCES = {
    "htx": ("HTX", "https://raw.githubusercontent.com/huobiapi/Tool-Node.js-VerifyAddress/main/snapshot/huobi_por.csv",
            "2026-11-10", "File carries no date; date of HTX's commit 0774c876e6 'feat: upgrade merkle_proof_20261101'. "
                          "Linked from https://www.htx.com/en-us/support/44924880373629/"),
    "bybit": ("Bybit", "https://static.bycustody.com/download/app/bybit_por_202212.csv", "2022-12",
              "Month from the file name; Bybit gives no day. Linked from Bybit's help article 'How to Verify Bybit "
              "Ownership of Wallet Addresses and Their Balances'"),
    "bitfinex": ("Bitfinex", "https://raw.githubusercontent.com/bitfinexcom/pub/main/wallets.txt", "2022-12-12",
                 "Date of Bitfinex's last commit to the file (83d06bdb5f); first published 2022-11-11. Linked from "
                 "blog.bitfinex.com"),
    "kucoin": ("KuCoin", "https://www.kucoin.com/blog/9152-34", "2022-11-11",
               "Date shown on the post: 2022/11/11 21:02:07"),
    "cryptocom": ("Crypto.com", "https://crypto.com/en/company-news/transparency-first", "2022-11-11",
                  "Date shown on the post: 11 Nov 2022. Crypto.com says these cover only a portion of its reserves"),
}
ADDR = re.compile(r"\b(0x[0-9a-fA-F]{40}|bc1[0-9a-z]{25,90}|[13][1-9A-HJ-NP-Za-km-z]{25,34}|T[1-9A-HJ-NP-Za-km-z]{33})\b")


def stored(chain: str, address: str) -> str | None:
    """The address in our stored form if it is valid for the chain, else None."""
    a = address.strip().strip('"')
    try:
        if chain == "tron":
            return a if a.startswith("T") and tron_address.is_valid(a) else None
        if chain == "btc":
            return btc_address.normalize(a)
        return evm_address.normalize(a)
    except ValueError:
        return None


def fetch(key: str, from_dir: pathlib.Path | None) -> str:
    names = {"htx": "htx.csv", "bybit": "bybit.csv", "bitfinex": "bitfinex.txt", "kucoin": "kucoin.html",
             "cryptocom": "cryptocom.html"}
    if from_dir:
        return (from_dir / names[key]).read_text(errors="ignore")
    import httpx
    r = httpx.get(SOURCES[key][1], headers={"user-agent": UA}, timeout=60, follow_redirects=True)
    r.raise_for_status()
    return r.text


def htx(text):
    body = text[text.index("coin,address,"):]
    for row in csv.DictReader(io.StringIO(body)):
        coin = (row.get("coin") or "").strip()
        chain = ("tron" if "TRC20" in coin or coin == "TRX" else "eth" if "ERC20" in coin or coin == "ETH"
                 else "btc" if coin == "BTC" else None)
        if chain:
            yield chain, row.get("address") or "", coin


BYBIT_NET = {"ETH": "eth", "Arbitrum": "arbitrum", "Optimism": "optimism", "TRON": "tron"}


def bybit(text):
    for row in csv.reader(io.StringIO(text)):
        if len(row) < 4 or not row[3].strip():
            continue
        label = row[0].strip()
        chain = "btc" if label == "BTC" else BYBIT_NET.get(label.split("-", 1)[1]) if "-" in label else None
        if chain:
            for a in row[3].split():
                yield chain, a, label


def bitfinex(text):
    for line in text.splitlines():
        if " - " not in line:
            continue
        a, label = line.split(" - ", 1)
        label = label.strip()
        chain = ("btc" if label.startswith("BTC ") or "(Omni Protocol)" in label else
                 "eth" if label.startswith("ETH/ERC20") else "tron" if label.startswith("Tron ") else None)
        if chain:
            yield chain, a, label


def page_text(text):
    t = html.unescape(text.replace("\\u003c", "<").replace("\\u003e", ">"))
    t = re.sub(r"<[^>]+>", " | ", t)
    return re.sub(r"(\s*\|\s*)+", " | ", t)


def kucoin(text):
    t = page_text(text)
    # Table: | Coin | Address | Balance | Height | ... The ETH rows give the Ethereum snapshot height.
    rows = re.findall(r"\| ([A-Z][A-Z0-9]{1,9}) \| (\S+) \| ([\d,.]+) \| (\d+) ", t)
    eth_heights = {h for coin, _, _, h in rows if coin == "ETH"}
    for coin, a, _, height in rows:
        if a.startswith("0x"):
            if height in eth_heights:          # same snapshot height as KuCoin's ETH rows: Ethereum
                yield "eth", a, f"{coin} (height {height})"
        elif a.startswith("T"):
            yield "tron", a, f"{coin} (height {height})"
        elif coin == "BTC":
            yield "btc", a, f"{coin} (height {height})"


def cryptocom(text):
    t = page_text(text)
    start = t.index("Wallet Addresses List:")
    heading = None
    for token in t[start:start + 4000].split(" | "):
        token = token.strip()
        if token in ("BTC:", "ETH, ERC20, and more:"):
            heading = token.rstrip(":")
            continue
        m = ADDR.fullmatch(token)
        if m and heading:
            yield ("btc" if heading == "BTC" else "eth"), token, heading


PARSERS = {"htx": htx, "bybit": bybit, "bitfinex": bitfinex, "kucoin": kucoin, "cryptocom": cryptocom}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--from-dir", type=pathlib.Path)
    args = ap.parse_args()
    merged: dict[tuple, list[str]] = {}
    for key, parse in PARSERS.items():
        exchange, url, published, note = SOURCES[key]
        kept = bad = 0
        for chain, address, description in parse(fetch(key, args.from_dir)):
            s = stored(chain, address)
            if s is None:
                bad += 1
                continue
            descs = merged.setdefault((exchange, chain, s, published, url, note), [])
            if description not in descs:
                descs.append(description)
            kept += 1
        if bad:
            sys.exit(f"{exchange}: {bad} address(es) failed their checksum; nothing written")
        print(f"{exchange}: {kept} row(s)")
    OUT.parent.mkdir(exist_ok=True)
    with OUT.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["exchange", "chain", "address", "description", "published", "source_url", "note"])
        for (exchange, chain, a, published, url, note), descs in sorted(merged.items()):
            w.writerow([exchange, chain, a, "; ".join(descs), published, url, note])
    print(f"{len(merged)} address(es) written to {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

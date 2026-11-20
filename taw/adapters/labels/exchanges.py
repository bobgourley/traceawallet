"""Exchange wallets, as listed by the exchanges themselves (local label sources).

Two bulk sources, both worded "listed by <exchange> as its wallet on <date>":

  exchange-por  data/exchange_wallets.csv: HTX's current list and the 2022 lists
                of Bybit, KuCoin, Bitfinex and Crypto.com, built by
                scripts/build_exchange_wallets.py from each exchange's own page.
  okx-por       OKX's monthly proof-of-reserves file, downloaded from OKX:
                https://static.okx.com/cdn/okx/por/chain/por_csv_YYYYMM0100_V1.zip
                (about 110 MB, one 220 MB CSV). The zip is kept in the bucket.

OKX: every row on a network we cover is loaded (ETH -> eth, ARBITRUM -> arbitrum,
OPTIMISM -> optimism, TRON -> tron, BTC -> btc), deposit addresses included. The
label quotes the row's Type column as OKX gives it ("Non Staking", "Native
Staking", ...); nothing is inferred about who holds an address. Rows whose
"address" is not an address for its network (ETH validator keys, 98 characters)
are counted and skipped.

Monthly refresh without rewriting: OKX labels are dated_label claims, so an
address still listed next month keeps its row and the date it was first listed;
a new address gets a row with the new date; an address no longer listed gets a
'withdrawn' row. label_load records every month's file.
"""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import pathlib
import re
import zipfile
from collections import Counter

import httpx

from taw.adapters.base import RawStore
from taw.adapters.chains import btc_address, evm_address, tron_address
from taw.adapters.labels.base import LabelClaim, Publication

CSV_PATH = pathlib.Path(__file__).resolve().parents[3] / "data" / "exchange_wallets.csv"
OKX_SOURCE, FILE_SOURCE = "okx-por", "exchange-por"
OKX_URL = "https://static.okx.com/cdn/okx/por/chain/por_csv_{stamp}0100_V1.zip"
OKX_PAGE = "https://www.okx.com/en-us/proof-of-reserves/download"
OKX_NETWORKS = {"ETH": "eth", "ARBITRUM": "arbitrum", "OPTIMISM": "optimism", "TRON": "tron", "BTC": "btc"}
OKX_HEADER = "coin,Type,Network,"


class NotPublished(LookupError):
    pass


def stored(chain: str, address: str) -> str | None:
    a = address.strip()
    try:
        if chain == "tron":
            return a if a.startswith("T") and tron_address.is_valid(a) else None
        if chain == "btc":
            return btc_address.normalize(a)
        return evm_address.normalize(a)
    except ValueError:
        return None


def wording(exchange: str, published: str) -> str:
    text = f"listed by {exchange} as its wallet on {published}"
    return text + " (month only; no day given)" if re.fullmatch(r"\d{4}-\d{2}", published) else text


# -- data/exchange_wallets.csv ------------------------------------------------------------------

class ExchangeFile:
    slug = FILE_SOURCE
    local = True

    def __init__(self, path: pathlib.Path = CSV_PATH):
        self.path = path

    def load(self) -> Publication:
        body = self.path.read_bytes()
        claims, per = [], Counter()
        dates = []
        for row in csv.DictReader(io.StringIO(body.decode())):
            ex, published = row["exchange"], row["published"]
            label = wording(ex, published)
            if row["description"]:
                label += f" (the exchange's description: {row['description']})"
            full = dt.date.fromisoformat(published) if len(published) == 10 else None
            if full:
                dates.append(full)
            claims.append(LabelClaim(chain=row["chain"], address=row["address"], category="exchange", label=label,
                                     claim_key=f"{ex}:{published}", source_date=full, claim_url=row["source_url"]))
            per[ex] += 1
        return Publication(FILE_SOURCE, max(dates), None, claims,
                           detail={"file_sha256": hashlib.sha256(body).hexdigest(), "rows_by_exchange": dict(per)})


# -- OKX ---------------------------------------------------------------------------------------

def okx_url(month: dt.date) -> str:
    return OKX_URL.format(stamp=f"{month:%Y%m}")


def parse_okx(zip_bytes: bytes, published: dt.date, sha: str | None) -> tuple[list[LabelClaim], dict]:
    claims: dict[tuple[str, str, str], LabelClaim] = {}
    skipped, other = Counter(), Counter()
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
        names = [n for n in z.namelist() if n.endswith(".csv")]
        if len(names) != 1:
            raise ValueError(f"expected one CSV in the OKX zip, found {names}")
        with z.open(names[0]) as raw:
            lines = io.TextIOWrapper(raw, encoding="utf-8", newline="")
            for line in lines:                                   # a coin/amount summary comes first
                if line.startswith(OKX_HEADER):
                    break
            else:
                raise ValueError("no address table in the OKX file")
            for row in csv.reader(lines):
                if len(row) < 5:
                    continue
                chain = OKX_NETWORKS.get(row[2].strip())
                if chain is None:
                    other[row[2].strip()] += 1
                    continue
                kind = row[1].strip()
                a = stored(chain, row[4])
                if a is None:
                    skipped[f"{row[2].strip()} / {kind}"] += 1
                    continue
                key = (chain, a, kind)
                if key not in claims:
                    claims[key] = LabelClaim(
                        chain=chain, address=a, category="exchange",
                        label=f"{wording('OKX', published.isoformat())} (type in OKX's file: {kind})",
                        claim_key=f"okx:{kind}", source_date=published, claim_url=OKX_PAGE, raw_sha256=sha,
                        dated_label=True)
    by_chain = Counter(c.chain for c in claims.values())
    return list(claims.values()), {"by_chain": dict(by_chain), "skipped_not_an_address": dict(skipped),
                                   "rows_on_other_networks": sum(other.values())}


class OkxPor:
    slug = OKX_SOURCE
    local = True

    def __init__(self, raw_store: RawStore, *, http: httpx.Client | None = None):
        self.raw_store = raw_store
        self.http = http or httpx.Client(timeout=600.0, follow_redirects=True,
                                         headers={"user-agent": "TraceAWallet list loader"})

    def load(self, month: dt.date) -> Publication:
        """The file for `month` (its first day). Raises NotPublished when OKX has none."""
        url = okx_url(month)
        resp = self.http.get(url)
        if resp.status_code in (403, 404):
            raise NotPublished(f"OKX has no file at {url} (HTTP {resp.status_code})")
        resp.raise_for_status()
        sha = self.raw_store.save(OKX_SOURCE, url, None, 200, resp.content)
        published = month.replace(day=1)
        claims, detail = parse_okx(resp.content, published, sha)
        return Publication(OKX_SOURCE, published, sha, claims,
                           unmapped=sum(detail["skipped_not_an_address"].values()),
                           detail={**detail, "url": url, "bytes": len(resp.content)})

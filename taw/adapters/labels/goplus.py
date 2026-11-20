"""GoPlus malicious-address flags for the looked-up address (EVM and Tron, never Bitcoin).

GET https://api.gopluslabs.io/api/v1/address_security/{address}?chain_id=...
(docs.gopluslabs.io/reference/addresscontractusingget_1, read 2026-11-20). Chain
ids: 1 (Ethereum), 42161 (Arbitrum), 10 (OP Mainnet), "tron". Calls carry an
access token from POST /api/v1/token (sign = sha1(app_key + time + app_secret));
it lasts 7200 seconds and is reused. It goes in the Authorization header bare:
the "Bearer <token>" form the docs show was refused live (code 4012).

The reply is stored unmodified (licence s.5) as a raw response. Each field
GoPlus sets to "1" becomes one label quoting the field and value exactly, worded
as GoPlus's claim (rule 9) and naming GoPlus's own data source when it gives
one: 'flagged by GoPlus: phishing_activities = "1" (data source: SlowMist)'.
contract_address is a description, not a flag; number_of_malicious_contracts_created
counts as a flag when it is not "0".

Never for Bitcoin: GoPlus answers every Bitcoin address with all flags "0",
which would read as clean (DECISIONS.md, 2026-11-20 evening).
Every call, the token included, is counted in the GoPlus budget ledger.
"""
from __future__ import annotations

import hashlib
import time
from typing import Callable

import httpx

from taw.adapters.base import RawStore
from taw.adapters.labels.base import LabelClaim

SOURCE = "goplus"
BASE = "https://api.gopluslabs.io"
CHAIN_IDS = {"eth": "1", "arbitrum": "42161", "optimism": "10", "tron": "tron"}
NOT_FLAGS = {"contract_address", "data_source"}
COUNT_FIELDS = {"number_of_malicious_contracts_created"}


class GoPlusError(RuntimeError):
    pass


def flags(result: dict) -> list[tuple[str, str]]:
    """[(field, value)] that GoPlus reports as set, in GoPlus's field order."""
    out = []
    for field, value in result.items():
        if field in NOT_FLAGS or not isinstance(value, str):
            continue
        if field in COUNT_FIELDS:
            if value.strip() not in ("", "0"):
                out.append((field, value))
        elif value == "1":
            out.append((field, value))
    return out


def claims_from(chain: str, address: str, result: dict, sha: str) -> list[LabelClaim]:
    source = (result.get("data_source") or "").strip()
    suffix = f" (data source: {source})" if source else ""
    return [LabelClaim(chain=chain, address=address, category="malicious_flag",
                       label=f'flagged by GoPlus: {field} = "{value}"{suffix}', claim_key=f"goplus:{field}",
                       source_date=None, raw_sha256=sha)
            for field, value in flags(result)]


class GoPlus:
    slug = SOURCE
    local = False

    def __init__(self, raw_store: RawStore, app_key: str | None, app_secret: str | None, *, budget=None,
                 http: httpx.Client | None = None, clock: Callable[[], float] = time.time):
        self.raw_store, self.budget, self._clock = raw_store, budget, clock
        self._key, self._secret = app_key, app_secret
        self.http = http or httpx.Client(timeout=30.0)
        self._token: tuple[str, float] | None = None

    def _count(self) -> None:
        if self.budget is not None:
            self.budget.before_call(1)

    def _counted(self) -> None:
        if self.budget is not None:
            self.budget.after_call(1, None)

    def _access_token(self) -> str | None:
        if not (self._key and self._secret):
            return None                                   # GoPlus answers without a token, at lower limits
        if self._token and self._token[1] > self._clock() + 60:
            return self._token[0]
        now = int(self._clock())
        sign = hashlib.sha1(f"{self._key}{now}{self._secret}".encode()).hexdigest()
        self._count()
        resp = self.http.post(f"{BASE}/api/v1/token", json={"app_key": self._key, "time": now, "sign": sign})
        self._counted()
        data = resp.json() if resp.content else {}
        token = (data.get("result") or {}).get("access_token")
        if resp.status_code != 200 or data.get("code") != 1 or not token:
            raise GoPlusError(f"GoPlus refused the access token request (HTTP {resp.status_code}, "
                              f"code {data.get('code')}: {data.get('message')})")
        self._token = (token, now + int((data.get("result") or {}).get("expires_in") or 0))
        return token                                      # never stored: the raw store keeps no token response

    def address_security(self, chain: str, address: str) -> tuple[dict, str]:
        """(result, sha of the stored reply). Raises GoPlusError when GoPlus doesn't answer code 1."""
        if chain not in CHAIN_IDS:
            raise ValueError(f"GoPlus is not asked about {chain} addresses")
        token = self._access_token()
        url = f"{BASE}/api/v1/address_security/{address}"
        params = {"chain_id": CHAIN_IDS[chain]}
        self._count()
        # The bare token: GoPlus refused "Bearer <token>" (code 4012) on 2026-11-20, though its docs show it.
        resp = self.http.get(url, params=params, headers={"Authorization": token} if token else None)
        self._counted()
        sha = self.raw_store.save(SOURCE, url, params, resp.status_code, resp.content)
        data = resp.json() if resp.content else {}
        if resp.status_code != 200 or data.get("code") != 1 or not isinstance(data.get("result"), dict):
            raise GoPlusError(f"GoPlus answered HTTP {resp.status_code}, code {data.get('code')}: "
                              f"{data.get('message')}")
        return data["result"], sha

    def claims_for(self, chain: str, address: str) -> tuple[list[LabelClaim], dict, str]:
        result, sha = self.address_security(chain, address)
        found = claims_from(chain, address, result, sha)
        check = {"chain_id": CHAIN_IDS[chain], "flags": [c.claim_key.split(":", 1)[1] for c in found],
                 "data_source": result.get("data_source") or "", "fields": len(result)}
        return found, check, sha

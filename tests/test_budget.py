"""Blockscout's daily budget: the 70/30 split, the adapter's metering hooks, and the
finished-range search that runs only when a lookup needs it."""
import datetime as dt

import httpx
import pytest

from taw.budget import BudgetExhausted, check_allowance, next_reset
from tests.test_evm_blockscout import A, FakeBlockscout, make

RESET = dt.datetime(2026, 11, 20, tzinfo=dt.timezone.utc)
ALLOW = 100_000


def allowed(consumer, credits, used, remaining=None):
    try:
        check_allowance(consumer, credits, used, remaining, ALLOW, RESET)
        return True
    except BudgetExhausted as exc:
        assert exc.resets_at == RESET and exc.provider == "Blockscout"
        return False


def test_next_reset_is_midnight_utc():
    assert next_reset(dt.datetime(2026, 11, 19, 23, 59, tzinfo=dt.timezone.utc)) == RESET
    assert next_reset(dt.datetime(2026, 11, 20, 0, 0, tzinfo=dt.timezone.utc)) == RESET + dt.timedelta(days=1)


def test_worker_may_use_the_whole_allowance():
    assert allowed("worker", 20, {"worker": 99_980})
    assert not allowed("worker", 20, {"worker": 99_990})
    assert not allowed("worker", 20, {}, remaining=10)          # the provider's own count wins


def test_scripts_stop_at_thirty_percent():
    assert allowed("scripts", 20, {"scripts": 29_980})
    assert not allowed("scripts", 20, {"scripts": 29_990})


def test_scripts_never_take_what_is_reserved_for_the_worker():
    # Someone else spent 75,000 today (the key is shared): only 25,000 left, all of it
    # still the worker's (70,000 reserve, worker has used nothing).
    assert not allowed("scripts", 20, {}, remaining=25_000)
    # The worker has used its 70,000: what is left beyond that may go to scripts.
    assert allowed("scripts", 20, {"worker": 70_000}, remaining=30_000)


def test_message_says_when_it_resets():
    with pytest.raises(BudgetExhausted, match="resets at 00:00 UTC"):
        check_allowance("worker", 20, {"worker": ALLOW}, None, ALLOW, RESET)
    with pytest.raises(BudgetExhausted, match="30%.*kept for the production worker"):
        check_allowance("scripts", 20, {"scripts": 30_000}, None, ALLOW, RESET)


# -- the adapter's hooks -------------------------------------------------------------

class Ledger:
    def __init__(self, stop_after=None):
        self.calls, self.stop_after = [], stop_after

    def before_call(self, credits):
        if self.stop_after is not None and len(self.calls) >= self.stop_after:
            raise BudgetExhausted("used up", RESET, "worker")

    def after_call(self, credits, remaining):
        self.calls.append((credits, remaining))


class Credited(FakeBlockscout):
    """Adds Blockscout's x-credits-remaining header to every reply."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.left = 5_000

    def handler(self, request):
        resp = super().handler(request)
        self.left -= 50 if "/api/v2/" in request.url.path else 20
        return httpx.Response(resp.status_code, content=resp.content,
                              headers={**resp.headers, "x-credits-remaining": str(self.left)})


def test_every_call_is_metered_with_the_providers_count(tmp_path):
    fake, ledger = Credited(), Ledger()
    adapter = make(tmp_path, fake, budget=ledger)
    adapter.fetch_transfers(A)
    assert len(ledger.calls) == len(fake.requests) > 0
    assert {c for c, _ in ledger.calls} <= {20, 50}
    assert ledger.calls[-1][1] == fake.left == adapter.credits_remaining
    assert adapter.credits_spent == sum(c for c, _ in ledger.calls)


def test_an_exhausted_budget_stops_before_the_request(tmp_path):
    fake, ledger = Credited(), Ledger(stop_after=2)
    with pytest.raises(BudgetExhausted):
        make(tmp_path, fake, budget=ledger).fetch_transfers(A)
    assert len(fake.requests) == 2                               # the third was never sent


# -- the finished internal range: once a day, from the last edges ----------------------------

class Now:
    def __init__(self):
        self.t = dt.datetime(2026, 11, 19, 12, tzinfo=dt.timezone.utc)

    def __call__(self):
        return self.t


def probes(fake):
    return sum(1 for c in fake.calls if c.get("action") == "txlistinternal" and c.get("offset") == "1")


def test_within_a_day_only_the_top_is_extended(tmp_path):
    fake, now = FakeBlockscout(), Now()
    fake.unfinished = [(0, 400)]
    ranges = {}

    class Store:
        get = staticmethod(lambda chain: ranges.get(chain))
        put = staticmethod(lambda chain, lo, hi, at: ranges.__setitem__(chain, (lo, hi, at)))
        drop = staticmethod(lambda chain: ranges.pop(chain, None))

    first = make(tmp_path / "a", fake, range_store=Store(), now=now).fetch_transfers(A)
    full = probes(fake)
    assert full > 3 and ranges["eth"][:2] == first.internal_range

    fake.latest += 50                                             # new blocks, all finished
    now.t += dt.timedelta(hours=5)
    second = make(tmp_path / "b", fake, range_store=Store(), now=now).fetch_transfers(A)
    assert probes(fake) - full == 1                               # one probe: [old top, new head]
    assert second.internal_range == (first.internal_range[0], first.internal_range[1] + 50)
    assert ranges["eth"][2] == now.t - dt.timedelta(hours=5)      # found_at kept: still the same day's search


def test_after_a_day_the_search_starts_from_the_last_edges(tmp_path):
    fake, now = FakeBlockscout(), Now()
    fake.unfinished = [(0, 400)]
    ranges = {}

    class Store:
        get = staticmethod(lambda chain: ranges.get(chain))
        put = staticmethod(lambda chain, lo, hi, at: ranges.__setitem__(chain, (lo, hi, at)))
        drop = staticmethod(lambda chain: ranges.pop(chain, None))

    first = make(tmp_path / "a", fake, range_store=Store(), now=now).fetch_transfers(A)
    before = probes(fake)
    fake.unfinished = []                                          # Blockscout caught up on old blocks
    now.t += dt.timedelta(hours=25)
    second = make(tmp_path / "b", fake, range_store=Store(), now=now).fetch_transfers(A)
    assert probes(fake) - before == 1                             # [0, old top] is finished; head unchanged
    assert second.internal_range == (0, first.internal_range[1])
    assert ranges["eth"][2] == now.t


def test_a_range_that_no_longer_holds_is_searched_afresh(tmp_path):
    fake, now = FakeBlockscout(), Now()
    ranges = {"eth": (100, 900, now.t - dt.timedelta(hours=30))}

    class Store:
        get = staticmethod(lambda chain: ranges.get(chain))
        put = staticmethod(lambda chain, lo, hi, at: ranges.__setitem__(chain, (lo, hi, at)))
        drop = staticmethod(lambda chain: ranges.pop(chain, None))

    fake.unfinished = [(0, 500)]                                  # 100..900 is not all finished any more
    batch = make(tmp_path, fake, range_store=Store(), now=now).fetch_transfers(A)
    lo, hi = batch.internal_range
    assert 500 < lo <= 510 and hi == 1000 and ranges["eth"] == (lo, hi, now.t)

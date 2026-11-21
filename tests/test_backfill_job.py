"""The Ethereum blacklist backfill as a worker job: slices that stop on time and queue
themselves again, picked up after any restart, finished when Etherscan agrees. No
network: Blockscout and Etherscan are fakes over a made-up multisig history."""
import pytest

from taw import blacklist_backfill as bf
from taw import jobs
from taw.adapters.labels import blacklist as bl
from tests.test_blacklist_backfill import USDT, Chain, addr

S = USDT.start_block
HEAD = bl.CONTRACTS[1].start_block + 20_000          # past USDC's start, so both contracts have a range


class R:
    def __init__(self, data, sha):
        self.data, self.sha = data, sha


class FakeScout:
    """Blockscout over a Chain: its txlist, transactionCount and eth_getLogs."""

    def __init__(self, chain: Chain):
        self.chain, self.requests = chain, 0

    def _block_number(self):
        return HEAD, "d4" * 32

    def _request(self, params):
        self.requests += 1
        rows, _ = self.chain.txlist(int(params["startblock"]), int(params["endblock"]))
        return R({"result": rows}, "a1" * 32)

    def _eth_call(self, to, data, tag):
        self.requests += 1
        return hex(self.chain.count), "c" * 64

    def _rpc(self, method, params):
        self.requests += 1
        f = params[0]
        if f["address"] not in (bf.OWNER_MULTISIG, USDT.contract):
            return R({"result": []}, "c3" * 32)                       # USDC: no events in this history
        logs, _ = self.chain.get_logs(f["address"], f["topics"][0], int(f["fromBlock"], 16), int(f["toBlock"], 16))
        return R({"result": logs}, "b2" * 32)


class FakeEther:
    """Etherscan's getLogs over the same USDT events (USDC: none)."""

    def __init__(self, chain: Chain, drop=0):
        self.chain, self.drop = chain, drop

    def _request(self, params):
        if params["address"] != USDT.contract:
            return R({"result": []}, "e" * 64)
        lo, hi = int(params["fromBlock"]), int(params["toBlock"])
        rows = [g for g in self.chain.usdt_logs if g["topics"][0] == params["topic0"] and lo <= int(g["blockNumber"], 16) <= hi]
        return R({"result": rows[self.drop:]}, "e" * 64)


def history():
    return Chain([(S + 10, "0ecb93c0", addr(1), True, False), (S + 50, "e4997dc5", addr(1), True, False),
                  (S + 9_000, "f3bdc228", addr(4), True, False)], destroy_amounts={addr(4): 465_514_638_375})


def test_a_slice_stops_before_its_next_request_once_its_time_is_up():
    now = [0.0]
    timed = bf.Timed(FakeScout(history()), deadline=10.0, clock=lambda: now[0])
    timed._eth_call("x", "0x", "latest")
    now[0] = 11.0
    with pytest.raises(bf.PauseSlice):
        timed._rpc("eth_getLogs", [{}])
    assert bf.Timed(FakeScout(history()), deadline=None)._eth_call("x", "0x", "latest")[0] == "0x3"


def test_the_worker_queues_a_slice_again_without_counting_the_attempt(monkeypatch):
    from taw.worker import __main__ as w
    sql = []

    class Conn:
        def execute(self, q, params=()):
            sql.append((q, params))
    monkeypatch.setattr(w.lists, "run_backfill",
                        lambda **kw: (_ for _ in ()).throw(jobs.Requeue(jobs.later(5), "slice done, progress saved")))
    status = w.handle(jobs.Job(id=9, kind=jobs.BACKFILL, payload={}, attempts=1, max_attempts=5), data_conn=Conn(),
                      raw_store_factory=lambda: None, adapter_for=None, log=lambda m: None)
    assert status == "queued"
    (q, params), = [x for x in sql if "UPDATE job" in x[0]]
    assert "attempts = GREATEST(attempts - 1, 0)" in q and params[1] == "slice done, progress saved"


def test_the_outcome_says_what_was_written_and_what_is_left():
    out = bf.Outcome(["USDT"], [], ["USDC"], {"USDT AddedBlackList": {"blockscout": 3, "etherscan": 3}}, {})
    assert out.message() == ("eth blacklist backfill: written: USDT; still to do: USDC (USDT AddedBlackList 3/3)")


# -- against a database -----------------------------------------------------------------------

psycopg = pytest.importorskip("psycopg")
from tests.test_store_pg import ACTOR, ADMIN_URL, conns, count, db_url, template_db  # noqa: E402,F401

pg = pytest.mark.skipif(not ADMIN_URL, reason="TEST_DATABASE_URL not set")


@pg
def test_slices_carry_on_after_a_restart_and_finish_when_etherscan_agrees(conns):
    from taw.worker import lists
    data, _ = conns
    _replies_saved(data)
    chain = history()
    assert lists.backfill_needed(data) and jobs.ensure_backfill(data, needed=True)
    assert not jobs.ensure_backfill(data, needed=True)                  # one at a time
    with pytest.raises(jobs.Requeue):                                     # out of time at once: nothing lost
        lists.run_backfill(data_conn=data, raw_store=None, actor=ACTOR, scout=FakeScout(chain),
                           ether=FakeEther(chain), seconds=-1)
    first = FakeScout(chain)
    with pytest.raises(jobs.Requeue):                                     # a short slice, then "a deploy"
        lists.run_backfill(data_conn=data, raw_store=None, actor=ACTOR, scout=_stop_after(first, 3),
                           ether=FakeEther(chain))
    saved = count(data, "SELECT count(*) FROM blacklist_backfill_part")
    assert saved >= 1
    again = FakeScout(chain)
    message = lists.run_backfill(data_conn=data, raw_store=None, actor=ACTOR, scout=again, ether=FakeEther(chain))
    assert message.startswith("eth blacklist backfill: written: USDT, USDC")
    rows = data.execute("SELECT action::text, address, amount_raw FROM blacklist_event WHERE chain = 'eth'"
                        " ORDER BY block_number").fetchall()
    assert rows == [("freeze", addr(1), None), ("unfreeze", addr(1), None), ("destroy", addr(4), 465_514_638_375)]
    assert not lists.backfill_needed(data) and not jobs.ensure_backfill(data, needed=lists.backfill_needed(data))
    assert lists.run_backfill(data_conn=data, raw_store=None, actor=ACTOR) == "eth blacklist backfill: already complete"


@pg
def test_providers_that_differ_write_nothing_and_fail_the_job(conns):
    from taw.worker import lists
    data, _ = conns
    _replies_saved(data)
    chain = history()
    with pytest.raises(RuntimeError, match="NOT written, providers differ: USDT"):
        lists.run_backfill(data_conn=data, raw_store=None, actor=ACTOR, scout=FakeScout(chain),
                           ether=FakeEther(chain, drop=1))
    assert count(data, "SELECT count(*) FROM blacklist_event WHERE chain = 'eth' AND token = 'USDT'") == 0


@pg
def test_a_recent_failure_is_not_retried_in_a_loop(conns):
    data, _ = conns
    data.execute("INSERT INTO job (kind, dedupe_key, payload, status, last_error, finished_at) VALUES"
                 " (%s, %s, '{}'::jsonb, 'failed', 'providers differ', now())", (jobs.BACKFILL, jobs.BACKFILL))
    assert not jobs.ensure_backfill(data, needed=True)


def _stop_after(scout, n):
    """A scout that stops the slice after n requests, as a deploy would."""
    calls = [0]

    class Stopping:
        def __getattr__(self, name):
            target = getattr(scout, name)
            if name not in ("_rpc", "_request", "_eth_call"):
                return target

            def wrapped(*a, **k):
                calls[0] += 1
                if calls[0] > n:
                    raise bf.PauseSlice()
                return target(*a, **k)
            return wrapped
    return Stopping()


def _replies_saved(data):
    """raw_response rows for the fakes' replies, as the bucket store would have written (rule 3)."""
    from tests.test_report_pg import seed_raw
    seed_raw(data, "blockscout", ["a1" * 32, "b2" * 32, "c3" * 32, "c" * 64, "d4" * 32])

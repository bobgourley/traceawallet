"""Daily call budget for metered providers (Blockscout, GoPlus), kept in Postgres.

Blockscout's free tier allows a number of credits a day (BLOCKSCOUT_DAILY_CREDITS,
default 100,000), counted over all uses of our key, and resets at 00:00 UTC. Every
reply carries x-credits-remaining, which is the truth about what is left; the
ledger also counts calls and estimated credits per consumer so the two uses can be
kept apart:

  worker   the production worker. It may use the whole allowance.
  scripts  try_evm, cross_check_evm, check_cases and the like. They may use at most
           30% of the allowance, and never what the worker's 70% has not yet used.

Credits per call are estimates from Blockscout's docs (20 for an ordinary call, 30 to
50 for some REST endpoints; REST is counted as 50 to be safe). The provider's own
remaining count decides when the allowance is used up.

Scripts must run where the database is (inside Railway: railway ssh); without it they
refuse to spend metered calls (script_budget_or_exit).
"""
from __future__ import annotations

import datetime as dt
import os
import sys
from dataclasses import dataclass
from typing import Protocol

PROVIDER = "blockscout"
SCRIPT_SHARE = 0.30


@dataclass(frozen=True)
class Provider:
    name: str           # shown in messages
    env: str            # setting that holds the daily allowance
    default: int
    unit: str           # what the allowance counts


# GoPlus publishes its free plan in compute units (150 a minute, 30,000 a day) but
# not what one address_security call costs, so its ledger counts calls against a
# conservative cap until GoPlus says (DECISIONS.md, 2026-11-20 "Labels: GoPlus").
PROVIDERS = {
    "blockscout": Provider("Blockscout", "BLOCKSCOUT_DAILY_CREDITS", 100_000, "credits"),
    "goplus": Provider("GoPlus", "GOPLUS_DAILY_CALLS", 1_000, "calls"),
}


def daily_allowance(provider: str = PROVIDER) -> int:
    p = PROVIDERS[provider]
    return int(os.environ.get(p.env, str(p.default)))


def next_reset(now: dt.datetime | None = None) -> dt.datetime:
    now = now or dt.datetime.now(dt.timezone.utc)
    return dt.datetime.combine(now.date() + dt.timedelta(days=1), dt.time(), dt.timezone.utc)


class BudgetExhausted(RuntimeError):
    """No calls left for this consumer today. `resets_at` says when to try again."""

    def __init__(self, message: str, resets_at: dt.datetime, consumer: str, provider: str = "Blockscout"):
        super().__init__(message)
        self.resets_at, self.consumer = resets_at, consumer
        self.provider = provider


class Budget(Protocol):
    def before_call(self, credits: int) -> None: ...

    def after_call(self, credits: int, remaining: int | None) -> None: ...


def check_allowance(consumer: str, credits: int, used: dict[str, int], remaining: int | None, allowance: int,
                    resets_at: dt.datetime, provider: str = PROVIDER) -> None:
    """Raise BudgetExhausted if `consumer` may not spend `credits` more today.

    remaining is the provider's own count (None if not seen today; then the ledger
    is used). The worker may spend the whole allowance. Scripts may spend at most
    SCRIPT_SHARE of it, and never what is still reserved for the worker (the
    worker's 70% minus what the worker has used).
    """
    p = PROVIDERS[provider]
    if remaining is None:
        remaining = allowance - sum(used.values())
    if remaining - credits < 0:
        raise BudgetExhausted(f"{p.name}'s daily allowance is used up; it resets at 00:00 UTC.", resets_at, consumer,
                              p.name)
    if consumer == "scripts":
        share = int(allowance * SCRIPT_SHARE)
        worker_reserve = max(0, allowance - share - used.get("worker", 0))
        if used.get("scripts", 0) + credits > share or remaining - credits < worker_reserve:
            raise BudgetExhausted(
                f"Scripts have used their share of {p.name}'s daily allowance ({SCRIPT_SHARE:.0%}, {share:,} "
                f"{p.unit}); the rest is kept for the production worker. It resets at 00:00 UTC.", resets_at,
                consumer, p.name)


class PostgresBudget:
    """Ledger in provider_budget (per day and consumer) and provider_status (the
    provider's last reported remaining credits). Uses an autocommit connection."""

    def __init__(self, conn, consumer: str, *, provider: str = PROVIDER, allowance: int | None = None,
                 now=lambda: dt.datetime.now(dt.timezone.utc)):
        if consumer not in ("worker", "scripts"):
            raise ValueError(consumer)
        self.conn, self.consumer, self.provider = conn, consumer, provider
        self.allowance = allowance or daily_allowance(provider)
        self._now = now

    def _today(self) -> dt.date:
        return self._now().date()

    def used(self) -> dict[str, int]:
        rows = self.conn.execute("SELECT consumer, credits FROM provider_budget WHERE provider = %s AND day = %s",
                                 (self.provider, self._today())).fetchall()
        return {c: int(n) for c, n in rows}

    def remaining(self) -> int | None:
        """The provider's own count, if it was reported today."""
        row = self.conn.execute("SELECT credits_remaining, observed_at FROM provider_status WHERE provider = %s",
                                (self.provider,)).fetchone()
        if row is None or row[1].astimezone(dt.timezone.utc).date() != self._today():
            return None
        return int(row[0])

    def before_call(self, credits: int) -> None:
        check_allowance(self.consumer, credits, self.used(), self.remaining(), self.allowance,
                        next_reset(self._now()), self.provider)

    def after_call(self, credits: int, remaining: int | None) -> None:
        self.conn.execute(
            "INSERT INTO provider_budget (provider, day, consumer, calls, credits) VALUES (%s, %s, %s, 1, %s)"
            " ON CONFLICT (provider, day, consumer) DO UPDATE"
            " SET calls = provider_budget.calls + 1, credits = provider_budget.credits + EXCLUDED.credits",
            (self.provider, self._today(), self.consumer, credits))
        if remaining is not None:
            self.conn.execute(
                "INSERT INTO provider_status (provider, credits_remaining, observed_at) VALUES (%s, %s, %s)"
                " ON CONFLICT (provider) DO UPDATE"
                " SET credits_remaining = EXCLUDED.credits_remaining, observed_at = EXCLUDED.observed_at",
                (self.provider, remaining, self._now()))


class PostgresRangeStore:
    """Blockscout's finished internal-transaction range per chain, shared by every
    worker process and kept across restarts."""

    def __init__(self, conn):
        self.conn = conn

    def get(self, chain: str) -> tuple[int, int, dt.datetime] | None:
        row = self.conn.execute("SELECT low_block, high_block, found_at FROM blockscout_internal_range"
                                " WHERE chain = %s", (chain,)).fetchone()
        return (int(row[0]), int(row[1]), row[2]) if row else None

    def put(self, chain: str, low: int, high: int, found_at: dt.datetime) -> None:
        self.conn.execute(
            "INSERT INTO blockscout_internal_range (chain, low_block, high_block, found_at) VALUES (%s, %s, %s, %s)"
            " ON CONFLICT (chain) DO UPDATE SET low_block = EXCLUDED.low_block, high_block = EXCLUDED.high_block,"
            " found_at = EXCLUDED.found_at", (chain, low, high, found_at))

    def drop(self, chain: str) -> None:
        self.conn.execute("DELETE FROM blockscout_internal_range WHERE chain = %s", (chain,))


def script_budget_or_exit():
    """(budget, range_store) for a script, or exit with a plain message. Scripts may
    only spend metered calls where the ledger is, i.e. inside Railway."""
    from taw import config
    try:
        import psycopg
        conn = psycopg.connect(config.DATABASE_URL or "", autocommit=True, connect_timeout=5)
        conn.execute("SELECT 1 FROM provider_budget LIMIT 1")
    except Exception as exc:                                           # noqa: BLE001
        sys.exit("This script spends Blockscout calls, which are metered in the database. Run it inside "
                 "Railway, e.g. railway ssh --service traceawallet-worker -- python scripts/<name>.py ...\n"
                 f"(could not open the ledger: {type(exc).__name__})")
    return PostgresBudget(conn, "scripts"), PostgresRangeStore(conn)

# Decisions

Choices made while building, and why. Newest first. A later decision that
reverses an earlier one says so; nothing here is edited away.

## 2026-11-17: shipping through CI

**Branch, green CI, then ask.** Railway deploys `main`, so changes go to a
branch first. GitHub Actions runs every test against a throwaway Postgres 16
with all migrations applied (by the production runner, then again to prove it
is a no-op). A skipped test fails the run. Merging to `main` waits for the
owner. The rule is in CLAUDE.md. This supersedes the "store tests need a real
Postgres" note below: they now run in CI.

**Snapshots follow the evidence-table rules.** An audit of 0004 before it
shipped found that its list of responses was a `bigint[]`, and Postgres cannot
enforce foreign keys on an array. `address_snapshot` now has `raw_response_id
NOT NULL` (the balance response), like `transfer`. The full set of responses is
in `address_snapshot_response`, with foreign keys at both ends. Both tables
block UPDATE, DELETE and TRUNCATE and grant only SELECT and INSERT to
`taw_app`. Database tests check these rules on every evidence table the worker
writes.

## 2026-11-17: worker and store

**Snapshots get their own table (`address_snapshot`, migration 0004).**
The handler needs somewhere to keep balances and contract status at a block
height. The 0001 comments point at `report.data`, but the report builder
(canonical JSON, hash, template version) does not exist yet, and writing report
rows now would fix its format before it is designed. `address_snapshot` holds
plain observations, one immutable row per fetch. The report builder will read
it. The 10-minute freshness check also reads it.

**Raw responses commit on their own; lookup data commits all at once.**
Each provider response is committed as it arrives, on a separate connection.
Addresses, transfers and the snapshot are written in one transaction at the
end. A job that fails halfway still leaves an honest record of what was fetched,
which a retry can reuse, and never leaves half a lookup in the evidence tables.
Bucket objects whose rows rolled back are harmless orphans.

**Only unchangeable responses are reused.** Before fetching a confirmed
transaction's events, the adapter asks the store for a saved HTTP 200 copy of
the exact request and uses it. Balances, account pages and block heights are
always fetched fresh. A saved copy with no events is fetched again, since an
empty answer can come from a node that is behind. Reused bodies are re-hashed
against their sha256 when read back.

**Freshness: 10 minutes, measured from `address_snapshot.fetched_at`.** A
reused lookup finishes the job without calling the provider and writes nothing.

**Retries.** Waits of 30 s, 2 min, 8 min, and so on (30 s × 4^(attempt−1)), up
to the job's `max_attempts` (3 by default). Failures that a retry cannot fix
fail at once: a TronGrid 403 (key blocked), a malformed address, a chain with no
adapter, a missing API key, a request that would have stored a secret, and a
saved body that fails its hash check. `last_error` is written for a person to
read.

**Stale claims.** A job still `running` 30 minutes after its claim is treated as
abandoned by a dead worker and can be claimed again; the claim counts as an
attempt. Without this, the one-live-job-per-address index would block that
address forever.

**Job payload is exactly `{chain, address}`.** The handler refuses any other
key. The address is normalized (Tron: base58) before the job is queued, so the
dedupe key is stable.

**Identical transfers in one transaction stay separate.** Each listed TRC-20
transfer claims its own event. When no event matches, the fallback index starts
above the transaction's highest real log index, so it can never equal a matched
one and be merged by the unique key.

**Two meanings of "truncated".** `TransferBatch.truncated`: more than the
2,000-transfer limit exists. `AddressSnapshot.truncated`: more than 50 tokens
held, so some balances lack decimals and symbol. Both are stored on the snapshot
row (`transfers_truncated`, `tokens_truncated`).

**Live checks run inside Railway, not from a laptop.** The database is reachable
only on Railway's private network, which is right. Scripts that touch the
real database run with `railway ssh` on the worker service. We do not add a
public database address for convenience.

**Store tests need a real Postgres.** `tests/test_store_pg.py` creates a
throwaway database from `TEST_DATABASE_URL` and is skipped without one. A
bundled local Postgres (`pgserver`) was tried and not installed (permission
was not given). Until a test database exists, the store's SQL is proven by
the live run on Railway.

## Open

- Services connect as `postgres`, the owner. 0001 defines a restricted
  `taw_app` role that is not used yet.
- Nothing yet records lookups in `lookup` or updates `lookup_count`. The
  worker only fetches and stores observations.

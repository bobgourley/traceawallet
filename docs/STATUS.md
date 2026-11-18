# Status

Last updated 2026-11-17. Design choices and their reasons are in
[DECISIONS.md](DECISIONS.md).

## Live

- `traceawallet-web` and `traceawallet-worker` run on Railway (project "adaptable-connection").
- `/healthz` reports 8 chains (six EVM chains, Tron, Bitcoin).
- Migrations 0001 to 0003 are applied; 0004 (`address_snapshot`) is applied on the next deploy.

## Worker: `address_lookup` jobs (Tron only so far)

Queue one with:

    railway ssh --service traceawallet-worker -- python scripts/enqueue.py tron T... --wait 300

And inspect what was stored (read-only):

    railway ssh --service traceawallet-worker -- python scripts/check_lookup.py tron T...

What a job does:

1. Reads the payload, which is exactly `{chain, address}` and holds nothing about a user.
2. If the address was fetched less than 10 minutes ago, reuses that and finishes.
3. Otherwise takes a snapshot (balances, contract status, block height) and up to 2,000 transfers.
   Events for confirmed transactions that were saved before are reused rather than fetched again.
4. Saves through `taw/store.py`: every raw response goes to the bucket with a `raw_response` row,
   committed as it arrives. Addresses, transfers and the snapshot are written together in one
   transaction. Re-running adds nothing and raises nothing.
5. On failure it retries after 30 s, 2 min, 8 min, up to `max_attempts`, then marks the job failed
   with a plain-language `last_error`. A TronGrid 403, a bad address or a missing key fails the job
   at once.

## Code

| Path | What it is |
|---|---|
| `taw/adapters/base.py` | Result dataclasses, `ChainAdapter`, `RawStore` (local folder, bucket) |
| `taw/adapters/chains/tron_trongrid.py` | Tron via TronGrid |
| `taw/adapters/registry.py` | Which adapter serves which chain |
| `taw/store.py` | Database writes for every chain |
| `taw/jobs.py` | Queue: enqueue, claim, retry, finish |
| `taw/worker/lookup.py` | The `address_lookup` handler and failure wording |
| `scripts/try_tron.py` | Try the adapter without a database |
| `scripts/enqueue.py`, `scripts/check_lookup.py` | Queue a lookup; inspect results |

## Tests

    .venv/bin/python -m pytest

56 tests run anywhere, without network access. 18 more (`tests/test_store_pg.py`) need a real
Postgres through `TEST_DATABASE_URL`. They are skipped locally and run in GitHub Actions
(`.github/workflows/tests.yml`) against a throwaway Postgres 16, where a skipped test fails the run.

The tests cover:

- Tron adapter replay from saved responses
- identical transfers in one transaction
- reuse of saved responses
- retries, 403 and pacing
- the raw stores
- the handler's happy paths, payload rules, retries and failure wording

## Checked against Tronscan (2026-11-17)

Address `TL7me8SQkNmieVngj9sPQGG2PSMENASHug` at block 88,091,753. TRX and USDT counts, totals,
first and last activity and the top destinations all matched. Tronscan also shows four TRC-10
airdrop tokens, which we do not read yet. The 0.69 TRX gap between TRX in and out is network fees.

## Known gaps

- TRC-10 tokens, TRX moved by contracts (internal transactions) and fees are not read.
- Only Tron has an adapter. EVM and Bitcoin jobs fail with "not built yet".
- Nothing creates `lookup` rows or updates `lookup_count` yet. The website does not queue jobs yet.
- Services connect as the database owner; the restricted `taw_app` role is unused.

## Next

1. Website: lookup form, Turnstile, Clerk, queue the job, record `lookup`.
2. Report builder over `address_snapshot` and `transfer`.
3. TRC-10, then the EVM adapter (Etherscan API V2), then Bitcoin (Esplora).

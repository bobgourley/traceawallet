# Status

Last updated 2026-11-20. Design choices and their reasons are in
[DECISIONS.md](DECISIONS.md).

## Live

- `traceawallet-web` and `traceawallet-worker` run on Railway (project "adaptable-connection").
- `/healthz` reports 8 chains (six EVM chains, Tron, Bitcoin).
- Migrations 0001 to 0009 are applied.
- The worker handles `address_lookup` jobs for every Phase 1 chain. Nothing queues them automatically
  yet.
  - Tron, through TronGrid.
  - eth, arbitrum and optimism, through Blockscout's free tier. Etherscan is used only in tests and
    cross-checks.
  - btc, through mempool.space, cross-checked against Blockstream.
- polygon and base (they need a paid Blockscout plan) and bsc (no source with suitable terms) fail
  at once with a plain reason.
- Every lookup runs the completeness checks and stores each result on its snapshot.

## Live check, Bitcoin (2026-11-20, production database and bucket)

Three lookups of `bc1q4t93pz7k8xaqsckmh30we380xw8ggtq4xanvmp` on `btc` through mempool.space, run by the
deployed worker.

| | Job 11 (03:01) | Job 12 (03:01, 8 s later) | Job 13 (03:12, after 10 min) |
|---|---|---|---|
| Outcome | full fetch, 4 s | reused snapshot 8, 1 s | full fetch, 4 s |
| Snapshot | 8, height 976,672 | 8 | 9, height 976,673 |
| Rows | 154 saved | 154 reused | 32 new, 154 reused (2 more transactions) |
| `native_reconciliation` | passed: 1,733,405 sat by our rows, provider totals and unspent outputs | | passed: 2,051,171 sat, all three |
| `tx_count` | passed: 14 = 14; 2 unconfirmed ignored | | passed: 16 = 16 (the 2 had confirmed) |
| `history_incomplete` | false | false | false |
| duplicate transfer keys | 0 | 0 | 0 |
| `event_log_verify()` | NULL (intact) | NULL | NULL |

`lookup_completed` events:

    31 03:01:46 snapshot 8 height 976672 transfers_saved 154 transfers_reused 0   snapshot_reused false history_incomplete false
    32 03:01:54 snapshot 8 height 976672 transfers_saved 0   transfers_reused 154 snapshot_reused true  history_incomplete false
    33 03:12:16 snapshot 9 height 976673 transfers_saved 32  transfers_reused 154 snapshot_reused false history_incomplete false

## Live check, Ethereum through Blockscout (2026-11-20)

Jobs 8 to 10 on `0xaab6417993d04a861f1f02c5c4bd2e9145d8f637`: a full fetch, a reuse, then a full fetch
with 0 new transfers. All checks that can run passed. Native reconciliation was not run, because
Blockscout's internal range starts at block 23,398,420. Details in DECISIONS.md.

## Live check, Ethereum through Etherscan (2026-11-19, production database and bucket)

Three lookups of `0xaab6417993d04a861f1f02c5c4bd2e9145d8f637` on `eth`, queued with
`scripts/enqueue.py` through `railway ssh` and run by the deployed worker.

| | Job 5 (22:13) | Job 6 (22:13, 18 s later) | Job 7 (22:26, after 10 min) |
|---|---|---|---|
| Outcome | full fetch, 9 s | reused snapshot 4, 1 s | full fetch, 8 s |
| Snapshot | 4, block 26,452,034 | 4 | 5, block 26,452,100 |
| eth address rows | 8 | 8 | 8 |
| transfers touching the address | 49 (49 new) | 49 | 49 (0 new) |
| duplicate transfer keys | 0 | 0 | 0 |
| raw_response rows (all sources) | 58 | 58 | 76 (18 more) |
| bucket objects | 48 | 48 | 52 (most bodies identical to job 5) |
| `event_log_verify()` | NULL (intact) | NULL | NULL |

`lookup_completed` events, one per job, each with only chain, address, snapshot, block and counts:

    14 22:13:14 snapshot 4 block 26452034 transfers_saved 49 transfers_reused 0  snapshot_reused false
    15 22:13:24 snapshot 4 block 26452034 transfers_saved 0  transfers_reused 49 snapshot_reused true
    16 22:26:36 snapshot 5 block 26452100 transfers_saved 0  transfers_reused 49 snapshot_reused false

Every job finished on attempt 1 with no `last_error`.

## Live check, Tron (2026-11-18, production database and bucket)

Three lookups of `TL7me8SQkNmieVngj9sPQGG2PSMENASHug`, queued with `scripts/enqueue.py` through
`railway ssh` and processed by the deployed worker. The tables started empty.

| | Job 1 (02:16) | Job 2 (02:16, 35 s later) | Job 3 (02:27, after 10 min) |
|---|---|---|---|
| Outcome | full fetch, 14 s | reused snapshot 1, 2 s | full fetch, 9 s |
| address rows | 20 | 20 | 20 |
| transfer rows | 35 (35 new) | 35 | 35 (0 new) |
| duplicate transfer keys | 0 | 0 | 0 |
| raw_response rows | 30 | 30 | 35 (events reused) |
| bucket objects | 30 | 30 | 34 (one body identical to run 1) |
| address_snapshot rows | 1 | 1 | 2 |
| `event_log_verify()` | NULL (intact) | NULL | NULL |

Every job finished on attempt 1 with no `last_error`. The payload held only chain and address.

## Worker: `address_lookup` jobs

Queue one with:

    railway ssh --service traceawallet-worker -- python scripts/enqueue.py tron T... --wait 300
    railway ssh --service traceawallet-worker -- python scripts/enqueue.py eth 0x... --wait 300
    railway ssh --service traceawallet-worker -- python scripts/enqueue.py btc bc1q... --wait 300

One job covers one chain. To look up an EVM address on three chains, queue three jobs.

And inspect what was stored (read-only):

    railway ssh --service traceawallet-worker -- python scripts/check_lookup.py tron T...

What a job does:

1. Reads the payload, which is exactly `{chain, address}` and holds nothing about a user.
2. If the address was fetched less than 10 minutes ago, reuses that and finishes.
3. Otherwise takes a snapshot (balances, contract status, block height) and up to 2,000 transfers
   (1,000 transactions on Bitcoin), always adding the earliest. Then it runs the completeness checks:
   - balance reconciliations
   - nonce or transaction count
   - the provider's two lists agree

   Each check's result is stored on the snapshot. A failed check marks the history as possibly
   incomplete.
4. Saves through `taw/store.py`: every raw response goes to the bucket with a `raw_response` row,
   committed as it arrives. Addresses, transfers and the snapshot are written together in one
   transaction. Re-running adds nothing and raises nothing.
5. On failure it retries after 30 s, 2 min, 8 min, up to `max_attempts`, then marks the job failed
   with a plain-language `last_error`. Some failures end the job at once:
   - a TronGrid 403, a bad address, or a missing or rejected key
   - a chain outside Phase 1

   A used-up Blockscout allowance puts the job back to run at 00:05 UTC without counting the
   attempt. Scripts may use 30% of Blockscout's daily allowance; the ledger is in
   `provider_budget` (DECISIONS.md, 2026-11-20 "Blockscout daily budget"). Blockscout's "not yet
   processed" answer is retried, never stored.

## Code

| Path | What it is |
|---|---|
| `taw/adapters/base.py` | Result dataclasses, `ChainAdapter`, `RawStore` (local folder, bucket) |
| `taw/adapters/chains/tron_trongrid.py` | Tron via TronGrid |
| `taw/adapters/chains/evm_etherscan.py` | EVM chains via Etherscan API V2 (tests and cross-checks only) |
| `taw/adapters/chains/evm_blockscout.py` | eth, arbitrum, optimism via the Blockscout PRO API (what the worker uses) |
| `taw/adapters/chains/btc_esplora.py`, `btc_address.py` | Bitcoin via mempool.space and Blockstream; address checksums |
| `taw/reconcile.py` | The completeness checks, the same names on every chain |
| `taw/adapters/chains/evm_crosscheck.py` | Compare two providers' results at one block |
| `taw/adapters/chains/evm_address.py` | EVM address checks, EIP-55 checksum, Keccak-256 |
| `taw/adapters/chains/common.py` | Request pacer and ABI decoding shared by adapters |
| `taw/adapters/registry.py` | Which adapter serves which chain |
| `taw/store.py` | Database writes for every chain |
| `taw/jobs.py` | Queue: enqueue, claim, retry, finish |
| `taw/worker/lookup.py` | The `address_lookup` handler and failure wording |
| `scripts/try_tron.py`, `scripts/try_evm.py`, `scripts/try_btc.py` | Try an adapter without a database |
| `scripts/cross_check_evm.py`, `scripts/cross_check_btc.py` | Run two providers at one height and list every difference |
| `scripts/enqueue.py`, `scripts/check_lookup.py` | Queue a lookup; inspect results |
| `scripts/purge_source_bodies.py` | Delete one source's bodies from the bucket, keep the rows (dry run by default; never run for real) |

## Tests

    .venv/bin/python -m pytest

234 tests run anywhere, without network access. 32 more (`tests/test_store_pg.py`) need a real
Postgres through `TEST_DATABASE_URL`. They are skipped locally and run in GitHub Actions
(`.github/workflows/tests.yml`) against a throwaway Postgres 16, where a skipped test fails the run.

The tests cover:

- Tron adapter replay from saved responses
- Etherscan adapter replay from saved responses, plus a fake Etherscan for the edge cases: key never
  stored, failed transactions skipped, identical transfers in one transaction kept apart, NFT
  transfers ignored, errors inside HTTP 200 bodies, paging past the 10,000-row window, truncation
  that keeps the first transfers
- identical transfers in one transaction
- reuse of saved responses
- retries, 403 and pacing
- the raw stores
- the handler's happy paths, payload rules, retries and failure wording
- purging one source's bodies: rows and other sources untouched, shared bodies kept, the purge logged
- the Phase 1 chain coverage lists
- Blockscout replays (Ethereum, Arbitrum, Optimism) and both live cross-checks, replayed
- Bitcoin replays from mempool.space and Blockstream, plus a fake Esplora server for coinbase inputs,
  unconfirmed and above-height transactions, a count mismatch, truncation and 429s
- every completeness check, including the Blockscout response that returned 6 of 41 token transfers

## Checked against Tronscan (2026-11-17)

Address `TL7me8SQkNmieVngj9sPQGG2PSMENASHug` at block 88,091,753. TRX and USDT counts, totals,
first and last activity and the top destinations all matched. Tronscan also shows four TRC-10
airdrop tokens, which we do not read yet. The 0.69 TRX gap between TRX in and out is network fees.

## Checked against etherscan.io (2026-11-19)

Address `0xaab6417993d04a861f1f02c5c4bd2e9145d8f637` on Ethereum at block 26,451,986, run with
`scripts/try_evm.py`. 18 requests.

| | Ours | etherscan.io | Why they differ |
|---|---|---|---|
| ETH balance | 0.000428974286563892 | 0.000428974286563892 | same |
| Token balances | none | none listed | same; USDC and USDT were spent to zero |
| Transactions | 7 native transfers | 27 transactions | 20 of the 27 move no ETH: they are the wallet's own `transfer()` calls on USDC (16) and USDT (4), which we record as its 20 outgoing token transfers |
| Internal | 1 in, 0.25022341 ETH | 1, 0.25022341 ETH from "Coinbase 10" | same; we do not read Etherscan's labels |
| Token transfers | 41 | "A total of 41 txns" | same |
| USDC | 17 in, 16 out, 9,280.39 each way | same rows | same |
| USDT | 4 in, 4 out, 2,569.517887 each way | same rows, sum shows 2,569.49 | Etherscan rounds each row to 2 decimals |
| First activity | 2026-09-02 11:53:11, 1,008.584979 USDT in | 2026-09-02 11:53:11 | same |
| First ETH in | block 25,889,440, tx `0x36d76b22…` from `0xc5267f13…` | "Funded By OKX: Deposit Funder 20", same tx | same; Etherscan's "Funded By" means first ETH, our "first funder" means first incoming of any kind (the USDT, 8 blocks earlier) |
| Outbound stablecoins | 16 USDC to `0x445f1631…`, 4 USDT to `0x559432e1…` | to "OKX 225" and "OKX: Hot Wallet 5" | same addresses |

ETH in (0.823659 native + 0.250223 internal) minus ETH out (1.072979) minus the balance leaves
0.000474 ETH, which is gas for the 27 transactions. We do not read fees.

## Railway settings (dashboard, not repo)

There is no `railway.json`; Railway stopped supporting it on 2026-12-01. Both `traceawallet-web`
and `traceawallet-worker` must have these in their dashboard settings:

| Setting | Value |
|---|---|
| Deploy → Custom Start Command | `sh start.sh` |
| Deploy → Restart Policy | On Failure |
| Deploy → Regions | US East (Virginia) |
| Build → Watch Paths | `**`, `!/docs/**`, `!/*.md`, `!/**/*.md` (one per line) |

Postgres, its volume and the bucket are in US East too. Check with
`railway environment config --json`.

## Known gaps

- Tron: TRC-10 tokens and TRX moved by contracts (internal transactions) are not read. Native
  reconciliation is not run: staking and resource delegation are not parsed.
- EVM:
  - polygon, base and bsc are not checked in Phase 1.
  - Blockscout reads internal transfers only where it has finished indexing. On Arbitrum that is
    almost nowhere (one block on 2026-11-20).
  - Optimism's L1 data fee is not read, so its native reconciliation is not run.
  - NFTs are not read.
  - Token balances cover the freezable stablecoins and the 20 most-used tokens.
- Bitcoin:
  - For a wallet with more than 10,000 transactions the earliest transaction is not reached.
  - For a wallet with very many unspent outputs the provider may refuse to list them.
- No prices yet. Prices will come from Chainlink feeds (DECISIONS.md, 2026-11-20).
- Nothing creates `lookup` rows or updates `lookup_count` yet. The website does not queue jobs yet.
- Services connect as the database owner; the restricted `taw_app` role is unused.

## Next

1. Prices (branch `prices`): built. After each lookup the worker reads Chainlink feeds for the
   transfers' (asset, bucket) pairs into the `price` table, at most 40 new buckets a lookup. Live
   check pending Blockscout's credit reset.
2. Label loaders: OFAC SDN list, GoPlus (never for Bitcoin; logo and "Powered by GoPlus"),
   stablecoin freeze checks, the exchange wallet file.
3. Transaction matching: find the victim's own transfer from what they entered.
4. The report.

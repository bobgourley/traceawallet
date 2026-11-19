# Status

Last updated 2026-11-19. Design choices and their reasons are in
[DECISIONS.md](DECISIONS.md).

## Live

- `traceawallet-web` and `traceawallet-worker` run on Railway (project "adaptable-connection").
- `/healthz` reports 8 chains (six EVM chains, Tron, Bitcoin).
- Migrations 0001 to 0004 are applied.
- The worker handles `address_lookup` jobs for Tron. Nothing queues them automatically yet.
- EVM lookups (Etherscan) are built on branch `evm-etherscan` and not deployed yet.

## Live check (2026-11-18, production database and bucket)

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

## Worker: `address_lookup` jobs (Tron and EVM chains)

Queue one with:

    railway ssh --service traceawallet-worker -- python scripts/enqueue.py tron T... --wait 300
    railway ssh --service traceawallet-worker -- python scripts/enqueue.py eth 0x... --wait 300

One job covers one chain. To look up an EVM address on three chains, queue three jobs.

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
   at once. So does an EVM chain that Etherscan's free plan does not cover (BNB Smart Chain, OP
   Mainnet, Base), a rejected Etherscan key, or a used-up daily allowance.

## Code

| Path | What it is |
|---|---|
| `taw/adapters/base.py` | Result dataclasses, `ChainAdapter`, `RawStore` (local folder, bucket) |
| `taw/adapters/chains/tron_trongrid.py` | Tron via TronGrid |
| `taw/adapters/chains/evm_etherscan.py` | EVM chains via Etherscan API V2, one instance per chain |
| `taw/adapters/chains/evm_address.py` | EVM address checks, EIP-55 checksum, Keccak-256 |
| `taw/adapters/chains/common.py` | Request pacer and ABI decoding shared by adapters |
| `taw/adapters/registry.py` | Which adapter serves which chain |
| `taw/store.py` | Database writes for every chain |
| `taw/jobs.py` | Queue: enqueue, claim, retry, finish |
| `taw/worker/lookup.py` | The `address_lookup` handler and failure wording |
| `scripts/try_tron.py`, `scripts/try_evm.py` | Try an adapter without a database |
| `scripts/enqueue.py`, `scripts/check_lookup.py` | Queue a lookup; inspect results |

## Tests

    .venv/bin/python -m pytest

114 tests run anywhere, without network access. 23 more (`tests/test_store_pg.py`) need a real
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

- TRC-10 tokens, TRX moved by contracts (internal transactions) and fees are not read.
- Bitcoin has no adapter; its jobs fail with "not built yet".
- EVM: BNB Smart Chain, OP Mainnet and Base need a paid Etherscan plan. NFTs (ERC-721, ERC-1155),
  fees, and tokens whose Transfer event does not index from and to are not read. Token balances
  cover the freezable stablecoins and the 20 most-used tokens only.
- Etherscan's terms limit redistribution and archiving of its data; see DECISIONS.md (Open).
- Nothing creates `lookup` rows or updates `lookup_count` yet. The website does not queue jobs yet.
- Services connect as the database owner; the restricted `taw_app` role is unused.

## Next

1. Website: lookup form, Turnstile, Clerk, queue the job, record `lookup`.
2. Report builder over `address_snapshot` and `transfer`.
3. TRC-10, then Bitcoin (Esplora).

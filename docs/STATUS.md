# Status

Last updated 2026-11-17.

## Live

- `traceawallet-web` and `traceawallet-worker` run on Railway (project "adaptable-connection").
- Migrations 0001 to 0003 are applied. `/healthz` reports 8 chains (six EVM chains, Tron, Bitcoin).
- The worker claims jobs but has no handlers yet, so every job fails with "no handler".

## Built, not yet wired in

### Adapter base (`taw/adapters/base.py`)

- `AddressSnapshot`, `TokenBalance`, `TransferRecord` (mirrors the `transfer` table) and `TransferBatch`.
- `ChainAdapter` protocol: `fetch_snapshot(address)` and `fetch_transfers(address, limit)`.
- `RawStore` protocol with two implementations:
  - `LocalRawStore` writes to `.raw/` (gitignored), for scripts and tests.
  - `BucketRawStore` writes to the bucket (`BUCKET_*` variables) and inserts a `raw_response` row on
    the caller's connection. It has only been tested against a stand-in bucket and database. It has
    not written to the real bucket yet.
- Both stores drop credential-looking fields from the stored URL and params. They refuse to store
  anything that contains the value of a `*KEY*`, `*SECRET*`, `*TOKEN*`, `*PASSWORD*` or `*SALT*`
  environment variable.

### Tron adapter (`taw/adapters/chains/tron_trongrid.py`)

- Reads the TRX balance, TRC-20 balances (with decimals and symbol read from each token contract),
  whether the address is a contract, TRX transfers, TRC-20 transfers and the latest confirmed block.
- Uses confirmed data only. The key goes in the `TRON-PRO-API-KEY` header. Requests are paced to
  9 a second, with backoff on 429 and 503 (Retry-After is honoured). A 403 stops it at once,
  because TronGrid's docs say a 403 means the key is temporarily blocked.
- Stops at 2,000 transfers. The newest 2,000 are kept and `truncated` is set.
- TronGrid's TRC-20 list has no block number or log index, so the adapter makes one call to
  `/v1/transactions/{tx}/events` per transaction. A busy address at the 2,000 cap takes about 4
  minutes.
- `AddressSnapshot.truncated` means more than 50 tokens are held. All balances are listed, but those
  past 50 have no decimals or symbol. Transfer truncation is on `TransferBatch.truncated`.

### Trial script

    railway run --service traceawallet-worker .venv/bin/python scripts/try_tron.py T...

### Tests

    .venv/bin/python -m pytest

There are 30 tests and none of them touch the network. The Tron tests replay responses saved from a
live run on `TL7me8SQkNmieVngj9sPQGG2PSMENASHug` (in `tests/fixtures/tron/`), plus hand-built
responses for paging, retries, 403, token metadata and skipped transaction types.

## Checked against Tronscan (2026-11-17)

Address `TL7me8SQkNmieVngj9sPQGG2PSMENASHug`, as of block 88,091,753:

| | TraceAWallet | Tronscan |
|---|---|---|
| TRX balance | 0 | 0 |
| TRX in | 7, 14.000013 | 7, 14.000013 |
| TRX out | 3, 13.310013 | 3, 13.310013 |
| USDT in | 23, 6,527.458759 | 23, 6,527.458759 |
| USDT out | 2, 6,527.458759 | 2, 6,527.458759 |
| First activity | 2026-10-19 09:18:51 | 2026-10-19 09:18:51 |
| Last activity | 2026-10-24 15:32:03 | 2026-10-24 15:32:03 |
| Contract | no | no (accountType 0) |
| TRC-10 tokens | not read | 4 airdropped tokens |

The TRX difference (14.000013 in, 13.310013 out, balance 0) is 0.69 TRX burned as network fees.
Neither side lists fees as transfers.

## Known gaps

- TRC-10 tokens are not read. The test address holds four airdropped TRC-10 tokens that Tronscan
  shows ("TRC20Ucom", "GasFree4uCOM", "gas.bi", "trxfee.io"). They look like spam ads, and spam
  airdrops can matter to an investigation, so they should be added.
- TRX moved by smart contracts (internal transactions) is not read.
- Fees burned are not recorded.
- Nothing writes `TransferRecord`s to the database yet. The writer must resolve addresses to
  `address.id` and map `raw_sha256` to `raw_response.id` (`BucketRawStore.row_ids`).
- If a listed TRC-20 transfer has no matching event, the adapter falls back to the transfer's
  position within the transaction as the index. That has not happened on real data yet.

## Next

1. Worker handler for `address_lookup` on Tron: snapshot, then transfers, then database rows.
2. TRC-10 balances and transfers.
3. EVM adapter (Etherscan API V2), then Bitcoin (Esplora).

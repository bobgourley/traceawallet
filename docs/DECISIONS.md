# Decisions

Choices made while building, and why. Newest first. A later decision that
reverses an earlier one says so; nothing here is edited away.

## 2026-11-19: Etherscan's terms and Phase 1 chain coverage

**Etherscan is a Phase 1 display source only.** Nothing that comes from
Etherscan may feed a public API. Phase 2 needs an EVM source whose terms permit
redistribution. Migration 0005 writes this, the terms we read, and
`terms_checked_on = 2026-11-19` onto the `etherscan` source row.
`resale_permitted` stays false.

**We can destroy Etherscan's bodies and keep the record.** Nothing in the
database points into the bucket except the `bucket_key` text column, so deleting
objects breaks no row, hash or foreign key. `scripts/purge_source_bodies.py
<slug>` lists what it would delete and changes nothing unless given `--execute`.
It has not been run for real. With `--execute` it writes one `event_log` row
(`raw_bodies_purged`, with every deleted sha256) before deleting, so the removal
is on the record (rule 5). Afterwards the `raw_response` rows still say what was
fetched, when, with what status, and what it hashed to. Only the bytes are gone.
One catch: bodies are stored by hash alone, so if another source ever returned
the same bytes, both share one object. The script keeps any object another
source's rows also point at, and lists it. A database test in CI runs a Tron and
an Ethereum lookup, purges Etherscan, and checks all of this. Not yet confirmed:
whether our bucket credentials are allowed to delete. The dry run only reads.

**Phase 1 chains: eth, polygon, arbitrum, tron and btc.** BNB Smart Chain, OP
Mainnet and Base are outside Etherscan's free plan. Their `chain` rows stay;
nothing is deleted. A job for them still fails at once with a plain message.

**Every report for a 0x address states its chain coverage.** It lists the chains
that were checked, and each chain that was not, with the reason. The list and
the reason codes live in `taw/adapters/registry.py` (`PHASE1_EVM_CHECKED`,
`EVM_NOT_CHECKED`, `NOT_CHECKED_REASONS`), so the report template words them
from data. A test fails if an EVM chain is in neither list.

**Our use may need the Advanced plan, and that is open.** Etherscan's API Terms
define "Commercial Application" use as use in a website or service "offered to
third parties, whether free of charge or for a fee". They reserve that use for
the Advanced plan ($299 a month) and above. Free, Lite and Standard are for
"Personal & Research" use. A free public site for fraud victims reads as
commercial under that wording. This is our reading of the page, not legal
advice. Until the owner decides, Etherscan data has only been used in tests and
live checks that nobody else sees.

## 2026-11-19: EVM chains through Etherscan API V2

**What the free plan covers.** Probed on 2026-11-18 with our key, one
`proxy/eth_blockNumber` call per chain:

| Chain | chainid | Free plan |
|---|---|---|
| Ethereum | 1 | yes |
| Polygon | 137 | yes |
| Arbitrum One | 42161 | yes |
| BNB Smart Chain | 56 | no |
| OP Mainnet | 10 | no |
| Base | 8453 | no |

The three refusals come back as HTTP 200 with "Free API access is not supported
for this chain. Please upgrade your api plan for full chain coverage." This
matches docs.etherscan.io/supported-chains. A job for one of those chains fails
on its first call, with no retries: "Etherscan's free plan does not cover BNB
Smart Chain, so this lookup cannot run. Not retried. It needs a paid Etherscan
plan." Nothing is hard-coded, so these chains start working if the plan is
upgraded.

**Limits.** docs.etherscan.io/resources/rate-limits says the free plan allows 3
calls a second and 100,000 a day. We pace at 2.5 a second. A burst test got
"Max calls per sec rate limit reached (3/sec)" from the fourth call on, as HTTP
200. A typical small lookup takes about 18 calls, so the daily cap is roughly
5,000 lookups. `addresstokenbalance` (all token holdings) is an "API Pro"
endpoint and is not used.

**Attribution.** The API Terms (last updated 1 October 2026) say that on any
plan, wherever API data is displayed, we must show "Powered by Etherscan.io
APIs" with a link to https://etherscan.io where practical. It must not suggest
Etherscan endorses us. The `etherscan` source row (migration 0003) already holds
that wording. The report template must show it.

**Terms that need the owner's decision.** The same terms forbid passing API data
to third parties as a feed, API or proxy, forbid bulk-downloading or archiving
their datasets, and say data must be destroyed when the agreement ends. They
mention a retention period without giving one. That bears on the Phase 2 public
API and on rule 3, which keeps every raw response. `resale_permitted` stays
false. This is not settled; see Open below.

**Errors arrive as HTTP 200, so every body is read.** `{"status": "0"}` with a
text result is an error. With an empty list it means "nothing found" and is not
an error. A rate-limit body is retried with growing waits. These fail the job at
once: chain not covered, key rejected, daily cap used up, and an API Pro
endpoint (that one would be our bug). The daily-cap wording ("Max daily rate
limit reached") is from Etherscan's error list. We have not seen it with our own
key, so it is matched loosely on "daily" plus "limit".

**The key never reaches storage.** Etherscan takes the key only in the query
string. The adapter adds it to the outgoing request alone. The RawStore gets the
bare endpoint URL and the other parameters. `clean_request()` would drop an
`apikey` field anyway, and refuses to store a value from the environment. A test
checks the index and every saved body for the key.

**One block for everything.** A lookup reads at `head`, which is the latest block
minus a few confirmations: 12 on Ethereum, 64 on Polygon, 20 on Arbitrum. Lists
use `endblock=head`, logs `toBlock=head`, and `eth_call`/`eth_getCode` read state
at head. Etherscan's balance endpoint only answers "latest" (balance history is
paid), so the native balance comes from Multicall3's `getEthBalance` at head.
The snapshot and the transfers then agree on `as_of_block`.

**ERC-20 log index from the logs endpoint.** `tokentx` has no log index. Token
transfers are read from `getLogs` filtered on the Transfer topic, with our
address as topic1 (sent) and then as topic2 (received). `logIndex` is unique
within the block, so two identical transfers in one transaction stay two rows.
`tokentx` is used only for decimals and symbols, and to pick where the logs scan
starts when history is long. Logs with four topics are NFT transfers and are
ignored. A token whose Transfer event does not index from and to cannot be found
this way. Those rows are counted (`unmatched_token_rows`), not stored. A Tron-style
fallback index was rejected because it would collide with a real log index the
next time the same transaction is seen from another address.

**Internal transfers use the trace position.** Etherscan's `traceId` is a path
in the call tree ("0", "0_1", "0_1_1"). It is unique within the transaction, so
it is encoded into one integer that keeps trace order, as base-32 digits over
six places ("0_1_1" is 1·32⁵ + 2·32⁴ + 2·32³). Paths up to six deep with steps
below 31 fit the int column. If `traceId` is missing or does not fit, the
position within the transaction as returned is used. Those numbers are all
below 32⁵, so the two never collide. The position depends on which rows the API
returned for this address, which is why it is only a fallback.

**Token balances without the paid endpoint.** `balanceOf` at head for each
freezable stablecoin on the chain, plus the 20 tokens with the most transfers in
the address's newest 1,000 token transfers. Only non-zero balances are kept. The
stablecoin list was confirmed by reading each contract's verified ABI on
Etherscan for a blacklist, block or freeze function or event:

- Ethereum: USDT, USDC, PYUSD, USDP, TUSD, EURC, FDUSD, USD1, USDG
- Polygon: USDT0, USDC, USDC.e
- Arbitrum: USDT0, USDC, USDC.e, PYUSD

DAI, USDe and RLUSD showed none and are left out. XAUT and PAXG can be frozen but
are gold tokens, not stablecoins. The chains outside the free plan have empty
lists until someone checks them.

**Truncation keeps the first transfers.** Over 2,000 transfers, the batch holds
the newest 2,000 and `truncated` is true. The very first native, internal and
token transfer, and the first incoming one of each, are fetched separately and
appended, so first activity and first funder are right. A truncated batch can
therefore hold up to six transfers past the limit, with a gap before them.

**Paging past 10,000 rows.** Etherscan refuses page × offset above 10,000. After
ten pages of 1,000 the adapter moves the block edge to the last block it saw and
starts again from page 1. Rows from that block arrive twice and are dropped by
key. If a single block holds more than 10,000 rows for one address, the lookup
fails with a clear error rather than looping.

**Mixed-case addresses must pass EIP-55.** A mixed-case address is a checksum,
and checking it catches most typos. All-lower and all-upper addresses are
accepted as typed. Keccak-256 is implemented in pure Python
(`chains/evm_address.py`) rather than adding a package, and tested against
published vectors.

**Shared helpers.** The request pacer and ABI decoding moved from the Tron
adapter to `taw/adapters/chains/common.py`.

## 2026-11-18: settings move to the dashboard; one region

**Railway settings live in the dashboard, not the repo.** This supersedes the
`railway.json` entry below. Before the 2026-12-01 cutoff, the start command
(`sh start.sh`), restart policy (On Failure) and watch paths were set in each
service's dashboard settings and `railway.json` was removed. Infrastructure as
Code (`.railway/railway.ts`) was not adopted: it is applied by hand with
`railway config apply`, deletes resources it leaves out, does not document
watch paths yet, and needs a newer CLI than this machine has. A test fails if a
`railway.json` or `railway.toml` comes back, since it would override the
dashboard.

**Everything in US East.** Postgres was in Europe (`europe-west4`) while the web
service was in US East, so every web request crossed the Atlantic (`/healthz`
took 1 to 4 s). The database, its volume, the web service, the worker and the
bucket (`iad`) are now all in US East; `/healthz` takes about 0.25 s.

## 2026-11-18: watch paths, and the Config as Code cutoff

**Docs and Markdown do not deploy.** `railway.json` has `build.watchPatterns`
`["**", "!/docs/**", "!/*.md", "!/**/*.md"]`. Railway skips a deploy when no
changed file matches. Negations only work after an including rule, hence the
leading `**`. Both services read the same root `railway.json`.

**Deadline: 2026-12-01.** Railway has deprecated Config as Code. `railway.json`
keeps working for existing services until 2026-12-01, then stops being read.
It holds the start command (`sh start.sh`, which picks web or worker and runs
migrations) as well as the watch paths, so something must replace it before
then. The replacement, `.railway/railway.ts` (Infrastructure as Code), is not
read on deploy; it is applied with `railway config apply`, and per Railway's
docs a resource left out of it is deleted. Its reference does not yet document
watch paths. Not decided yet: see STATUS.md.

**Every completed lookup writes a `lookup_completed` event.** The payload holds
chain, address, snapshot id, as_of_block, `transfers_saved` (new rows),
`transfers_reused` (already stored) and `snapshot_reused`. A full lookup writes
it in the same transaction as its data. It holds nothing about who asked.

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
- Etherscan's API Terms limit redistribution, archiving and retention. Decided
  2026-11-19: display only, no public API, and a purge script exists. Still
  open: Phase 2 EVM source, and whether a public site needs the Advanced plan.
- BNB Smart Chain, OP Mainnet and Base need a paid Etherscan plan or a different
  provider. They are outside Phase 1.

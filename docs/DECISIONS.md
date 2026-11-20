# Decisions

Choices made while building, and why. Newest first. A later decision that
reverses an earlier one says so; nothing here is edited away.

## 2026-11-20 (night): Sign-in first, Cloudflare IPs, the GoPlus logo

**Sign-in before the form.** While the site is invite-only, a signed-out visitor
sees a short welcome and a Sign in button, not the form; a signed-in stranger
sees the invite-only notice. The start-before-sign-in path stays in the code
behind `LOOKUP_BEFORE_SIGN_IN=1`, which is off. With it off, the API refuses a
lookup that has only a session.

**The visitor's IP behind Cloudflare.** traceawallet.com resolves to Cloudflare
(nameservers eric and melinda.ns.cloudflare.com; responses carry both
`cf-ray` and `x-railway-edge`).
- **Which header.** The per-connection limit uses Cloudflare's
  `CF-Connecting-IP`, but only when the connection Railway saw (`X-Real-IP`,
  which its edge overwrites) comes from Cloudflare's published ranges, read
  2026-11-20.
- **Why.** A request sent straight to the Railway domain can't choose its own
  IP; it falls back to `X-Real-IP`.
- **Checking it live.** `GET /api/v1/connection` returns the first 16
  characters of the salted hash the limit counts, and never the IP.

**GoPlus logo.**
- **Where it comes from.** It's the horizontal logo from GoPlus's own media
  kit (gopluslabs.io/en/media-kit), downloaded 2026-11-20. The SVG's sha256 is
  `4cc4fa68…24e5`; it has no scripts or external references. It is served from
  our own origin as `/static/goplus-logo.svg`.
- **Terms.** The kit offers the logos for partner and press use and states no
  further terms. The API licence asks for the logo with "Powered by GoPlus"
  wherever results are shown.
- **Where it appears.** Beside GoPlus's result in every web report, and on the
  About page. The wordmark is white, so it sits on GoPlus's own dark brand
  colour (#151515). The text report keeps the written credit.

**Clerk keys already in Railway.** Both services hold development-instance
keys (`pk_test_`, `sk_test_`), from the application the owner created on
2026-11-16. Its Frontend API is `profound-prawn-7291.clerk.accounts.dev`. A
development instance works on our domain and shows a "development mode" badge.
A production instance, needed before the site opens beyond testers, takes
DNS records for a clerk subdomain.

## 2026-11-20 (night): The web interface

**The site is a client of the API.** The pages call `/api/v1` over the loopback
connection (`TAW_API_URL`) and never open the database. They pass the visitor's
Clerk session token as a Bearer token, our own session id as `X-TAW-Session`,
and the visitor's IP as `X-Real-IP`, which Railway's edge sets. The API reads
no cookies, so a form on another site cannot act through it. The `X-TAW-User`
test header and the `TAW_API_ENABLED` switch are gone. Until every setting the
site needs exists, the API answers 503 and the home page says "Coming soon", so
a deploy cannot open the site early.

**Sign-in (Clerk, docs read 2026-11-20).**
- **Where the token lives.** ClerkJS (clerk-js 6 with the ui 1 bundle, loaded
  from the instance's Frontend API) keeps a short-lived session token in the
  `__session` cookie.
- **How it's checked.** The API verifies it: RS256, `exp` and `nbf`, and
  `azp` must be our own origin. The key comes from `CLERK_JWT_KEY`, or from
  Clerk's JWKS fetched with the secret key.
- **Invite-only.** The email comes from a custom session claim,
  `primaryEmail`. Only addresses in `TESTER_EMAILS` may use the site; a token
  without the claim counts as not invited.
- **Before sign-in.** A lookup starts on submit, tied to a salted hash of our
  session cookie. Signing in claims every lookup from that session made in the
  last day. Results are shown only to a signed-in owner.

**Limits** come from the lookup table itself:

| Who | Limit |
|---|---|
| Before sign-in, per IP | 5 an hour, 20 a day |
| Signed in, per account | 10 an hour, 40 a day |
| Before sign-in, the whole site | 200 a day |

The site-wide cap is there because a lookup starts before sign-in: without it,
people who are not invited could spend the day's Blockscout allowance. When
less than 5% of today's allowance is left, the lookup still queues, and the
reply says it will run after the 00:00 UTC reset. IPs and session ids are
stored only as HMAC-SHA256 with `HASH_SALT`.

**Turnstile** (docs read 2026-11-20). The token goes to `siteverify` with the
secret and the visitor's IP. Tokens last 300 seconds, can be used once and are
at most 2048 characters. A failed check starts nothing.

**Protection.**
- **Cookies.** Our session cookie is HttpOnly, Secure and SameSite=Lax.
- **Forms.** Every form carries a CSRF token, an HMAC of the session id.
- **Content security policy.** It allows only our own origin, the Clerk
  Frontend API, `*.protect.clerk.com` and `challenges.cloudflare.com`, from
  Clerk's published CSP list. Styles allow `'unsafe-inline'` because Clerk
  styles itself at runtime. There are no inline scripts, `frame-ancestors` is
  `'none'`, and HSTS is on.
- **Search engines.** Lookup, report, account and sign-in pages send
  `X-Robots-Tag: noindex` and `no-store`.

**One template for text and HTML.** The web report is the text template
rendered with autoescape on. Addresses get a copy button and a "Look up" link;
hashes and contracts get a copy button; outside links get anchors. Each
heading becomes a section, and sections longer than eight lines start
collapsed. The buttons carry no text of their own, only an aria-label and a
CSS label. A test checks that every line of the text report appears in the
HTML for all five cases, and the text golden files did not change. Token names
stay escaped and defanged and never become links.

**Screenshots.** `scripts/screenshot_pages.py` serves the app on a seeded
throwaway database and photographs each page at 390 px with Playwright driving
the installed Chrome. A fixed tester stands in for Clerk, inside that process
only.

## 2026-11-20 (night): Report wording, third pass

- **Summary per asset.** One line per recognised asset that moved: received,
  sent, and what remains ("No USDC remains at this address."). Received minus
  sent need not equal what remains: fees, and destructions by an issuer, move
  balances too.
- **Lookalikes of the address itself.** The report's own address is now part
  of the lookalike comparison. A group that holds it says that its other
  addresses imitate this address's beginning and end, and the summary counts
  them. Case 3: `0xfa0ac3c1…33a9` against `0xfa0a50dc…33a9`.
- **Tokens named like ours.** An unrecognised token's name is reduced to what
  a reader sees: NFKC folding, Cyrillic, Greek, Armenian and similar letters
  mapped to their Latin lookalikes, invisible characters and punctuation
  dropped. If what's left reads as a recognised asset on that chain, the report
  says it is not that issuer's token and gives the real contract. The five
  tokens with the most transfers are shown, and the rest are counted. In case
  3, 13 of 14 such tokens read as USDC.
- **Chains not covered.** One sentence names them. The reason codes stay in
  the data.
- **Balance around the victim's transfer.** It is the sum of every earlier
  transfer of that asset, stated only when the whole history was read and
  that asset's balance reconciles. It is never stated for ETH or TRX, whose
  fees move the balance with no transfer record. Case 3: 0.041921 USDC before,
  242,209.45551 after. The next two transfers out, 230,000 and 12,209.45551,
  add up to exactly that.
- **Times** carry no "UTC" each; the report says once that all times are UTC.
  When the prior-lookup count is zero, the report says no one else has looked
  the address up here yet.

## 2026-11-20 (late evening): Report wording, first revision

The owner's review of the first S.D.N.Y. report changed the templates. Most
changes are wording; the data gained a summary, list-matching counts,
first-hop entries grouped by address, lookalike groups, every zero and very
small transfer, and a code for what each source was used for.

- **Summary first.** It covers what is held, freezes with their dates, the
  largest destination and its share of what left, list matches out of how many
  checked, and whether the history read is complete.
- **Freezes in plain words first.** The address can no longer send the token.
  A freeze returns nothing to anyone; that takes legal process. The issuer can
  release or destroy the funds. The contract detail goes on a second line.
- **Sources say what they were used for.** No claim wording ("frozen by
  Circle") and no internal notes. The required credits stay ("Powered by
  Etherscan.io APIs", "Powered by GoPlus", Chainlink's disclaimer).
- **Token names are untrusted text.** Each list of unrecognised names carries
  the warning not to contact anyone named in them. Dots and "://" are broken
  up ("bio[.]link") so no name can become a link.
- **List matching states its limits.** The report says how many first-hop
  addresses were checked, against which lists and of what date, and that no
  match does not mean an address isn't an exchange's.
- **Fingerprint.** The report's id and sha256 close the report. They come from
  the stored row, outside the hashed data.
- **The owner's matched transaction.** It shows in a private section of the
  owner's view only (`GET /api/v1/lookups/{id}/report`). The stored report and
  its hash never hold it.
- **Fixed sections.** Still drafts. Agency names and addresses were checked on
  the agencies' own pages on 2026-11-20:
  - ic3.gov (FBI). Its Recovery Asset Team brochure says to contact your
    financial institution "as soon as fraud is recognized".
  - PSA240624, on fake lawyers who ask for fees and "back taxes".
  - reportfraud.ftc.gov (FTC press release, 2020-10-22).
  - econsumer.gov, named on ftc.gov as the site for international scam
    reports.

  No phone number is included. The only one found, the FTC's, came from a
  2020 page.

## 2026-11-20 (evening): Reports, matching and the lookup API

**A report is one snapshot.** One chain, one address, one snapshot, and the
chain data comes only from the source that produced that snapshot. The
transfers are that source's rows in transactions touching the address, up to
the snapshot block and recorded by the time the snapshot was taken. Labels,
label checks and prices are read as they stood at a cut-off, `labels_as_of`,
which is part of the report. Building again from the same snapshot with the
same cut-off gives the same data and the same sha256, and the same build is
stored once (`report.snapshot_id`, migration 0017). The hash covers the
canonical JSON the 0001 schema comment names. The canonical form refuses
floats, dates and very large integers rather than guessing how to print them,
so amounts and times are strings.

**What is outside the hash.** The prior-lookup count changes after a report is
built, so it is read from `lookup_count` when the report is shown, bucketed
(none, 1, 2 to 5, 6 to 20, more than 20). It is never part of `report.data`.

**Wording lives in templates.** `report.data` holds facts, codes and numbers.
All prose is in `taw/report/templates/`. A test fails if assessment words
(scam, fraud, safe, clean, suspicious and the like) appear anywhere outside
the two fixed sections: the recovery-scam warning and the IC3 checklist. Both
are marked as drafts until the owner approves them.

**Set apart, neutrally.** Three kinds of transfer are listed on their own and
left out of the first-hop lists:
- zero-value transfers
- tiny ones: below 1 USDT, USDC or DAI, 1 TRX, 0.0005 ETH or 0.00001 BTC
- tokens we don't recognise

A token is identified by its contract. The fixtures show why: they hold tokens
named "USDT" and "USDC" from other contracts, and symbols with Cyrillic
letters and invisible characters, which the report prints as `<U+2063>` and
the like.

**Lookalike addresses (added without being asked).** The S.D.N.Y. address has
small funders such as `TWzkby…KBqchq` that begin and end like its largest
destination `TWzSi…KBqchq`. That is the address the complaint calls "TWzSi
Entity A". The report points out any first-hop address that shares its first
two characters after the prefix and its last four with another one in the same
report, and says to check every character. It makes no claim about why.

**Prices.** USD comes only from stored Chainlink rows. With no row the report
says "no price available" and leaves the figure out; it is never counted as
zero. The share held in freezable stablecoins is worked out only when every
holding has a price. Production has no price rows yet, so for now every
figure reads "no price available".

**Matching** (`taw/matching.py`). The result is stored on the lookup row only
(`lookup.match_result`). The audit trigger on `lookup` logs ids only, so the
inputs and the result never reach the event log, and `lookup_redact()` clears
them.

How it matches:
- **With a hash.** The transaction is read directly (`fetch_transaction`, new on
  every chain adapter), at any age. If it pays a different address the result
  says so and lists the addresses it paid.
- **Without a hash.** It tries sender, amount and date, then amount and date.
  The amount received may be up to 1% below the amount stated, plus rounding of
  the last digit given. The date allows a day either side, in UTC.
- **More than one fit.** All candidates are returned and none is picked.
- **Nothing found.** The result says whether that settles it. It doesn't when
  the history read was truncated before the date given.
- **After a match.** It lists the next outgoing transfers of the same asset,
  with the sentence that this does not show they were the victim's funds.
  Zero-value transfers are left out of that list, because others can create
  them.

The case 3 example: A.T.'s 242,209.53 USDC was one of 15 transfers in a
Coinbase batch transaction, and 242,209.413589 USDC arrived. The next USDC out
is the 230,000 to `0x5842b2…` that paragraph 22 describes.

**Jobs.** A lookup starts one `address_lookup` job per chain: three for a 0x
address (Ethereum, Arbitrum One, OP Mainnet). Jobs are still shared, and
`lookup_job` records which ones a lookup started. When the victim gave a hash
or an amount, one `lookup_match` job waits for those jobs to finish. Its
payload is the lookup id and nothing else. After an address lookup the worker
builds the report and recounts `lookup_count`; a failure there is a note, not
a failed lookup.

**The API** (`taw/api/`): `POST /api/v1/lookups`, `GET /api/v1/lookups/{id}`
and `GET /api/v1/reports/{public_id}`.
- **Off by default.** Every route is off unless `TAW_API_ENABLED=1`, and the
  setting stays off in production until sign-in, Turnstile and rate limits
  exist. With it off the routes answer 404. The OpenAPI page is turned off too.
- **Who is asking.** Until Clerk is wired in, the caller names their account in
  an `X-TAW-User` header. That is a stand-in for tests only.
- **Who can read what.** A lookup is readable by its owner only; anyone else
  gets the same 404 as for a lookup that doesn't exist. A report is readable by
  any signed-in caller holding its public id, because it holds nothing about
  who asked.

**Fixtures** (`tests/fixtures/reports/`, `scripts/record_report_fixtures.py`).
One address per case, recorded on 2026-11-20 with no Blockscout calls:
- Tron through TronGrid: 206 and 29 requests
- Bitcoin through mempool.space: 6 requests
- Ethereum through Etherscan, which is for tests only: 47 and 42 requests
- GoPlus: four calls from a laptop, outside the worker's ledger

The labels, blacklist events and list dates were exported from production
with a read-only script. The response bodies stay out of the repo; every
record keeps its sha256. The golden files are `report.json` and `report.txt`
per case.

## 2026-11-20 (afternoon): Burned funds: a global index of blacklist events

**The problem.** Tether can destroy a frozen address's USDT
(`destroyBlackFunds`). The balance drops to zero with no Transfer event. The
production smoke test (TC66d59…KNWAN on Tron) showed USDT balance 0 against
transfers summing to 465,514.638375. The reconciliation therefore said "history
may be incomplete" when the history was complete and the funds had been burned.

**The index.** Migration 0016 adds `blacklist_event` (append-only) and
`blacklist_scan`. It holds every freeze, release and destruction event of:

| Chain | Token | Events | Read through |
|---|---|---|---|
| Ethereum | USDT | AddedBlackList, RemovedBlackList, DestroyedBlackFunds | Blockscout `eth_getLogs` |
| Ethereum | USDC | Blacklisted, UnBlacklisted | Blockscout `eth_getLogs` |
| Tron | USDT | the same three as Ethereum USDT | TronGrid's events list |
| Tron | USDC | the same two as Ethereum USDC | TronGrid's events list |

USDC has no event for burning a frozen balance. Arbitrum and OP Mainnet are left
out because their freeze events index the address, so one address's events cost
one call at lookup time (as before).

**Backfill once, then daily.** `scripts/backfill_blacklist.py` does the first
scan for each chain. The `blacklist_refresh` job runs daily at 01:00 UTC, after
Blockscout's reset. It scans each backfilled contract from the end of its index
to the head (64 blocks short of the head on Ethereum). A contract never
backfilled is skipped and said to be so. It is never treated as having no
events.

**Cost.**

- Tron dry run, 2026-11-20 (to block 88,170,277): 149 TronGrid calls.
  - USDT: 9,321 AddedBlackList, 1,006 RemovedBlackList and 1,308
    DestroyedBlackFunds events. The earliest is 2020-06-26.
  - USDC: 1,206 Blacklisted and 640 UnBlacklisted events.
  - Every destruction in it has an earlier freeze. One release has no freeze
    before it: Tether released an address it had never frozen, which the
    contract allows.
- Ethereum (Etherscan's counts at block 26,454,430): 5,091 events.
  - Expected backfill: 15 to 20 Blockscout calls (300 to 400 credits).
  - Daily update: five calls.

**Checking Blockscout against Etherscan.** Blockscout's log search returned 6 of
a test wallet's 41 transfers once. So the Ethereum backfill also reads the same
range from Etherscan and compares the two, log by log (transaction hash and log
index). If they differ for a contract, that contract is not written: the index
is never filled in from Etherscan and never written short. Both providers'
replies are kept as a fixture, and a test replays them.

**Audit.** As for label loads (0015): a scan writes its events and one
`event_log` entry (action `scan`, entity `blacklist_scan`) in one transaction.
The entry holds the row count, counts by action and a sha256 over the rows
added. `scripts/verify_label_loads.py` recomputes the scans too.

**Freeze dates.** For Ethereum USDT and Tron USDT and USDC, the freeze date on a
frozen label now comes from the index: the last freeze event for the address
with no release after it, up to the snapshot block. The note names the
transaction and how far the index was complete.

**The reconciliation.** Before the checks, the worker reads the contract's
freeze status (the same reads the freeze step used to make, now reused) and
the address's destruction events from the index. Then, per stablecoin:

- A destruction at or before the snapshot block counts as an accounted
  movement out. It is listed in the token's result with its transaction hash,
  log index, block and amount.
- Balance = transfers in − out − destroyed: `reconciled`.
- Frozen, balance zero, and no destruction found:
  `frozen_zero_no_destruction`. This is its own result, not reconciled. It marks
  the history incomplete, and its reason says how far the index was complete.
- Anything else that doesn't add up: `not_reconciled`, as before.

**Deploys and new job kinds.** During the exchange-wallets deploy the old worker
claimed the new `exchange_refresh` job and failed it ("No handler"). That used
up the day's run. Now a worker defers a job kind it doesn't know for ten
minutes, without counting the attempt. `ensure_daily` also queues a day's job
again when an old worker failed it for that reason.

## 2026-11-20 (afternoon): Audit: one entry per label load

The owner decided this before the first OKX load (about 500,000 labels).
Writing a full copy of every row into `event_log` would have doubled the
load's size for no gain, since labels are append-only and can't be changed
after they are written.

**File loads** (OFAC's SDN list, `data/exchange_wallets.csv`, OKX's monthly
file) now do everything in one transaction:

1. Work out the rows to add, supersede or withdraw.
2. Write the `label_load` row, with the file's sha256 (0015).
3. Insert the labels with that `load_id`.
4. Compute a digest over the inserted rows.
5. Write one `event_log` entry: action `load`, entity `label_load`, payload
   with source, publication date, file sha256, row count, the rows' sha256 and
   the method.

A label with a `load_id` skips the per-row audit trigger. The trigger now
fires `WHEN (NEW.load_id IS NULL)`. A load that changes nothing still gets its
`label_load` row and its `event_log` entry, with 0 rows.

**The digest** (`labels.load_digest`) covers the load's rows in id order, one
line each:

`json_build_array(id, chain, address, source, category, label, claim_url,
source_date, observed_at in UTC, status, supersedes_id, raw_response_id,
claim_key)`

The digest is the sha256 of those lines, each ending in a newline. Every field
is in a fixed text form, so the digest can be recomputed from the tables alone.
`scripts/verify_label_loads.py` does that for every load, or one, and exits 1
on any mismatch. A test edits a loaded row with the immutability trigger
switched off, and the verifier catches it.

**Labels written one at a time** (freeze status, GoPlus) have no load id and
keep the full per-row copy. The 1,716 OFAC labels from the first production
load (2026-11-20, before this change) also keep theirs.

## 2026-11-20 (midday): Labels: exchange wallets

**Which lists** (owner's choice, 2026-11-20). Only lists an exchange published
itself:

- OKX's current monthly file.
- HTX's current list.
- The 2022 lists of Bybit, KuCoin, Bitfinex and Crypto.com.

Every label reads "listed by <exchange> as its wallet on <date>", the date
being the one the exchange gives. Bybit's file gives only a month, so its
labels say "on 2022-12 (month only; no day given)". HTX's file has no date; its
date is that of HTX's commit (2026-11-10). None of these pages states terms for
reuse, so resale stays false on both source rows (0014).

**The five smaller lists** are in `data/exchange_wallets.csv`, 83 addresses,
built by `scripts/build_exchange_wallets.py` from each exchange's own page or
file. An address is in only when the exchange's own words put it on a chain we
cover:

- HTX's coin column: TRC20/TRX, ERC20/ETH, BTC.
- Bybit's network suffix.
- Bitfinex's label: BTC, Omni, ETH/ERC20, Tron.
- Crypto.com's headings ("BTC:", "ETH, ERC20, and more:").
- KuCoin's table has no network column. An `0x` row counts as Ethereum only
  when its height equals KuCoin's ETH snapshot height (15,944,930); other `0x`
  rows are left out. Tron- and Bitcoin-format rows are unambiguous.

Every address passes its checksum. The exchange's own description of the row
("BTC cold wallet", "USDT-TRON") is quoted in the label.

**OKX** (`okx-por`). Each month the worker downloads OKX's file. November's is
`por_csv_2026110100_V1.zip`: 111 MB, one 219 MB CSV, 686,224 rows. The zip is
kept in the bucket as a raw response. Rows on ETH, ARBITRUM, OPTIMISM, TRON and
BTC are loaded, deposit addresses included. The label quotes OKX's Type column
as given ("Non Staking", "Native Staking"); nothing is inferred about who holds
an address. November gives 498,873 labels:

- Tron: 474,538
- Ethereum: 17,385
- Bitcoin: 6,727
- Arbitrum: 119
- OP Mainnet: 104

Of these, 13 addresses appear under two types and get one label each. 6,561
"Native ETH Staking" and "Eigenlayer Staking" rows hold 98-character validator
keys, not addresses; they are counted and skipped. 180,485 rows are on networks
we don't cover.

**Monthly refresh without rewriting.** The `exchange_refresh` job runs daily
at 06:00 UTC, and at once the first time:

- `data/exchange_wallets.csv` is reloaded when its sha256 changes.
- OKX's file for the current month is loaded once it exists. A month with no
  file yet is not an error.

OKX labels keep the date they were first listed. An address still in next
month's file keeps its row untouched. A new one gets a row dated with the new
month. One that is gone gets a `withdrawn` row. `label_load` records every
month, so "still listed in the 2027-02-01 file" is known without touching the
rows. Inserts go in chunks of 20,000.

**Applying.** `okx-por` joins OFAC and `exchange-por` as a local source, so
every funder and first-hop destination is matched against all three at no API
cost.

## 2026-11-20 (morning): Labels: applying them

**The looked-up address gets every source.** After each lookup the worker runs,
in order: freeze status, GoPlus, prices, labels. The last step reads every
current label on the address: OFAC, the exchange file, the stablecoin
contracts and GoPlus.

**Funders and first-hop destinations get only the local sources** (OFAC, the
exchange file). Those are answered from our own tables and cost no API calls.
GoPlus and the contracts are never asked about them. A GoPlus label that
exists for a counterparty, because someone looked that address up earlier, is
still not shown as part of this lookup.

- Account chains (EVM, Tron): a funder sent to the address, and a destination
  received from it. EVM matching ignores case; Tron's is exact.
- Bitcoin: a funder is another input of a transaction that paid the address. A
  destination is an output of a transaction that spent from it, worded
  "outputs of transactions that spent from this address". The address's own
  change is left out.

Nothing new is stored: labels are already append-only rows with dates, so a
report can rebuild what applied at the time of its lookup. The job log gives
the counts, e.g. "labels: 2 on the address; 1 of 140 first-hop address(es)
carry an OFAC or exchange label".

## 2026-11-20 (morning): Labels: GoPlus

**What is asked.** `GET /api/v1/address_security/{address}?chain_id=` for the
looked-up address only. The chain ids are 1, 42161, 10 and `tron`, all listed
in GoPlus's reference (updated 2026-08-20). Never Bitcoin. The reply is stored
unmodified as a raw response under the `goplus` source. Each field GoPlus sets
to "1" becomes one label that quotes the field and value exactly, with
GoPlus's own data source when it names one: `flagged by GoPlus:
phishing_activities = "1" (data source: SlowMist)`. `contract_address` is a
description, not a flag. `number_of_malicious_contracts_created` counts when
it isn't "0". A flag that disappears on a later lookup is withdrawn, not
deleted. Every question goes in `label_check`, "no flags" included. The
logo and "Powered by GoPlus" rule (2026-11-20 evening) applies wherever these
are shown.

**Access token, sent bare.** The worker uses `GOPLUS_APP_KEY` and
`GOPLUS_APP_SECRET` to get a token from `POST /api/v1/token`; it lasts two
hours and is reused. GoPlus's docs show `Authorization: Bearer <token>`, but
live on 2026-11-20 that form was refused (code 4012, "signature verification
failure") and the bare token worked. Without keys it calls with no token.

**Daily ledger, in calls.** `provider_budget` now has a `goplus` row beside
`blockscout`, with the same split: the worker may use all of it, scripts 30%.
GoPlus's free plan is 150 compute units a minute, 30,000 a day and 150,000 a
month (gopluslabs.io/security-api). It doesn't say how many units one address
call costs, and its replies carry no count. So the ledger counts calls against
`GOPLUS_DAILY_CALLS`, default 1,000, until GoPlus states the cost. The token
request counts as a call. A lookup uses one or two GoPlus calls. If the
allowance runs out, GoPlus is skipped for that lookup and the job log says so;
the lookup still completes.

**Live check (2026-11-20).** One call for an S.D.N.Y. address
(`TGuqb...kgdyf`, frozen by Tether per the complaint and per the contract)
returned no flags. "No flags" means GoPlus has nothing on it, not that it is
clean; the report must say so.

## 2026-11-20 (morning): Labels: stablecoin freeze status

**Function names, from each contract's published code** (2026-11-20). The EVM
sources are the verified implementations behind each proxy, read through
Etherscan, plus Blockscout's public explorer for OP Mainnet. The Tron sources
are the on-chain ABIs, read through TronGrid.

| Chain | Token | Status function | Freeze events | Date cheap? |
|---|---|---|---|---|
| Ethereum | USDT (TetherToken) | `isBlackListed(address)` | AddedBlackList / RemovedBlackList, address not indexed | No |
| Ethereum | USDC (FiatTokenV2_2) | `isBlacklisted(address)` | Blacklisted / UnBlacklisted, indexed | Yes |
| Arbitrum | USDT0 (ArbitrumExtensionV2) | `isBlocked(address)` | BlockPlaced / BlockReleased, indexed | Yes |
| Arbitrum | USDC (FiatTokenV2_2) | `isBlacklisted(address)` | indexed | Yes |
| OP Mainnet | USDC (FiatTokenV2_2) | `isBlacklisted(address)` | indexed | Yes |
| Tron | USDT (TetherToken) | `isBlackListed(address)` | AddedBlackList / RemovedBlackList | No |
| Tron | USDC (FiatTokenProxy) | `isBlacklisted(address)` | Blacklisted / UnBlacklisted | No (not built) |

**Status** is one call per contract for the looked-up address. On EVM chains it
is an `eth_call` at the snapshot block through Blockscout (20 credits each,
metered). TronGrid can't call at a past block, so on Tron it is read at the
current head just after the snapshot, and both are recorded. Every question and
answer goes in `label_check` (0013), "not frozen" included. A frozen address
also gets a label from `usdt-contract` or `usdc-contract`: "frozen by Tether:
the USDT contract on Ethereum (0xdac1...) reports isBlackListed = true for this
address". A call that gives no answer never withdraws an existing freeze label.

**Freeze dates, read only where cheap.**

- Built: USDC on the three EVM chains and USDT0 on Arbitrum. The events index
  the address, so one `eth_getLogs` up to the snapshot block, plus one block
  read for the time, finds the last freeze. That is two more calls, and only
  for a frozen address.
- Ethereum USDT: the events don't index the address. A date would need a
  binary search on `isBlackListed` at past blocks, about 25 calls (500
  credits). Not built.
- Tron USDT: TronGrid can't filter events by address, and the contract logged
  more than 6,000 freezes between July and November 2025 alone. Not built.
- Tron USDC: 1,206 freeze events in all (7 pages of 200), but TronGrid still
  can't filter them, so it means reading every page each time. Not built; it
  could be, with a local copy of the events.

**Ground-truth cases.** Five freeze statements are now checks:

- case 3: 74bf is blacklisted, which is the consequence of the burn
- case 5: the seven USDT groups
- S.D.N.Y.: the ten addresses
- D.D.C. 02967: KNWAN and 7McvZ

The Tron ones were confirmed live on 2026-11-20. A never-used Tron address and
the S.D.N.Y. activating address both read as not frozen, so the call does tell
the two apart. The Ethereum ones wait for Blockscout's credit reset.

## 2026-11-20 (small hours): Labels: OFAC SDN list

**One interface for every label source** (`taw/adapters/labels/base.py`, rule
7). There are two shapes:

- A bulk source loads a whole published list: OFAC, the exchange file. Its
  labels live in our tables, so applying them costs no API calls. These are
  the local sources.
- An address source is asked about one address at a time: GoPlus, the
  stablecoin contracts.

Every claim carries its source, date, wording and raw response.

**Append-only labels with a key** (0012, `taw/labels.py`). Each claim has a
`claim_key`, e.g. `sdn:<entry uid>:<tag>:<address>`. A reload compares each
key with the newest row for it:

- unchanged: nothing is written
- new: a row is added
- changed: a new row supersedes the old one
- gone: a `withdrawn` row supersedes the old one (a delisting)
- back: a new row supersedes the withdrawal (a relisting)

`label_load` records every load, including ones that changed nothing. A
label's `source_date` is the publication that made that row.

**The file.** `SDN.XML` comes from OFAC's Sanctions List Service (about 30 MB).
The service redirects to a signed link that expires within the hour, so the
file is stored under the stable OFAC URL; the signed one is never kept. A file
with a DTD is refused, so no entity is ever expanded.

**Chains come from the address format**, not the currency tag:

- 0x and 40 hex characters go to every EVM chain, matched without regard to
  case.
- A valid T... address goes to Tron.
- A valid Bitcoin address goes to Bitcoin.
- Anything else isn't mapped, and is counted by tag in `label_load.detail`.

On the 2026-11-19 list (1,082 digital currency IDs) this matters: one "XBT"
entry is a Tron address, seven "USDT" entries are Bitcoin addresses (Omni),
and 50 are formats we don't cover (Litecoin, Monero, Bitcoin Cash's own
format, Dash, Zcash, Solana, XRP and others). A Bitcoin Cash or Bitcoin SV
address in legacy format is a valid Bitcoin address too, so it is mapped to
Bitcoin; the tag in the label says what OFAC listed it as.

**Wording** (rule 9): "listed by OFAC on the SDN list: <name> (SDN entry <uid>;
programs ...); listed as Digital Currency Address - XBT". The link is OFAC's
search page for that entry.

**Daily.** The worker queues an `ofac_refresh` job once a day for 23:00 UTC.
The first one runs at once if no refresh has ever finished. `scripts/load_ofac.py`
runs the same load by hand.

## 2026-11-20 (small hours): Prices: Chainlink at bucket starts

This supersedes "read at the block before the transfer, cached per hour" in the
2026-11-20 (late) prices entry. Each price is now a time bucket's price, and
the bucket size depends on age: hourly for transfers in the last 48 hours,
daily before that. The owner asked for this.

**Where a bucket's price is read.** `latestRoundData()` at the last block at or
before the bucket's start, found with Blockscout's `getblocknobytime` (closest
before). The start, not the end, so the price never uses anything after the
bucket began, and a bucket in progress already has a fixed answer. A transfer
late in a daily bucket gets a price up to a day older than the transfer. The
round's `updatedAt`, age and block are stored beside it.

**Feeds.** These were read from the contracts on 2026-11-20 (`description()`,
`decimals()` = 8):

- ETH/USD, BTC/USD, DAI/USD, USDT/USD and USDC/USD on Ethereum
- TRX/USD on OP Mainnet

A stablecoin is priced from its own feed, never assumed to be $1. Only an
issuer's own token gets its feed's price:

- USDT0 on Arbitrum is Tether's, so it is priced as USDT.
- Bridged USDC.e gets no price.
- DAI on Ethereum, Arbitrum and OP Mainnet was confirmed on-chain (symbol
  DAI, 18 decimals).

**No price.** A read gives no price when:

- the call is empty or reverts (the feed didn't exist yet)
- the round is older than the feed's heartbeat plus a quarter (75 minutes for
  ETH, BTC and DAI; 30 hours for USDT and TRX; 28 h 45 m for USDC)
- the answer isn't positive

The read is still stored, with `usd` NULL and the reason, so it isn't paid for
again. Today's heartbeats are applied to old rounds too. Some early rounds
(DAI/USD updated once a day in 2020) will show as no price; that is the safe
side.

**Table.** `price` (0011) is append-only. It holds asset, bucket, status, USD,
feed chain and address, block, round, `updatedAt`, age, the limit used, the
`chainlink` source row, and two raw responses (the block lookup and the
feed read). The raw responses' own source is `blockscout`, which served them.

**Cost, all counted in the Blockscout ledger as the worker.** Each new bucket
costs one block lookup per feed chain plus one `eth_call` per asset. That's 20
credits a call, so 2 calls (40 credits) for one asset and one more call for
each further Ethereum asset in the same bucket. A stored bucket costs nothing
again, for any lookup. One lookup reads at most `PRICE_MAX_NEW_PER_LOOKUP`
(default 40) new buckets, newest first: at most 80 calls (1,600 credits, 1.6%
of the daily allowance). The rest wait for a later lookup. Running out of
allowance stops pricing; the lookup still completes.

## 2026-11-20 (late night): Blockscout daily budget

On 2026-11-20 our Blockscout key ran out of credits (HTTP 402, "Out of
credits") during the ground-truth case checks. Production EVM lookups could not
run until 00:00 UTC. Three changes make sure scripts can't do that to the
worker again.

**The finished-range search runs only when a lookup needs it.** It never ran on
a timer. Before this change, though, each worker process kept the result for
one hour in memory, so a busy day paid for a full binary search (about 13 to 25
calls) per chain per hour, and again after every restart. The range now lives
in `blockscout_internal_range` (0010), shared by every process and kept across
restarts. For 24 hours the lower edge is reused and only the top is extended to
the current head: one call when the newest blocks are finished. After 24 hours
the next search starts from the stored edges: one call to see whether the range
can now start at block 0, one more if it can't, then the top. A full search
runs only if the old range no longer holds. A lookup that finds unfinished
blocks inside the range drops it, so the next one searches afresh.

Idle cost per day: 0 calls before, 0 after. Nothing runs without a lookup. On a
day with lookups, the range now costs about 2 or 3 calls per chain per day,
plus one call per lookup to extend the top.

**A daily ledger in Postgres.** `provider_budget` counts calls and estimated
credits per UTC day for two consumers, `worker` and `scripts`.
`provider_status` keeps Blockscout's own `x-credits-remaining` count from the
last reply, which wins over our estimate. The worker may use the whole
allowance (BLOCKSCOUT_DAILY_CREDITS, default 100,000). Scripts (`try_evm`,
`cross_check_evm`, `check_cases`) may use 30%, and never the part of the
worker's 70% it hasn't used yet. A script stops with a message saying which
limit it hit. Scripts refuse to run Blockscout calls at all without the ledger,
so they now run with `railway ssh` on the worker rather than `railway run`.
Credits per call are estimates: 20 for an ordinary call, 50 for REST.

**Running out defers the job; it doesn't fail it.** When the budget or
Blockscout says the allowance is used up, the job goes back in the queue to run
at 00:05 UTC. Its `last_error` reads "Blockscout's daily allowance used up,
resets at 00:00 UTC", and the attempt is not counted, so the job keeps all its
retries.

## 2026-11-20 (late): prices, Chainlink terms, TronGrid limit

**Prices come from Chainlink feeds read on-chain.** Research is in
`docs/research/price-sources.md`.

- ETH, BTC, USDT, USDC and DAI are read from the Ethereum mainnet feeds.
- TRX is read from the OP Mainnet TRX/USD feed (from 2023-05-18).
- Each price is `latestRoundData()` read with `eth_call` at the block before
  the transfer. The value is kept with its feed, round, `updatedAt` and block,
  and shown with its age.
- Any other asset shows its amount with "no price available". So does any
  transfer earlier than a feed's first readable block.
- **DefiLlama is not used in Phase 1.** This supersedes the earlier plan to
  ask it for permission.

**Chainlink's redistribution clause needs a written answer before any Phase 2
paid API.** Its Terms of Service (Version 6.0, effective 2026-08-18) say "you
may not resell, lease, lend, share, distribute or otherwise permit any third
party to use the Website or the Services". Showing a derived USD value in a
free report reads as outside that clause. Serving feed values through a paid
API comes close. Ask Chainlink in writing before building one.

**TronGrid limit confirmed: 100,000 requests a day.** The owner confirmed our
account's limit. New free accounts since 2026-10-22 get 10,000 a day
(data-sources.md), so this needs checking again if the key is ever replaced.

## 2026-11-20 (night): Bitcoin through Esplora

**mempool.space is primary and Blockstream is the cross-check, behind one
client.** Both speak the Esplora REST API (`taw/adapters/chains/btc_esplora.py`,
docs at github.com/Blockstream/esplora/blob/master/API.md). Probed on
2026-11-20 with one tip-height call each: both answered height 976,665 with
HTTP 200 and no rate-limit headers. mempool.space caches for 1 second and
Blockstream for 10. Neither publishes a limit, and mempool.space's docs warn
of 429s and bans, so requests are paced at 2 a second. 429 and 5xx answers are
retried with growing waits, honouring Retry-After. Neither needs a key.

Their terms are silent (data-sources.md). Self-hosting either one is the clean
path for the public site and stays open.

**Addresses.** P2PKH (`1…`) and P2SH (`3…`) are checked with the base58
checksum. SegWit v0 (`bc1q…`) is checked as bech32 (BIP-173), and taproot and
later versions (`bc1p…`) as bech32m (BIP-350). Testnet and mixed-case bech32
are refused. bech32 is stored lower case.

**One height.** Everything is as of the tip height read at the start.
Unconfirmed transactions are ignored and counted. Confirmed transactions above
the height, which arrive if a block is mined during the lookup, are read, set
aside and counted. The provider's address totals and unspent outputs are
corrected by exactly those transactions, so every comparison is at one height.

**Rows follow migration 0003.** A `utxo_in` is an input, with `from` set; a
`utxo_out` is an output, with `to` set. `event_index` is the real input or
output position. For each transaction, every input and output of this address
is stored, plus the other parties' inputs and outputs, capped at 200 per
transaction. Capped transactions are listed in the check
`other_party_rows_capped`, which does not mark the history incomplete, since
our own rows are never capped. Coinbase inputs and outputs without an address
are skipped. Amounts are in satoshis, decimals 8.

**Truncation.** Up to 1,000 transactions, newest first. Past that the batch is
truncated, and paging continues to the end of the history to fetch the
earliest transaction, up to 400 pages (10,000 transactions). Esplora lists
only newest first, so there is no shortcut to the oldest. If the end isn't
reached, `earliest_transaction` fails and first activity is unknown. A
truncated history is never reconciled.

**Checks, under the same names as the other chains:**

- `native_reconciliation`: received minus spent from our rows must equal the
  provider's address totals and the sum of its unspent outputs.
- `tx_count`: the transactions we stored must equal the provider's count at
  the height.

Bitcoin has no tokens, so the token checks are left out. An adapter's own
check now replaces the generic one of the same name.

**Never GoPlus for Bitcoin.** `registry.goplus_allowed("btc")` is false, and a
test holds it there.

**Live cross-check, 2026-11-20.** Address
`bc1q4t93pz7k8xaqsckmh30we380xw8ggtq4xanvmp` at height 976,672, through
mempool.space and Blockstream:

- No differences: 14 transactions, 154 rows, balance 1,733,405 sat.
- All checks passed. The balance matched three ways (our rows, the provider's
  totals, the unspent outputs), and the count was 14 = 14.
- mempool.space's own address page showed the same balance (0.01733405), total
  received (0.05660986), sent (0.03927581) and 14 transactions, with first
  and last activity at the same seconds.
- Blockstream's page renders only with JavaScript and could not be read
  automatically. Its API answers matched mempool.space's exactly.

The wallet also shows why 0003 forbids "sent to": each of its six spends
returned change to itself.

## 2026-11-20 (evening): regenesis, spam gaps, GoPlus, NBCTF

**OP Mainnet's regenesis hides early transactions from every explorer.** On
2021-11-11 Optimism restarted its chain history at block 0. It carried balances
and nonces forward and dropped the earlier transactions. Explorers, Blockscout
and Optimistic Etherscan alike, list nothing before it.

The live Optimism wallet `0x5cb8…a33e` shows this. Its nonce was already 7 at
block 0 and is 12 now. Both explorers list exactly the 5 transactions it sent
since. Each later rise in the nonce matched a listed transaction: 7 → 10 in
2022, 11 in June 2026, 12 in November 2026.

The fix: the OP Mainnet chain settings record `history_start_block = 0`. The
snapshot reads the nonce there, and `tx_count` compares the transactions sent
since then with the provider's list. The earlier count is stored in the check's
details as `transactions_before_history`, with the note "unavailable from any
explorer". A report must say that this many transactions predate any record an
investigator can see.

**Blockscout's REST token list leaves out tokens it judges to be spam.** On the
same wallet it left out a 100 "ASAFE-N" airdrop that its Etherscan-style token
list and Optimistic Etherscan both show. Address-poisoning spam is common on
victims' wallets, and investigators need to see it.

The fix: when the two lists disagree, the transaction's receipt settles it.

- A transfer only in the token list is stored with the receipt's log index.
  The ASAFE-N transfer is now stored at log 67.
- A transfer only in the REST list is kept if the receipt confirms it.
- `token_lists_agree` fails, marking the snapshot incomplete, only for a gap
  the receipt cannot settle. Its details count `filled_from_receipts` and
  `unfilled`.
- There is no reputation filtering anywhere: spam stays visible, labelled only
  by what it is.

Cost: one receipt call per disagreeing transaction. After both fixes the
wallet's lookup reports `history_incomplete: false`, with every check
explained.

**GoPlus: never for Bitcoin; logo, wording and unmodified results.**

- Never query or display GoPlus for a Bitcoin address. It answers every
  Bitcoin address with all flags "0", which reads as clean but means "not
  covered".
- Wherever its results appear, show the GoPlus logo with "Powered by GoPlus"
  (licence s.5; failing to show it lets GoPlus end the licence).
- Show the results unmodified (s.5). A flag is still worded as GoPlus's claim
  (design rule 9): "flagged by GoPlus", naming GoPlus's own listed sources
  where it gives them.
- Migration 0009 sets the source row's attribution to match.

**NBCTF is out of Phase 1.** Its new site (matal.mod.gov.il) publishes no terms
or licence, only placeholder text under an "all rights reserved" footer. Its
robots.txt disallows crawling. Many of its listed "wallets" are 8 or 9-digit
Binance account numbers, not addresses. Using it would mean reusing a
government publication with no stated permission, scraped against the site's
wishes, and matching data that is often not an address at all. The source row
stays; migration 0009 adds this to its licence text. Revisit if NBCTF grants
permission in writing.

## 2026-11-20 (later): Blockscout fixed; every check stored separately

**Internal transfers: only the finished range (owner's choice, option 1).**
Blockscout answers status "2" for any range with blocks it has not finished
indexing. The adapter finds the finished range by binary search on that
status, about 12 calls for each end. It caches the range per chain for an
hour, reads internal transfers only inside it, and records the range on the
snapshot (`internal_from_block`, `internal_to_block`, migration 0008).

Nothing outside the range counts as checked. Native reconciliation does not
run unless the range is the whole history. A page that still answers status
"2" inside the range fails the lookup, and the lookup is retried.

**Tokens come from Blockscout's REST list.** It carries the real log index. On
the test wallet that is 32 for the first USDT transfer, the same as both
transaction receipts. The per-transaction list holds every transfer in the
transaction, so it is filtered to this address.

**Two-list check.** Blockscout's Etherscan-style token list is still read. It
must agree with the REST list, as a multiset of (tx, contract, from, to,
amount), over the blocks both cover. If not, `token_lists_agree` fails and the
snapshot is marked incomplete. The Etherscan adapter's equivalent check is
`token_list_matches_logs`.

**Every check is stored separately** in `address_snapshot.checks`, each with a
status of passed, failed or not_run, a reason and its numbers:

| Check | What it compares |
|---|---|
| `stablecoin_reconciliation` | Each freezable stablecoin's balance against in minus out. Marks incomplete if it fails. |
| `other_token_reconciliation` | The same for other tokens. Informational only. |
| `native_reconciliation` | The native balance against in minus out, minus fees, plus withdrawals. |
| `tx_count` | The nonce against the number of transactions sent. |
| `token_lists_agree` / `token_list_matches_logs` | The adapter's two token lists against each other. |

Per-token numbers stay in `token_reconciliation`. `history_incomplete` is true
when any check that marks incomplete has failed.

**Native coin, Ethereum and Arbitrum.** The rule is balance = native and
internal in − out − fees on every transaction sent (failed and zero-value ones
included) + validator withdrawals. Ethereum withdrawals come from Etherscan's
`txsBeaconWithdrawal` list, or from Blockscout's address summary
(`has_beacon_chain_withdrawals`), reading the slow list only when that flag is
set. It matched to the wei on the Ethereum test wallet and on an Arbitrum
wallet. Optimism and Tron record not_run, with the reason: Optimism's L1 data
fee is not read; Tron's staking and resource delegation are not parsed.

**Transaction count (nonce).** The address's nonce at the snapshot block must
equal the number of transactions it sent in the provider's list, failed ones
included. A mismatch on a history that is not truncated marks the snapshot
incomplete. Edge cases:

- **Contracts are skipped.** A contract's nonce counts the contracts it
  created, not transactions; under EIP-161 it even starts at 1.
- **EIP-7702 accounts are skipped.** Their code is `0xef0100` plus a 20-byte
  address. They are wallets, not contracts, so `is_contract` is false and the
  delegate is recorded. But every authorization they sign raises the nonce
  without a transaction from them.
- **Not caught:** a wallet that signed an EIP-7702 authorization and later
  cleared it has empty code again. Its nonce is then higher than its sent
  count, so the check would fail falsely. Spotting it means reading type-4
  transactions it did not send; not built.
- **Etherscan can't read old nonces.** Its free plan keeps no state older than
  about 128 blocks ("historical state ... is not available"), so an Etherscan
  nonce at an older block is not_run. Blockscout reads archive state.
- **Unverified:** transactions an L1 forces onto Arbitrum or Optimism (deposits
  and retryable tickets) may raise a nonce without appearing in the list. No
  test wallet had one.

**Live cross-check, 2026-11-20: no mismatches.**

| Address | Block | Transfers (both providers) | Native | Nonce |
|---|---|---|---|---|
| Ethereum `0xaab6…f637` | 26,452,607 | 49: 7 native, 1 internal, 41 token | Etherscan passed to the wei; Blockscout not_run (internal range starts at 23,397,898) | 22 = 22 |
| Arbitrum `0x9be8…e377` | 525,767,251 | 62: 10 native, 52 token | Etherscan passed to the wei (fees 0.0000206094 ETH); Blockscout not_run | 26 = 26 |

Both recordings are test fixtures; the cross-check is replayed in CI.

**Blockscout on Arbitrum checks almost no internal transfers.** Its finished
range came out as one block (524,324,867), because unfinished blocks are
scattered through its Arbitrum index, not confined to one end. The adapter
handles this honestly: one block is recorded and nothing else counts as
checked. In practice, though, internal transfers on Arbitrum are unchecked
until Blockscout's index improves or another source is added. A lookup also
spends about 25 calls finding the range when the hourly cache is cold. This
is open.

**Found and fixed on the way:**

- Etherscan writes zero as a bare `"0x"`, so a log at index 0 crashed the
  lookup.
- Blockscout returned HTTP 500 for every call for about two minutes during
  testing; 500 is now retried.
- Blockscout's per-transaction token list has no timestamp; the block's time
  is used.
- A log-index scare: Blockscout's first item for a transaction was another
  transfer in the same batch payout. Both receipts confirm log 32 is ours.

The `evm-blockscout` branch's merge condition is met: tokens come from the
REST list, internal transfers follow the finished-range rule, and the live
cross-check passes. The owner decides when to merge.

## 2026-11-20: reconciliation, for every chain

**Every lookup checks that its transfers explain its token balances.** For
each token, transfers in minus transfers out, up to the snapshot block, is
compared with the balance read from the contract at that block
(`taw/reconcile.py`). It covers the freezable stablecoins on every chain, plus
any other token whose balance was read. Each token gets one status, and both
numbers are kept:

| Status | Meaning |
|---|---|
| `reconciled` | The two numbers are equal. |
| `not_reconciled` | They differ. |
| `not_checkable` | The check can't be made. The reason is `history_truncated` or `balance_not_read`. A third reason, `internal_range_incomplete`, is reserved for the native coin. |

The results go on the snapshot (migration 0007): `token_reconciliation`,
`history_incomplete` and `incomplete_reasons`.

**An incomplete history is never presented as a finished answer.** A snapshot
is marked incomplete when either of these holds:

- a freezable stablecoin fails to reconcile on a history that is not
  truncated, or
- the adapter itself reported that its list may miss transfers.

The report must say the history may be incomplete. Rows from before 0007 have
NULL in the new columns. That means "not checked" and must not be read as
complete either. The `lookup_completed` event and the job message carry the
flag too.

**Why the adapter's own signal counts.** Balance arithmetic can't see a gap
that nets to zero. Blockscout's log search returned 6 of the test wallet's 41
token transfers. The wallet passes every token straight through, so in minus
out still equalled the balance, 0. What caught it was the EVM adapter's
comparison of the token list (41 rows) with the logs (6): 35 rows had no
matching log. That count now goes on the batch as an incomplete reason. A
replay test of that exact response proves the snapshot is flagged.

**Results on the test addresses.**

| Address | Token | Balance | In minus out | Status |
|---|---|---|---|---|
| Tron | USDT | 0 | 0 | reconciled |
| Ethereum (Etherscan fixture) | USDC | 0 | 0 | reconciled |
| Ethereum (Etherscan fixture) | USDT | 0 | 0 | reconciled |
| Ethereum (Blockscout, 6 of 41) | USDC | 0 | 0 | reconciled, but `history_incomplete`: "35 of 41 token-list rows have no matching Transfer log" |

**Two related bugs found and fixed.** Blockscout pads log `topics` to four with
null. The adapter read four topics as an NFT transfer and dropped every
Blockscout ERC-20 log, even the 6 it returned. Nulls are now ignored, and a
test covers it. The reconciliation alone would also have caught this on any
wallet with a non-zero stablecoin balance.

**Known blind spot.** A balance can change without a Transfer event: a
rebasing token, or an issuer burning frozen funds (Tether's
`destroyBlackFunds`). Such a token shows as `not_reconciled`. For a stablecoin
that marks the history incomplete, which is the safe direction.

**Native coin: checkable, not built.** Balance = in − out − fees was tested on
data we already hold:

| Chain | Result |
|---|---|
| Ethereum | 0.823658723973332 in + 0.25022341 internal in − 1.0729786868 out − 0.000474472886768108 fees (`gasUsed × gasPrice` on the 22 transactions it sent) = 0.000428974286563892. That is the balance read, to the wei. |
| Tron | 14.000013 in − 13.310013 out − 0.69 fees (`ret.fee` on the transactions it sent) = 0, the balance read. |

Extra calls per lookup to build it:

- **Ethereum and Arbitrum:** none. The gas fields are already in the
  transaction list we fetch, including failed and zero-value transactions,
  which pay fees too. Ethereum adds one call for validator withdrawals to the
  address, which are not transactions.
- **Optimism:** the L1 data fee is not in `gasUsed × gasPrice`. That needs one
  receipt per sent transaction, or Blockscout's REST transaction list, at
  about one call per 50 transactions. To be confirmed.
- **Tron:** none. But TRX staking (freeze, unfreeze, delegate) and voting
  rewards move the liquid balance and must be parsed.

It is `not_checkable` whenever the history is truncated or internal transfers
were not read in full.

## 2026-11-19 (later): Blockscout for EVM; permissions asked for

**Blockscout has been asked in writing; the answer gates Phase 2, not testers.**
The owner emailed info@blockscout.com on 2026-11-19. The email asks Blockscout
to confirm three things: a free public report site, a later paid API serving our
own reports (clause 7(h) of their terms), and keeping raw responses. A written
answer is recommended, but it does not block the launch to testers: nothing in
Blockscout's terms forbids a free public site. A written answer is required
before any Phase 2 public API is built on Blockscout data.

**The worker uses Blockscout for eth, arbitrum and optimism.**
`taw/adapters/chains/evm_blockscout.py` extends the Etherscan adapter, because
Blockscout speaks Etherscan's API. The differences:

- block, `eth_call` and `eth_getCode` go by JSON-RPC POST;
- logs are capped at 1,000 per query;
- internal rows use `transactionHash` and `index`.

The Etherscan adapter stays for tests and cross-checks only, and no job uses
it. Research and reasons are in `docs/research/evm-sources.md`.

**"Not yet processed" is never an answer.** If Blockscout answers status "2"
("Some internal transactions within this block range have not yet been
processed"), the lookup raises `BlockscoutIncomplete`. Nothing is saved and the
job retries later. A partial list is never stored as complete.

**Internal transfers carry each provider's own index.** Etherscan's
`event_index` is an encoded trace path, while Blockscout's is its own index of
the internal transaction within the transaction. Both are unique within the
transaction. The table's unique key includes the source, so the two never
collide. The cross-check matches internal transfers on transaction, from, to
and amount, not on the index.

**Phase 1 on free plans: eth, arbitrum and optimism from Blockscout, plus tron
and btc.** This supersedes the 2026-11-19 list of eth, polygon and arbitrum.
Blockscout's free tier stopped serving Polygon and Base on 2026-10-01. With our
key on 2026-11-19 they answered HTTP 402 "Featured chain 137 requires one of
the following plans: Builder, Business, Pro" (8453 the same), while eth,
arbitrum and optimism answered.

Chains not checked, with their reason codes:

| Chain | Reason code | Why |
|---|---|---|
| polygon | `needs_paid_blockscout_plan` | "It needs a paid Blockscout plan." |
| base | `needs_paid_blockscout_plan` | "It needs a paid Blockscout plan." |
| bsc | `no_source_with_suitable_terms` | Blockscout answers 404 "Network not supported", and no other provider's terms clearly allow our use. |

A job for any of the three fails at once with a plain message. They are never
routed to Etherscan or anywhere else, and their chain rows stay. A test checks
this with every key set.

**Blockscout's own index is incomplete in two places (found 2026-11-19, live).**
Details are in `docs/research/evm-sources.md`, Correction.

- **Logs.** `getLogs` by topic finds only the last few days on Ethereum, and
  still answers "OK". It found 6 of the test wallet's 41 token transfers. The
  REST list `/api/v2/addresses/{a}/token-transfers` found all 41, with log
  indexes matching Etherscan.
- **Internal transactions.** On Ethereum, anything before about block
  23,397,763 is "not yet processed". On Optimism, the newest day or so is.

As built, every eth and optimism lookup fails, and token transfers would have
come back incomplete. **The `evm-blockscout` branch is not to be merged until
both are fixed and the cross-check passes.** Two decisions are open:

1. How to treat unfinished internal ranges.
2. Whether to switch tokens to the REST list.

**Cross-check mode.** `scripts/cross_check_evm.py` runs Etherscan and Blockscout
at the same block (`at_block`). It reports every difference in transfers,
balances, first activity and truncation, and exits 1 if there is any.

**Other sources need permission before users see them.** Research is in
`docs/research/data-sources.md`, and each source row's licence and
`terms_checked_on` were updated by migration 0006.

| Source | Status |
|---|---|
| OFAC | Ready. |
| TronGrid | Ready for the free site. |
| GoPlus | Allowed with its logo and "Powered by GoPlus". |
| DefiLlama | Needs written permission: its terms bar republishing data. |
| NBCTF | Needs written permission: it publishes no terms. |
| Bitcoin (mempool.space, Blockstream) | Neither hosted API's terms settle it. The open choice is self-hosting or reading Blockstream's login-only API terms. |

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
- The lookup API records lookups and the worker updates `lookup_count`, but
  the API is off in production (`TAW_API_ENABLED`) until Clerk sign-in,
  Turnstile and per-account rate limits exist. `X-TAW-User` is a test stand-in.
- The recovery-scam warning and the IC3 checklist in every report are drafts
  awaiting the owner's approval.
- Etherscan's API Terms limit redistribution, archiving and retention. Decided
  2026-11-19: display only, no public API, and a purge script exists. Since
  then Etherscan is used for tests and cross-checks only.
- Blockscout's written reply to the 2026-11-19 email. Not needed for testers;
  required before a Phase 2 public API uses Blockscout data.
- Written permission from DefiLlama before its data is shown (NBCTF is out of
  Phase 1, 2026-11-20), and
  from GoPlus, TronGrid and Blockscout before any paid API.
- Bitcoin source for the public site: self-host esplora or mempool, or read
  Blockstream's Explorer API terms.
- BNB Smart Chain has no source with suitable terms; outside Phase 1.
- Internal transfers on Arbitrum: Blockscout's finished range is one block
  (2026-11-20). Another source, or wait for Blockscout's index.

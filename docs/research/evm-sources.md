# EVM data sources: which ones allow a free public site

## Correction, 2026-11-19 (live tests with our PRO API key)

The Blockscout entry below was written from docs, before we had a key. Live
calls changed three things.

1. **Free tier covers eth, arbitrum and optimism only.** Polygon and Base
   answer HTTP 402 "Featured chain 137 requires one of the following plans:
   Builder, Business, Pro" (8453 the same). The dashboard says these became
   paid on 2026-10-01. BNB Smart Chain answers 404 "Network not supported". So
   the Blockscout row's "5: no BNB" is 3 on the free tier.
2. **Its log search is silently incomplete on Ethereum.** `getLogs` filtered on
   the Transfer topic and our test address returned 3 logs each way, all from
   the last few days. Etherscan returned 20 sent and 21 received. A range around
   the wallet's first transfer (block 25,889,000 to 25,890,000) answered "No logs
   found". Every answer was status "1", "OK". The REST list
   `/api/v2/addresses/{address}/token-transfers?type=ERC-20` returned all 41,
   each with `log_index` (log 543 for the newest, as on Etherscan). Use that,
   not `getLogs`.
3. **Its internal-transaction index has unfinished ranges.**
   - Ethereum: any range reaching back before about block 23,397,763 answers
     status "2", "not yet processed". From there to the tip it is complete, and
     it returned the same 0.25022341 ETH internal transfer Etherscan shows.
   - Optimism: the last 50,000 blocks or so (about a day) answered status "2".
     Older windows were complete.
   - Arbitrum: every window sampled was complete.

What this does to the recommendation: Blockscout's terms are still the best
fit. But the "high, about 80%" reuse estimate assumed its Etherscan-compatible
endpoints behave like Etherscan's, and two of them do not. Token transfers
need its REST list. Internal transfers need a rule for unfinished ranges, which
is an open decision (DECISIONS.md, 2026-11-19, latest entry).

Researched 2026-11-19. Why: Etherscan's API Terms treat a website "offered to
third parties, whether free of charge or for a fee" as Commercial Application use
(Advanced plan, $299 a month). They also ban passing data to third parties as an
API or feed, ban archiving, and require deleting data when the agreement ends.
See DECISIONS.md, 2026-11-19.

Every terms quote below was read on the provider's own page. The clauses the
recommendation rests on were checked a second time against the raw page text:
Blockscout's terms and software licence, and the grant clauses of Alchemy, Ankr,
Chainstack and QuickNode. Routescan blocks scripted downloads; its quotes were
read through a page reader and matched on a second site (snowtrace.io) that
carries the same terms. Moralis blocks scripted downloads too, so its terms were
read from a Wayback Machine copy dated 2026-10-12 that carries the same version
label as the live page. None of this is legal advice.

## Summary

| Provider | Free public site? | Paid API later? | Storing / deletion | Free plan | Cheapest paid | Our 6 chains | Fits the adapter? | Reuse of `evm_etherscan.py` |
|---|---|---|---|---|---|---|---|---|
| **Blockscout PRO API** | Not prohibited; licence is "for their intended purposes", no internal-use limit | Terms silent; one clause (7h) on "duplicating its essential elements" is a risk | No limits, no deletion duty | 5 req/s, 100K credits/day (about 5,000 calls) | Builder $49/mo, 100M credits | 5: no BNB | Yes, all five needs, all on free tier | High, about 80% |
| Ankr | Ambiguous: "solely for Customer's own internal business purposes", grant written for node hosting | Needs "prior written consent" | No limits, no deletion duty | 200M credits/mo, archive, address-history API | Pay as you go from $10 | 6, but internal transfers only on eth and bsc | Mostly; no log index in its token-transfer API, so logs via `eth_getLogs` | About 40% (JSON-RPC parts) |
| Alchemy | Ambiguous: "solely for your internal business purposes", though it defines "End Users" of your apps | Conflicts with "allow any Third Party to use the Services", unless agreed in writing | No limits; deletion clause covers "Confidential Information", unlikely to include chain data | 30M CU/mo, but `eth_getLogs` capped at 10 blocks | Pay as you go, $0.52 per 1M CU | 6 for RPC; internal transfers only eth, polygon, base | Partly; internal transfers missing on arbitrum and optimism | About 40% |
| Moralis | Ambiguous: bans providing the Services "for use by a third party ... as a service or otherwise" | High risk under the same clause | No limits; deletion clause is about Moralis deleting our data | None (trial on request) | Starter $149/mo, needs attribution | 6 | Yes (ERC-20 transfers carry `log_index`) | About 40% |
| GoldRush (Covalent) | Ambiguous: bans "display, perform, publish, or distribute the Service" | Needs express permission | Silent | 14-day trial only | $10/mo, but "Do not use this plan when: Running a commercial business application" | 6 (no historical balances on arbitrum, no RPC on optimism) | Partly; ERC-20 by address needs a known token | About 30% |
| QuickNode | Ambiguous: bans use "for the benefit of third party" | High risk ("resell", "distribute") | Silent | 30-day trial, then billed | Build $49/mo | 6 for RPC; address history (Blockbook) Ethereum only, from $100/mo | Weak without Blockbook | About 40% |
| Chainstack | Ambiguous: "internal business purposes" | Needs a separate resale subscription | Silent | 3M requests, no archive | Growth $49/mo | 6 for RPC | No address index at all | About 35% |
| Routescan | Ambiguous: "solely for your internal business purposes", yet requires "Powered by Routescan.io APIs" for non-private use | Needs "express written permission" | Bans caching "for commercial purposes beyond what is permitted under your Plan" | 5 req/s, 100K/day | Standard $200/mo | 1: Etherscan-style API is Ethereum only | Ethereum only; no state at a past block | Near 100%, but moot |
| Dune Sim API | n/a | n/a | n/a | n/a | n/a | n/a | Shut down 2026-08-01 | none |

"Ambiguous" means the page neither clearly allows nor clearly forbids our use.

## Recommendation

Move eth, polygon and arbitrum to the Blockscout PRO API. Before building on it,
get written confirmation from Blockscout on two points: that a free public report
site is fine, and that a paid API serving our own reports would not breach
clause 7(h). It is the only candidate whose terms contain no internal-use limit,
no redistribution ban, no caching ban and no deletion duty. It also speaks
Etherscan's API, so most of the adapter carries over. The free tier is about
5,000 calls a day (roughly 270 lookups at 18 calls each); Builder at $49 a month
is about 5 million calls.

It also adds optimism and base, which Etherscan's free plan refused. That needs a
small DECISIONS.md change if you want them back in Phase 1.

BNB Smart Chain has no clean option. Every provider that covers it (Ankr,
Alchemy, Moralis, GoldRush, QuickNode, Chainstack) has ambiguous terms. Keep it
out of Phase 1, as now, and ask Ankr in writing if it becomes important: Ankr's
terms invite "prior written consent", and its free plan is the most generous.

Do not self-host Blockscout to avoid the hosted terms. Its software licence
changed on 2026-04-22: a paid service that "incorporates, uses, depends on, or
is materially enabled by the Software" needs a Commercial Licence. Versions up
to backend v10.2.0 stay under GPL v3.0, but pinning old code to dodge a licence
is not a plan.

## Provider notes

### Blockscout PRO API

Terms: https://eaas.blockscout.com/terms-and-conditions, "Last Updated: December
6, 2024", Version 1.1. They cover "Products", which include the "Application
Programming Interface (API)". No separate API terms were found.

1. Public site and paid API. The licence is "a limited, temporary,
   non-transferable, non-exclusive, revocable, non-sublicensable licence (right)
   to access and use the Products for their intended purposes". There is no
   internal-use limit and nothing on resale, redistribution or attribution. The
   one risky clause is 7(h): "copying, reproducing, or cloning any Product as a
   whole, or duplicating its essential elements without our prior written
   consent". A paid API that mirrors their endpoints could fall under it. A
   paid API serving our own reports probably does not, but that is a reading,
   so ask.
2. Storage. Nothing on caching, storing, archiving, retention or deletion.
3. Plans (https://docs.blockscout.com/rate-limits): Free 5 req/s and 100K
   credits a day; Builder $49/mo, 15 req/s, 100M credits; Pro $199/mo. A call is
   20 credits by default; the docs say the free tier is "roughly 5,000 standard
   requests a day". All chains on every tier. A key is required on
   api.blockscout.com.
4. Chains: eth, polygon, arbitrum, optimism (explorer.optimism.io), base. Not BNB
   Smart Chain. Blockscout's own migration table marks Base with a cross while
   using chain 8453 as its example; test it with a key.
5. Data: `txlist`, `txlistinternal`, `tokentx` and `getLogs` work. `getLogs`
   filters by topic without a contract address and returns `logIndex`.
   Historical native balance is `account/eth_get_balance&block=N`; `eth_call` and
   `eth_getCode` go through JSON-RPC POST to `/{chain_id}/json-rpc`.
6. Differences from Etherscan that our adapter must handle:
   - No `module=proxy`; it answers "Unknown module". A small JSON-RPC client is
     needed for the block, `eth_call` and `eth_getCode` calls.
   - `account/balance` ignores `tag` and returns the latest balance. Our adapter
     reads native balance through Multicall3 via `eth_call`, which still works.
   - `txlistinternal` on the live eth instance returns `index` and
     `transactionHash`, where the spec says `traceId` and `hash`. It can also
     return `status: "2"`, "Some internal transactions within this block range
     have not yet been processed". That must count as incomplete, not as an
     answer.
   - `getLogs` returns at most 1,000 logs, needs `fromBlock` and `toBlock`, and
     documents no page/offset, so paging is by block range.
   - The parameter is `chain_id`, not `chainid`.

### Ankr

Terms: https://www.ankr.com/terms/, effective July 15, 2026. §4 grants use "to
develop, launch and manage Nodes solely for Customer's own internal business
purposes". The "Service" is defined as node hosting, and the RPC and Advanced
API are not mentioned. §5.2 bars "third party access to, any element of the
Service" and "other commercial exploitation" without "Ankr's prior written
consent". The terms say nothing on storage or deletion. Plans
(https://www.ankr.com/docs/rpc-service/service-plans/): Freemium gives 200M
credits a month with archive data and the Advanced API, a 3,000-block
`eth_getLogs` range, and no trace methods. Premium is pay as you go from $10.
Chains: all six, plus Tron and Bitcoin. `ankr_getTransactionsByAddress` gives
native transfers by address. `ankr_getTokenTransfers` has no log index, so token
transfers would still come from `eth_getLogs`. Internal transfers need
`trace_filter` (Premium), documented for eth and bsc only.

### Alchemy

Terms: https://legal.alchemy.com/, Version 3.0, effective November 12, 2026. §2.1
grants use "solely for your internal business purposes". §2.2(v) forbids "allow
any Third Party to use the Services for any purpose other than for your benefit
... unless otherwise agreed in writing by Alchemy". Against that, §1.5 defines an
"End User" as someone who "utilizes or accesses applications you provide or
create using the Services". The terms have no caching ban; the termination
clause (§11.6) covers "Confidential Information", which should exclude public
chain data. Free plan: 30M CU a month, but `eth_getLogs` is limited to 10
blocks per call, which rules out address-wide log scans. Pay as you go is $0.52
per 1M CU. `alchemy_getAssetTransfers` gives native and ERC-20 transfers by
address. Internal transfers are only on Ethereum, Polygon and Base, and the trace
API is paid and documented for Ethereum only.

### Moralis

Terms: https://moralis.com/terms/, Version 1.4, August 2026. §1.1: "The Client
may not transfer, sub-licence or in any other way provide, permit or utilise the
Services for use by a third party, e.g. through time-sharing, as a service or
otherwise." §2.3 and §2.6 assume the client builds applications with users of
their own. Nothing on storage; the termination clause (§13.6) lets Moralis delete
our data, not the reverse. There is no self-serve free plan; Starter is
$149/mo, and the pricing page says "Starter and Pro require attribution". It has
the best data fit of the indexers: ERC-20 transfers by address with `log_index`,
internal transactions on all six chains, balances `to_block`.

### GoldRush (Covalent)

Terms: https://goldrush.dev/terms-of-use, last updated September 1, 2024. §4.3
bars "display, perform, publish, or distribute the Service", "Use the Service to
provide information or data to a competitor of Covalent", and "Sell, rent, lease
or sublicense the Service or access thereto, unless expressly permitted". Silent
on storage. Only a 14-day trial is free. The $10 plan says "Do not use this plan
when: Running a commercial business application"; Professional is $250/mo.
ERC-20 transfers by address need a known token contract; historical balances are
missing on Arbitrum, and JSON-RPC is missing on OP Mainnet.

### QuickNode

Terms: https://www.quicknode.com/terms, last updated December 1, 2025. §7(v): "not
sublicense, lease, sell, resell, rent, loan, distribute, transfer or otherwise
allow the use of the Quicknode Products ... for the benefit of third party".
Silent on storage. The free plan is a trial: "all free trials (including the
Free Plan indicated at quicknode.com/pricing), are limited to a thirty (30) day
term". Build is $49/mo. Address history needs Blockbook, Ethereum only, from
$100 a month per chain.

### Chainstack

Terms: https://chainstack.com/tos, June 2026. §3.1 allows use "for Customer's
internal business purposes", and §2.5 offers a separate subscription for "access
that would result in resale of Chainstack's Service". Chainstack may ask for a
"Powered by Chainstack" logo. The free plan has no archive data, and there is no
address index for EVM chains, so we would have to build one ourselves.

### Routescan

Terms: https://routescan.io/terms-and-conditions, last updated June 23, 2025.
§3.1: "This license is granted solely for your internal business purposes." Yet
§5.1 requires "Powered by Routescan.io APIs" when data is "not used for private
purposes". §3.4 bars reselling access and caching "for commercial purposes beyond
what is permitted under your Plan". Its Etherscan-compatible API is nearly a
drop-in replacement, but a live test returned "chain not supported" for polygon,
arbitrum, bsc, optimism and base. It also refuses historical `tag` values on
`eth_call` and `eth_getCode`.

### Dune Sim API

Shut down on August 1, 2026 (https://docs.codex.io/migrations/dune-sim: "Dune
retired its Sim API on August 1, 2026").

## Reuse of `evm_etherscan.py` with Blockscout

Carries over unchanged: block pinning, paging with the 10,000-row window,
native-transfer and ERC-20 log parsing, the Transfer-topic log scan, trace-index
encoding (fed from `index` instead of `traceId`), truncation and first
transfers, token metadata, the stablecoin lists, key handling and the raw-store
rules.

Needs new code:
- a JSON-RPC POST path for `eth_blockNumber`, `eth_getBlockByNumber`, `eth_call`
  and `eth_getCode`;
- block-range paging for `getLogs` under its 1,000-log cap;
- field mapping for `txlistinternal`;
- treating `status: "2"` as "not finished" (retry later, never store as
  complete);
- Blockscout's own error and rate-limit messages;
- a `blockscout` source row with its terms, through a migration.

The simplest shape is one adapter with a small per-provider dialect, so
Etherscan stays available for comparison runs.

## Before deciding

1. Email Blockscout and ask in writing about the free public site, a later paid
   API serving our reports, and storing raw responses.
2. Get a free PRO API key and re-run `scripts/try_evm.py` against it for the
   same test address, to confirm the field names, Base support and credit cost
   per lookup.
3. Only then build the dialect.

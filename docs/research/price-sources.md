# USD prices: Chainlink feeds on-chain, or DefiLlama with permission

Researched 2026-11-20. Question: can Chainlink price feeds, read at a historical
block, give USD prices for ETH, BTC, TRX, USDT, USDC and DAI at the time of a
transfer, instead of DefiLlama, whose terms bar "republish the data in any form
without permission" (data-sources.md)?

How it was checked: a researcher read Chainlink's docs, its feed directory (the
JSON the docs pages are built from) and its Terms of Service. They then read
feeds live, with `eth_call` against public archive nodes. I re-checked the
licence and distribution clauses against the saved terms page, and the
Ethereum feed addresses, thresholds and heartbeats against Chainlink's
directory. None of this is legal advice.

## Short answer

Chainlink can price ETH, BTC, USDT, USDC and DAI from about August 2020 (USDT
and USDC from February 2021). It takes one `eth_call` to an Ethereum mainnet
feed at the block before the transfer, which our Blockscout key can already
make. TRX is the weak spot: there is no Ethereum or Arbitrum feed. OP Mainnet
has one from May 2023, BNB Chain from March 2021, and nothing exists before
then.

Chainlink's terms have nothing written for reading on-chain feed data. One
general clause on distributing or reselling "the Services" is ambiguous for a
later paid API. DefiLlama's terms are explicit and stricter.

**Recommendation:** use Chainlink for the five majors, cached per hour. Add OP
Mainnet's TRX/USD feed from May 2023. Email DefiLlama for written permission to
cover the long tail and older dates. Until permission arrives, a report says
"no USD value available" rather than guess.

## Coverage

These are the feeds to use: the plain `<asset>-usd` feed in Chainlink's
directory, not its `-svr` or backup copies. Every one is risk category "low",
with 8 decimals.

| Feed | Network | Proxy | Deviation / heartbeat | Readable from |
|---|---|---|---|---|
| ETH/USD | Ethereum | `0x5f4eC3Df9cbd43714FE2740f5E3616155c5b8419` | 0.5% / 1 h | about 2020-08-09 |
| BTC/USD | Ethereum | `0xF4030086522a5bEEa4988F8cA5B36dbC97BeE88c` | 0.5% / 1 h | about 2020-08-09 |
| DAI/USD | Ethereum | `0xAed0c38402a5d19df6E4c03F4E2DceD6e29c1ee9` | 0.25% / 1 h | about 2020-08-09 |
| USDT/USD | Ethereum | `0x3E7d1eAB13ad0104d2750B8863b489D65364e32D` | 0.25% / 24 h | 2021-02/03 |
| USDC/USD | Ethereum | `0x8fFfFfd4AfB6115b954Bd326cbe7B4BA576818f6` | 0.25% / 23 h | 2021-02 |
| TRX/USD | Ethereum, Arbitrum | none | | |
| TRX/USD | OP Mainnet | `0x0E09921cf7801A5aD47B892C8727593275625a9f` | 0.5% / 24 h | 2023-05-18 |
| TRX/USD | BNB Chain | `0xF4C5e535756D11994fCBB12Ba8adD0192D9b88be` | 0.2% / 10 min | 2021-03 (needs a BNB node) |
| TRX/USD | Tron | (Tron contract) | 0.5% / 24 h | 2025-02-26 |

Arbitrum and OP Mainnet have their own ETH, BTC and stablecoin feeds too, from
2021 and 2022. They aren't needed: ETH on an L2 is the same asset as on
Ethereum, so mainnet feeds price both. That avoids L2 sequencer outages and the
missing old L2 state that public nodes showed.

## How to read a price at a block

Call `latestRoundData()` on the proxy with `eth_call` at block B. That returns
the price, round and `updatedAt` exactly as the chain saw them at B, and proxy
upgrades don't matter. It worked live for 2020 ETH and BTC prices. The other
route, walking old rounds with `getRoundData`, now reverts for the early phases
of those two feeds, so it cannot reach 2020 at all.

Before a feed existed, the call returns nothing or reverts; both mean "no
price". A Bitcoin or Tron transfer needs its time mapped to an Ethereum block:
after the Merge (12-second blocks) an estimate plus one or two header reads is
enough, and before it, a short binary search.

## Calls per lookup

One `eth_call` per asset per hour bucket, and per day for USDT, USDC and DAI,
whose feeds update about daily. A report with N transfers over H distinct hours
in A assets needs at most min(N, A × H) calls. A month of three assets is at
most a few hundred, and usually far fewer, because fraud flows cluster in time.
A price for a given feed and hour never changes, so it can be cached for every
report, not per lookup. Each response is stored like any other raw response
(rule 3), with the block, round and `updatedAt`.

## Accuracy limits a report must state

- **Age.** The value is the last round at or before the block, not a trade. It
  can be up to one heartbeat old (1 hour for ETH, BTC and DAI; about a day for
  USDT and USDC) and off from the market by up to the deviation threshold.
  Show `updatedAt` and its age beside every value.
- **Parameters change.** Today's thresholds and heartbeats don't describe 2020
  readings: DAI/USD updated once a day then. State the age, not today's
  settings.
- **Capped stablecoin feeds.** USDT/USD and USDC/USD are capped at a maximum:
  Chainlink says that above the cap "the price will not be reported on-chain".
  So an upward depeg is hidden. DAI/USD is priced from DEX state.
- **Suggested wording:** "USD value estimated from the Chainlink ETH/USD feed
  on Ethereum, round R, last updated T (age X) as of block B. Approximate; not
  an exchange price. Not affiliated with or endorsed by Chainlink."

## Terms

Chainlink Foundation Terms of Service, Version 6.0, effective 18 August 2026
(https://chain.link/terms). "Services" includes data "provided by decentralized
networks of node operators that are providing such services via data feeds", so
feed data is covered.

- **Licence:** "a limited, revocable, non-transferable license to access and
  use those portions of the Website and the Services that are proprietary to
  the Foundation in accordance with their intended uses and using their
  designated public interfaces." Reading the proxy contract is its designated
  interface.
- **The ambiguous clause:** "you may not resell, lease, lend, share, distribute
  or otherwise permit any third party to use the Website or the Services". A
  USD value derived from a feed and shown in a free report is a weak reading
  of "distribute the Services". Raw feed values sold through a paid API come
  closer.
- **Not advice:** the Services "do not constitute advice or a recommendation"
  and are provided "as is".
- **No attribution is required.** Naming the Foundation must not suggest
  endorsement.

Before any paid API, send one email to the address in Chainlink's docs
(chainlink_data_feeds@smartcontract.com) to settle the distribution clause.

## Compared with DefiLlama

| | Chainlink on-chain | DefiLlama, with permission |
|---|---|---|
| Coverage | ETH, BTC, USDT, USDC, DAI from 2020/21; TRX from 2021 (BNB) or 2023 (OP); no long tail | Long-tail tokens by contract and chain, including Tron; gaps seen on some L2 stablecoins |
| Effort | `eth_call` plus mapping times to blocks | One HTTP API |
| Checkable | Anyone can re-read round R at block B | Aggregated; the method can't be reproduced |
| Terms | No display ban; one ambiguous distribution clause | "republish the data in any form without permission"; damages up to USD 100,000 per violation |

A note on the DOJ test cases in `tests/cases/`: any transfer before August
2020, or TRX before March 2021, gets no Chainlink price at all.

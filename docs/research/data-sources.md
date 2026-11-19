# Non-EVM data sources: may we show them to users?

Researched 2026-11-19, before any of these sources is shown to users. The EVM
sources are in [evm-sources.md](evm-sources.md). Same questions: does the source
allow a free public site, and a paid API later; does it limit storing; what are
the limits and price; what attribution does it want.

How it was checked: four researchers read each provider's own terms, pricing and
docs pages. I then re-read the clauses each verdict rests on against the page
text: GoPlus's licence (§3, §5, §6), DefiLlama's Terms of Use (cl.7, cl.8), and
TronGrid's Terms of Service (§3, read from the site's own script bundle because
the page is rendered by JavaScript). The NBCTF, mempool.space and Blockstream
findings rest on the researchers' reading and were not re-checked. OFAC is
US government work (17 U.S.C. 105: "Copyright protection under this title is not
available for any work of the United States Government"). None of this is legal
advice.

## Summary

| Source | Supplies | Free public site | Paid API later | Storing / deletion | Free access | Cheapest paid | Attribution | Before showing it |
|---|---|---|---|---|---|---|---|---|
| OFAC SDN | Sanctioned addresses | Yes | Yes | No limits | Free, no key | n/a | None required; must not imply Treasury endorses us | Ready. Word matches as OFAC's listing, say lists are "not likely to be exhaustive" |
| TronGrid | Tron chain data | Not forbidden (licence "for their intended purpose") | Grey: bars "resell ... or provide unauthorized third-party access to the Services" | No limits; docs recommend caching | 10,000 req/day for new accounts (older accounts 100,000), 15 req/s per key | Not published in text; prices load from a config feed we could not read | None | Ready for the free site. Check our plan's daily limit in the TronGrid dashboard |
| GoPlus | Malicious-address flags | Yes, with the GoPlus logo and "Powered by GoPlus" | Needs written permission ("directly use our original data to conduct any commercial activities") | Silent | 150K CU/month | Test Package $199/mo | Mandatory logo; do not modify results | Show logo. Never call it for Bitcoin |
| DefiLlama | Historical prices | Ambiguous: Terms bar "republish the data in any form without permission"; FAQ calls the API "open" and "free to use" | Needs permission or an Enterprise data licence | Ambiguous: bars "copy the materials" | Free, no key, limit not published | API plan $300/mo (one search result said $250) | Not required; "much appreciated" | Ask for written permission |
| NBCTF (Israel) | Seizure-order addresses | No terms published; low risk with a link to the order | Ambiguous: "© ... all rights reserved"; no licence | Silent | Free JSON API | n/a | None stated | Ask nbctf@mod.gov.il; respect robots.txt |
| mempool.space | Bitcoin chain data | Silent: terms have no licence or use clause | Silent | Silent | Rate-limited, numbers not published | Enterprise from $1,499/mo | None; trademark policy allows "powered by mempool.space" | Prototype only, or self-host |
| Blockstream | Bitcoin chain data | Silent in public terms; the Explorer API's own terms sit behind a login, not read | Unknown | Unknown | Free tier being rate-limited (2026-04-22 blog) | 500K credits for $40/mo | None stated | Read the Explorer API terms after signing up, or self-host |

## Source notes

### OFAC SDN list

US Department of the Treasury. Free; no key; no published rate limit.

- **Downloads:** `https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/SDN_ADVANCED.XML`, plus `SDN.XML` and the CSVs. Each answers with a redirect to a presigned link that lasts one hour. Always start from the stable URL and never store the presigned one.
- **Storage:** FAQ 89 says administrators "can overwrite any old data", which is permission, not a duty. Keeping every snapshot is fine, and it is the only way to show a past listing, since the current file holds no history.
- **Automated access:** fetch the files, never script the search tool. FAQ 287 says the search tool "should not be utilized by automated systems".
- **Address format:** in SDN.XML, addresses appear as `<idType>Digital Currency Address - ETH</idType>`. On 2026-11-19 there were 1,082 addresses: 534 XBT, 273 TRX, 124 ETH, 94 USDT and others.
- **Matching:** the tag names a currency, not always a chain, so USDT and USDC entries need the chain worked out from the address format. 67 of the 124 ETH addresses are mixed-case checksums, so matching must ignore case.
- **Delistings:** published under Recent Actions. Store a delisting as a superseding row, not a deletion.
- **Wording:** FAQ 562 says the listings "are not likely to be exhaustive". A report must say an unlisted address is not cleared, and that the decision is OFAC's, not ours.

### TronGrid

[Terms of Service](https://www.trongrid.io/terms-of-service), last updated September 24, 2026. The page is rendered by JavaScript; its text was read from the site's script bundle. The terms name only "TronGrid", under Singapore law.

- **Licence:** §6 grants "a limited, non-exclusive, non-transferable right to use the Services for their intended purpose".
- **Resale:** §3 says "You must not ... resell, sublicense or provide unauthorized third-party access to the Services or API keys." Showing our own reports is not access to the Services. A paid API that passed TronGrid data through could be.
- **Storage:** nothing limits it. The developer docs recommend "backoff, caching, pagination, and checkpointed scans".
- **Limits:** paid plans arrived on 2026-10-22. New accounts get 10,000 requests a day and 15 per second per key. Accounts made before that keep 100,000 a day. Which one ours is was not checked; it shows in the TronGrid dashboard.
- **Warranty:** §7 says "Blockchain data and third-party network data may be incomplete, delayed or inaccurate."

### GoPlus

[API License Agreement](https://docs.gopluslabs.io/reference/api-license-agreement-new), last updated August 21, 2023, with Tops Labs Ltd under New York law.

- **§2 grant:** a licence "to use the API and display certain Company Marks".
- **§3 attribution:** "Except for personal usage, Your Application should source attribution via a backlink or a mention that Your Application is 'Powered by Go+ Security'."
- **§5 logo, mandatory:** "you shall display our Company Mark, specifically the GoPlus logo with 'Powered by GoPlus' expression." Without it, GoPlus reserves the right to terminate and sue.
- **§5 results:** "We do not suggest any modification to our API results." Show the flags as returned.
- **§6 commercial use:** "You shall not directly use our original data to conduct any commercial activities and generate revenue without Goplus's explicit written permission." A free site is fine; a paid API needs permission. §6 also limits "data mining", which in context means scraping to get around their limits, not keeping one response per lookup.
- **Coverage:** the address-security endpoint covers eth, bsc, arbitrum, polygon, base, optimism and tron. It does not cover Bitcoin: a Bitcoin address comes back with every flag "0", which means "not covered", never "clean". Never call it for Bitcoin.
- **Flags are keyed by address, not chain.** A flagged address came back flagged even with a made-up chain id.
- **Wording:** responses name their own sources (for example "SlowMist,BlockSec"). Rule 9 wording becomes "flagged by GoPlus (data from SlowMist, BlockSec)".
- **Plans:** the free plan is 150K CU a month. The Test Package is $199 a month.

### DefiLlama

[Terms of Use](https://defillama.com/terms), effective 24 June 2025, with DefiLlama Limited (UAE). The terms cover the "official public API(s)".

- **cl.7:** a licence "for personal, non-commercial purposes".
- **cl.8** forbids, among other things:
  - "resell the data or resell access to the data through your plan without permission"
  - "republish the data in any form without permission"
  - "copy, scrape, harvest or otherwise exploit the Content & Data for commercial purposes without prior written consent"
- **API access:** it also bars "programmatically access any API other than our official public API", so using the public price API itself is allowed.
- **The conflict:** the FAQ says "Our API is an open API and it free to use. Citing DefiLlama as the source is much appreciated." The Terms are the binding text. A USD value computed from their price and shown on our site is republished data, so ask first. Penalties in cl.14 go up to USD 100,000 per violation.
- **Coverage gaps:** on 2026-11-19, Base USDC, Polygon USDT and Arbitrum USDT returned no price. Reports need a "no price available" path, and any fallback (such as $1 for a stablecoin) must be labelled as our choice.

### NBCTF (Israel Ministry of Defense)

- **New site:** https://matal.mod.gov.il. The old nbctf.mod.gov.il is behind a bot wall.
- **No terms:** the "terms of use" page holds placeholder text, and the footer reads "© 2024 זכויות שמורות למט״ל ומשרד הביטחון" ("rights reserved to NBCTF and the Ministry of Defense"). There is no licence to quote.
- **Copyright:** under Israeli law (section 36) the state owns copyright in works by its employees. Whether seizure orders are exempt like statutes is unclear. Wallet addresses themselves are facts.
- **Access:** a public JSON API lists 1,732 wallet records. robots.txt says `Disallow: /`, so scheduled fetches should be rare and polite, or agreed with NBCTF.
- **Data quality:**
  - Many records are 8- or 9-digit Binance account IDs, not addresses.
  - The chain has to be inferred.
  - Wallet records carry no order number; that comes from the order pages, which give issue and expiry dates.
- **Third-party copy:** OpenSanctions republishes it under CC BY-NC 4.0, which is its own licence. Do not take it from there for a paid product.

### mempool.space

- **Terms:** the [Terms of Service](https://mempool.space/terms-of-service) (updated July 10, 2024) cover "their associated API services". They contain only disclaimers: no licence, no use clause, no storage clause. Silent, not permissive.
- **Limits:** rate limits are enforced, with no numbers published.
- **Paid plans:** enterprise plans start at $1,499 a month, read from the price feed the site loads (the page itself shows no prices).
- **Self-hosting:** the software is AGPL-3.0. Running our own instance against our own Bitcoin node removes the hosted terms. AGPL would only reach code we modify and serve, not our own code that calls it.

### Blockstream

- **Terms:** the public [terms](https://blockstream.com/terms) are general and silent on API use.
- **Explorer API:** it has its own terms, shown at first login to dashboard.blockstream.info. Nobody has read them yet.
- **Limits:** the free tier got rate limits on 2026-04-22, "designed for individual use, prototyping, and development".
- **Paid plans:** Basic is 500K credits for $40 a month.
- **Self-hosting:** the esplora and electrs software is MIT-licensed, so self-hosting is the clean route here too.

## What this means

- **OFAC:** ready to show.
- **TronGrid:** ready for the free site. A paid API later needs a word with TronGrid.
- **GoPlus:** can be shown on the free site with the logo. A paid API needs written permission.
- **DefiLlama and NBCTF:** need written permission before their data appears on the public site.
- **Bitcoin:** both hosted APIs are fine for development. For the public site, either read Blockstream's Explorer API terms after signing up, or self-host esplora or mempool. Self-hosting is the only path with no terms question at all.

Every source row's `license` and `terms_checked_on` were updated in migration 0006.

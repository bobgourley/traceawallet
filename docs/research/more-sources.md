# More free sources: what else can go in a report?

2026-11-20

**Recommendation in short.** Build these three first:

1. **More stablecoin freeze checks.** Extend the freeze adapter we already have to USDC and EURC on Base, Polygon and Avalanche, USDT0 on Polygon and Optimism, Avalanche USDt, and the Paxos, First Digital, World Liberty and Ripple dollars. The data is the issuer's own on-chain record, there are no terms to clear, and a frozen balance is the one result that can lead to money coming back. Small job.
2. **California DFPI Crypto Scam Tracker.** One public page, public domain, last changed the day I checked. It matches the platform's website and name, and about 125 wallet addresses written into its complaint narratives. Small to medium job.
3. **Domain age from RDAP and certificate transparency (crt.sh).** Every website a victim types in gets an answer: when the domain was registered, and when its first HTTPS certificate was issued. Both still answer after a site has gone dark. Small to medium job.

Next in line: Blockscout's address tags for first-hop destinations (high value, but wait for Blockscout's written answer, which we already need for other reasons), then the CFTC RED List and SEC PAUSE list.

Three sources have terms that rule out our use as things stand: **Etherscan labels** (and the GitHub datasets scraped from them), **Dune's Spellbook labels** until 3 March 2027, and **the UK FCA warning list** taken from the FCA website. Details below.

## How this was checked

I read each source's own terms page, licence file or API docs on 2026-11-20 and ran small live tests where I could. "Verified" below means I read the text or ran the call myself that day. "Not verified" means I could only see it second-hand, usually because the site blocked automated reading (IOSCO, the FCA warning list page and parts of sec.gov sit behind Cloudflare or SEC rate checks). "Not stated" means I looked and found no terms. None of this is legal advice.

Two live tests shaped the ranking:

- **Phishing lists rarely catch investment-fraud sites.** I took the 805 distinct website domains in the DFPI tracker and checked them against MetaMask's and ScamSniffer's blocklists. MetaMask had 4. ScamSniffer had 25. Together, 29 of 805 (3.6%). Those lists are built for wallet drainers and fake airdrop pages, which is a different crime from the "investment platform" sites our users meet.
- **Domains in the tracker disappear fast.** Of 15 random `.com` domains from the DFPI tracker, 6 no longer existed at the registry. crt.sh still had the certificate history for one of the dead ones (`defiwa11etbch.com`, first certificate 2024-03-28).

## Ranking

Value is to a fraud victim or the investigator helping them. Effort assumes our existing adapter pattern.

| Rank | Source | Checks | Value | Effort | Terms for us |
|---|---|---|---|---|---|
| 1 | Stablecoin freeze status, more tokens and chains | Address | High | Small | No terms: public chain state |
| 2 | California DFPI Crypto Scam Tracker | Website, name, address | High when it hits | Small to medium | Public domain (verified) |
| 3 | RDAP registration date + crt.sh first certificate | Website | Medium, always answers | Small to medium | RDAP: low-volume use fine (verified). crt.sh: not stated |
| 4 | Blockscout address tags (metadata service) | Destinations, address | High | Small to medium | Not stated for tags; ask Blockscout |
| 5 | CFTC RED List | Website, name | Medium | Small | Public domain, credit requested (verified) |
| 6 | SEC PAUSE list | Website, name | Medium | Small to medium | US government work; SEC automated-access rules apply |
| 7 | GoPlus phishing-site check | Website | Low to medium | Small | Same GoPlus licence we already follow |
| 8 | Google Safe Browsing | Website | Low to medium | Small | Free for non-commercial use, strict wording (verified) |
| 9 | ScamSniffer domain list | Website | Low for our users | Small | GPL-3.0 |
| 10 | MetaMask eth-phishing-detect | Website | Low for our users | Small | Permissive custom licence (verified) |
| 11 | L2BEAT contract lists | Destinations | Medium | Medium | MIT (verified) |
| 12 | CourtListener RECAP search | Address | High when it hits | Medium | Free tier too small; our use needs their OK |
| 13 | IOSCO I-SCAN | Website, name | Medium | Medium | Signed agreement needed (partly verified) |
| 14 | FBI FLASH on Funnull hosting | Website | Low to medium | Small | US government work, TLP:CLEAR (verified) |
| 15 | ENS primary name | Address | Low | Small | No terms: public chain state |
| 16 | DefiLlama adapter wallet lists | Destinations | Medium | Medium | No licence file; DefiLlama terms unclear |
| 17 | Chainabuse API | Address, website | High data, no access | n/a | Free key is 10 calls a month; terms not found |
| 18 | forfeiture.gov | Address | Low | Large | US government work |
| Out | Etherscan labels, eth-labels, etherscan-labels | Destinations | | | Forbidden by Etherscan terms |
| Out | Dune Spellbook labels | Destinations | | | Forbidden by licence until 2027-03-03 |
| Out | FCA warning list from fca.org.uk | Website, name | | | Forbidden without written permission |
| Out | Phishfort, CryptoScamDB, MEW darklist, Polkadot list, Interpol | | | | Closed, stale, off-topic or nonexistent |

## Sources for the platform name or website

### California DFPI Crypto Scam Tracker

**What it offers.** A table of scams reported to California's financial regulator. Columns: Primary Subject, Complaint Narrative, Scam Type, Website, Screenshot. On 2026-11-20 it had 656 entries naming 805 distinct domains. The narratives also quote wallet addresses: I counted 93 EVM, 2 Tron and about 30 Bitcoin addresses. There is no date column. (Verified.)

**How to get it.** Scrape one page: `https://dfpi.ca.gov/consumers/crypto/crypto-scam-tracker/`, about 1 MB of HTML holding a single table. No API or download. robots.txt allows the page for ordinary clients; it blocks AI crawlers such as GPTBot and ClaudeBot, and our server fetch is neither. (Verified.)

**Update frequency.** The page says "As new scams emerge, the DFPI will update this list on an ongoing basis." Its modified date was 2026-11-20. Fetching once a day is plenty.

**Terms.** DFPI's [Conditions of Use](https://dfpi.ca.gov/conditions-of-use/): "In general, information presented on this web site, unless otherwise indicated, is considered in the public domain. It may be distributed or copied as permitted by law." Photographs are excluded ("the State does make use of copyrighted data (e.g., photographs) which may require additional permissions"), so we should not copy the screenshots. No clause on automated access. Storing copies: allowed under the public-domain wording. (Verified 2026-11-20.)

**Proposed wording.**
- Website match: "The website capnovas.com is listed in the California DFPI Crypto Scam Tracker (our copy of 2026-11-20). DFPI says entries are based on consumer complaints and that it "has not verified the losses reported by complainants."" Link to the tracker.
- Address match: "This address is quoted in a complaint narrative in the California DFPI Crypto Scam Tracker, in the entry for "grokai-network.net"."
- Name-only match: "A platform named "X" appears in the California DFPI Crypto Scam Tracker. The website listed there is Y; it may not be the same platform." DFPI itself warns that "The companies or websites listed may sound similar to the names of other companies or websites."

**Build effort.** Small to medium. One fetch, one HTML table parse, plus careful domain and address extraction from free text.

**Caveats.** Complaints, not findings. Some entries name lookalikes of real firms (the page lists `coindcx.win` and warns it is not `coindcx.com`), so we must match whole hostnames, never "contains". Only California complaints. No entry dates, so we record when we first saw each row.

### CFTC RED List

There is no "Registration Deletion list". The CFTC's list is the **Registration Deficient List**, or RED List.

**What it offers.** Foreign firms that appear to need CFTC registration and don't have it. About 284 entries on 2026-11-20, dated 2015 to 2025. Each entry page gives the web address, country, operator, product type and date added. Example: AAFX Trading, `https://www.aafxtrading.com/`, added 04/25/2017. (Verified.)

**How to get it.** Scrape the list (`https://www.cftc.gov/LearnAndProtect/Resources/Check/redlist.htm?items_per_page=All` returned every row) and each entry page for the web address. No API.

**Update frequency.** Irregular batches. 2025 added 42 entries; none so far dated 2026.

**Terms.** CFTC [Web Policy](https://www.cftc.gov/WebPolicy/index.htm): "Government information at the CFTC website is in the public domain. Public domain information may be freely distributed and copied, but it is requested that in any subsequent use the CFTC be given appropriate acknowledgement." (Verified 2026-11-20.)

**Proposed wording.** "The CFTC lists "AAFX Trading" (aafxtrading.com) on its RED List of unregistered foreign firms, added 04/25/2017. The CFTC says inclusion "does not mean that the CFTC or a court has concluded that a violation of any provision of the Commodity Exchange Act or the Commission's regulations has occurred.""

**Build effort.** Small. About 300 pages to fetch once, then the new ones.

**Caveats.** Mostly forex and binary-option brokers. Pig-butchering sites change domain too often to be listed. A name match without a website match is weak.

### SEC PAUSE list

**What it offers.** Firms that, per the SEC, falsely claim to be registered, licensed or located in the US, plus impersonators of real firms and fake regulators. Entries give the name, website, email, phone, address and date. Example: "Equities Registrar", `equitiesregistrar.com`, Oct. 14, 2022. (Entry fields verified on one entry; I could not load the full list page, so the entry count is not verified.)

**How to get it.** Scrape `https://www.sec.gov/enforce/pause-program-entities` and its entry pages. The SEC's developer page limits automated access to "no more than 10 requests per second" and blocks clients it can't classify; sec.gov asks scripts to send a User-Agent naming the operator and a contact address. That is the owner's call, since it means giving the SEC a contact email.

**Update frequency.** The landing page said "last reviewed" May 22, 2024. Not verified whether entries have been added since.

**Terms.** Content written by the SEC is US government work and not under copyright (17 U.S.C. 105). Not verified: any separate SEC site licence.

**Proposed wording.** "The SEC's PAUSE list includes "Equities Registrar" (equitiesregistrar.com), added Oct. 14, 2022, among entities the SEC says falsely claimed to be registered, licensed or located in the United States. Being on the list is not an SEC finding that the securities laws were broken."

**Build effort.** Small to medium. Scrape plus sec.gov's access rules.

**Caveats.** Its contact details come from the soliciting entities themselves. Coverage is securities-style solicitation rather than crypto platforms. Possibly not updated since 2024.

### UK FCA warning list

**What it offers.** The UK's list of firms operating without FCA authorisation, including many crypto "investment" sites.

**How to get it.** Not allowed from the FCA site. The [FCA legal terms](https://www.fca.org.uk/legal) (last updated 12/10/2026) say users must not "use nor attempt to use any "scraper," "robot," "bot," "spider," "data mining," ... to access, acquire, copy, or monitor any portion of the website ... without the prior written consent of the FCA" (4.8), and "You must not reproduce or store any part of this site in any other website ... without our prior written permission" (3.3), and "You must not use data from this site to provide a data feed to any comparison table or any other website without our written permission" (3.4). (Verified 2026-11-20.) FCA alerts also reach IOSCO I-SCAN, so I-SCAN is the legitimate route.

**Verdict.** Forbidden as a direct source unless the FCA gives written permission. Asking costs one email.

### IOSCO I-SCAN

**What it offers.** One searchable pool of investor alerts from securities regulators worldwide, FCA alerts included.

**How to get it.** An API at `https://api.iosco.org/v1/i-scan/warnings`, Bearer key, 10 requests a second, with a one-hour IP block if exceeded (verified from the [API docs](https://api.iosco.org/v1/i-scan/)). To get a key you upload a signed "API Terms of Use Agreement". The [warninglists](https://github.com/mariotrerotola/warninglists) project says the website also offers a CSV export; not verified, because iosco.org blocked my requests.

**Update frequency.** Not stated.

**Terms.** I could not read the API agreement PDF (blocked). Second-hand only: search summaries say the data comes "as is", that the regulator who issued each alert is responsible for it, and that IOSCO's general website terms allow redistribution for non-commercial purposes. The warninglists project summarises reuse as "public information; reuse requires attribution and no distortion". All not verified.

**Proposed wording.** "IOSCO's I-SCAN lists an investor alert about "X" (x.com), issued by [regulator] on [date]."

**Build effort.** Medium: sign the agreement, then a simple API adapter.

**Caveats.** The portal says it is not a complete list of members' alerts. Names in alerts are often generic.

### Domain registration date (RDAP)

**What it offers.** The registry's record for a domain: registration, expiry and last-changed dates, registrar and status. A site registered weeks before the victim found it is a useful fact for an investigator.

**How to get it.** Free JSON over HTTPS. IANA's bootstrap file (`https://data.iana.org/rdap/dns.json`, 592 registries on 2026-11-20) says which server answers for each ending. For `.com` and `.net` that is `https://rdap.verisign.com/com/v1/domain/<name>`. (Verified.)

**Update frequency.** Live.

**Terms.** Each registry sets its own. Verisign's [RDAP terms](https://www.verisign.com/domain-names/registration-data-access-protocol/terms-service/index.xhtml) give the data "for information purposes only" and bar "high volume, automated, electronic processes that send queries or data to the systems of Verisign". They say nothing on storing or republishing. One lookup per report is not high volume. Other registries' terms: not checked one by one. (Verisign verified 2026-11-20.)

**Proposed wording.** "The .com registry's record (RDAP, read 2026-11-20) gives capnovas.com a registration date of 2025-12-16." When the registry has no record: "The registry had no record of this domain on 2026-11-20. It may have expired or been deleted."

**Build effort.** Small to medium: the bootstrap lookup, one HTTP call, date parsing, and a cache by domain.

**Caveats.** An old date proves little: lzfu.com (2005) and best-pam.com (2012) are both in the DFPI tracker, so old or re-bought domains get used too. Registrant names are almost always hidden. Some country-code registries run no RDAP server.

### Certificate transparency (crt.sh)

**What it offers.** Every public HTTPS certificate is logged. The first certificate for a domain is a good stand-in for "when the site went live", and the log keeps it after the domain dies.

**How to get it.** `https://crt.sh/?q=<domain>&output=json`, no key. (Verified: returned 10 certificates for a domain the registry no longer knows.)

**Update frequency.** Live, within hours of issuance.

**Terms.** Not stated. The site shows only "© Sectigo Limited 2015-2026. All rights reserved." A Sectigo staffer wrote in the crt.sh Google Group in 2023 that the web endpoint allows about 5 requests a minute per IP. Not verified as current. The underlying CT logs are public by design.

**Proposed wording.** "Certificate transparency logs (searched through crt.sh) show the first HTTPS certificate for this domain was issued on 2024-03-28."

**Build effort.** Small, but crt.sh is slow and often fails, so it needs a timeout and a "not available" outcome. SSLMate's Cert Spotter API is a fallback; its terms were not checked.

**Caveats.** Subdomain certificates show up too and should be filtered. A missing certificate doesn't mean anything.

### MetaMask eth-phishing-detect

**What it offers.** MetaMask's blocklist, 121,413 domains on 2026-11-20, plus an allowlist and a fuzzy-match list. (Verified.)

**How to get it.** One JSON file on GitHub: `src/config.json` in `MetaMask/eth-phishing-detect`. Updated many times a day.

**Terms.** The repository uses the "Don't Be A Dick Public License" 1.2: "Do whatever you like with the original work, just don't be a dick", with examples that include "Selling the unmodified original with no work done what-so-ever" and claiming the work as your own. Showing a match with credit is fine. (Verified 2026-11-20.)

**Proposed wording.** "This domain is on MetaMask's open phishing blocklist (eth-phishing-detect)."

**Build effort.** Small.

**Caveats.** Only 4 of 805 DFPI domains matched. Use exact hostname and parent-domain matching only; MetaMask's own fuzzy matching would flag legitimate lookalike names, and we must never run it.

### ScamSniffer scam database

**What it offers.** 362,418 crypto phishing domains on 2026-11-20, drainers and fake claim pages above all. (Verified.)

**How to get it.** `blacklist/domains.json` in `scamsniffer/scam-database` on GitHub. The README says it is "Updated daily (~05:00 UTC)" and "Published with a 7-day delay". Its address lists stopped in February 2024.

**Terms.** GPL-3.0 licence file. GPL is written for software; using the list on our server does not oblige us to publish our code, and showing a match with credit is within it. (Licence verified; how GPL applies to a data file is my reading.)

**Proposed wording.** "This domain is on ScamSniffer's crypto phishing blocklist."

**Build effort.** Small. A 9 MB file once a day.

**Caveats.** 25 of 805 DFPI domains. A week behind.

### Other open blocklists

- **Phishfort.** The GitHub list says it "will be disabled from May 1, 2025". Its replacement page now says "The public blocklist API service is officially deprecated" and points to a partner-only API. Not available. (Verified.)
- **CryptoScamDB.** Last change April 2023, no licence. Stale. (Verified.)
- **MyEtherWallet ethereum-lists** (MIT). The address and URL darklists were last changed in November 2020. Stale. (Verified.)
- **Polkadot phishing list** (Apache-2.0, updated September 2026). Its domains target Polkadot users and its addresses are Substrate addresses, which we don't trace. Low value. (Verified.)

### GoPlus phishing-site check (added)

**What it offers.** A yes or no on a URL from the provider we already use. `https://api.gopluslabs.io/api/v1/phishing_site?url=...` returned `"phishing_site":1` for a ScamSniffer-listed domain and `0` for a DFPI-listed one. (Verified 2026-11-20.)

**Terms.** The GoPlus licence we already follow (see `data-sources.md`): logo and "Powered by GoPlus", no modifying results.

**Proposed wording.** "Flagged by GoPlus as a phishing site." Show nothing when GoPlus says 0; a 0 is not a clean bill of health.

**Build effort.** Small: one more call in the existing GoPlus adapter.

**Caveats.** Same drainer-focused coverage as the open lists.

### Google Safe Browsing (added)

**What it offers.** Google's lists of social-engineering, malware and unwanted-software sites.

**Terms.** "The Safe Browsing API is for non-commercial use only", meaning "not for sale or revenue generating purposes"; commercial users must use Web Risk instead. Warnings must not "lead users to believe that the page in question is, without a doubt, an unsafe web resource", must use words like "suspected" or "potentially", must say "Advisory provided by Google" with a link, and may not show data older than the service's cache time. ([usage limits](https://developers.google.com/safe-browsing/v4/usage-limits), verified 2026-11-20.) Free, with a key from Google Cloud.

**Proposed wording.** "Google Safe Browsing lists this website as a suspected deceptive (phishing) site. Advisory provided by Google."

**Build effort.** Small.

**Caveats.** The cache rule clashes with our snapshot reports: a stored report cannot keep showing an old Google warning. It needs a fresh check each time the report is shown, or the line dropped from older reports. A later paid API would need Web Risk, which is paid.

### FBI FLASH on Funnull hosting (added)

**What it offers.** FLASH 20250529-001 (29 May 2025, TLP:CLEAR) lists hostnames of Funnull Technology, which the FBI says hosted "thousands of websites associated with cryptocurrency investment fraud (CIF) scams". The FBI tied 548 Funnull hostnames to "over 332,000 unique domains". The PDF lists the hostnames, not the 332,000 domains. (Verified.)

**How to use it.** Look up the victim's domain in DNS and see whether it points (CNAME) to one of the listed hostnames.

**Terms.** US government work. The FLASH is released TLP:CLEAR, "with no guarantees or warranties, for potential use at the sole discretion of recipients".

**Proposed wording.** "On 2026-11-20 this website's DNS pointed to b3b73d16.u.fn01.vip, a hostname the FBI listed in FLASH 20250529-001 as infrastructure of Funnull Technology, which the FBI says hosted thousands of crypto investment fraud websites."

**Build effort.** Small: one DNS query and a fixed list.

**Caveats.** A frozen 2025 snapshot; Funnull has likely moved since. A dead site gives no answer. The wording needs the owner's judgement, since it puts a victim's site next to the FBI's description.

### Other state regulators and Interpol

Washington DFI and Wisconsin DFI also run investment scam trackers. I saw them in search results but did not evaluate them. Interpol publishes no list of scam sites or addresses; its public data is Red and Yellow Notices about people.

## Sources for the address

### More stablecoin freeze checks

Our `taw/adapters/labels/freeze.py` already reads USDT and USDC on Ethereum, Arbitrum, Optimism and Tron. I called each function below with `eth_call` on public RPC nodes on 2026-11-20 and got a valid answer. Each answer was `false` for a test address, which proves the function exists. ABIs were read from Blockscout's verified source for the Ethereum contracts.

| Token | Chain | Contract | Status function (selector) | Freeze / unfreeze events | Address indexed? |
|---|---|---|---|---|---|
| USDC | Base | 0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913 | `isBlacklisted(address)` (0xfe575a87) | Blacklisted / UnBlacklisted | Yes |
| USDC | Polygon | 0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359 | `isBlacklisted(address)` | same | Yes |
| USDC | Avalanche | 0xB97EF9Ef8734C71904D8002F8b6Bc66Dd9c48a6E | `isBlacklisted(address)` | same | Yes |
| EURC | Ethereum, Base | 0x1aBaEA1f7C830bD89Acc67eC4af516284b1bC33c, 0x60a3E35Cc302bFA44Cb288Bc5a4F316Fdb1adb42 | `isBlacklisted(address)` | same | Yes |
| USDT0 | Polygon | 0xc2132D05D31c914a87C6611C10748AEb04B58e8F (name() now returns "USDT0") | `isBlocked(address)` (0xfbac3951) | BlockPlaced / BlockReleased | Yes |
| USDT0 | Optimism | 0x01bFF41798a0BcF287b996046Ca68b395DbC1071 | `isBlocked(address)` | BlockPlaced / BlockReleased | Yes |
| USDt | Avalanche | 0x9702230A8Ea53601f5cD2dc00fDBc13d4dF4A8c7 | `isBlocked(address)` | not checked | |
| PYUSD | Ethereum | 0x6c3ea9036406852006290770BEdFcAbA0e23A0e8 | `isFrozen(address)` (0xe5839836) | FreezeAddress / UnfreezeAddress | Yes |
| PYUSD | Arbitrum | 0x46850aD61C2B7d64d08c9C754F45254596696984 | `isFrozen(address)` | not checked | |
| USDP | Ethereum | 0x8E870D67F660D95d5be530380D0eC0bd388289E1 | `isFrozen(address)` | FreezeAddress / UnfreezeAddress | Yes |
| USDG | Ethereum | 0xe343167631d89B6Ffc58B88d6b7fB0228795491D | `isFrozen(address)` | FreezeAddress / UnfreezeAddress | Yes |
| FDUSD | Ethereum, BNB Chain | 0xc5f0f7b66764F6ec8C8Dff7BA683102295E16409 (same on both) | `frozen(address)` (0xd0516650) | Freeze / Unfreeze (caller, account) | Yes, both |
| USD1 | Ethereum, BNB Chain | 0x8d0D000Ee44948FC98c9B98A4FA4921476f08B0d (same on both) | `frozen(address)` | Freeze / Unfreeze (caller, account) | Yes, both |
| RLUSD | Ethereum | 0x8292Bb45bf1Ee4d140127049757C2E0fF06317eD | `accountPaused(address)` (0xbc8c4b4f) | AccountPaused / AccountUnpaused | **No** |
| TUSD | Ethereum | 0x0000000000085d4780B73119b644AE5ecd22b376 | none public | Blacklisted(account indexed, bool) | Yes |
| sUSDe | Ethereum | 0x9D39A5DE30e57443BfF2A8307A4256c8797A3497 | `hasRole(FULL_RESTRICTED_STAKER_ROLE, address)` | RoleGranted / RoleRevoked | Yes |

No freeze function on: **USDe** itself (Ethena; only staked sUSDe has a blacklist), **USDT and USDC on BNB Chain** (Binance-bridged copies), **DAI/USDS**. Out of scope for now: PYUSD on Solana and Stellar, RLUSD on the XRP Ledger.

PYUSD, USDP and USDG also have `wipeFrozenAddress`, and TUSD has `destroyBlackFunds`. These issuers can destroy a frozen balance, and USD1 logs `FrozenFundsReallocated`. A report could say so when the event appears.

**Terms.** None: this is public contract state. The issuer's role is a fact recorded on chain.

**Proposed wording.** Same pattern as today, naming the issuer's own function: "Frozen by Paxos: the PYUSD contract on Ethereum reports isFrozen = true at block N." For RLUSD use Ripple's word: "Paused by Ripple: the RLUSD contract reports accountPaused = true." For sUSDe: "Ethena's staked USDe contract lists this address as fully restricted."

**Build effort.** Small for everything with a status function: new rows in a table we already have. Medium for TUSD (event scan only) and RLUSD freeze dates (its events don't index the address, so it needs the same global-index trick we use for Ethereum USDT).

**Caveats.** Only says something if the victim's money is still sitting there in that token. Issuers rarely freeze on their own; a "no" is normal and must not be read as anything.

### California DFPI tracker (address match)

Covered above. About 125 addresses sit in the narratives. Small extra effort once the tracker is parsed.

### CourtListener (RECAP)

**What it offers.** Full text of federal court filings that anyone bought on PACER through the RECAP extension. Civil forfeiture complaints for scam money (our five test cases among them) list full wallet addresses. A hit is very strong evidence for an investigator.

**How to get it.** REST API, search with `type=rd` for documents, with a free account token. Without a token the search API returns 401 (verified). I could not test whether a wallet address in a PDF's text is found by search, because I have no token.

**Rate limits.** "By default, authenticated users may make up to 5 requests per minute, 50 requests per hour, and 125 requests per day." (Verified, [API overview](https://wiki.free.law/c/courtlistener/help/api/rest/v4/overview).) That is too few for a public site unless we cache hard and search only addresses that have received money from a victim.

**Terms.** [Allowed API usage](https://free.law/membership/allowed-api-usage/): membership access is "for personal, educational, research, journalistic, and exploratory use"; "Small organizations that are pre-revenue and unfunded are welcome"; but it "may not be used to build tools for for-profit or non-profit organizations". Commercial users "should contact our partnerships team". TraceAWallet is free and unfunded but is a tool for the public, so it falls between these lines. Ask Free Law Project in writing. CourtListener's own terms page blocked me: data licence and storage rules not verified.

**Proposed wording.** "This address appears in a filing in [court], [case name], case [number], filed [date], available through CourtListener's RECAP archive [link]. The filing is the government's or the parties' account, not a court's finding unless the document says so."

**Build effort.** Medium: token, tight rate-limit queue, caching, and parsing results to the docket.

**Caveats.** Only documents someone already bought are in RECAP. Addresses split across lines in PDF text can be missed.

### forfeiture.gov

**What it offers.** Official notices of pending federal forfeitures, as PDF only ("All notices are provided as PDF (Portable Document Format) files"). (Verified.)

**Caveat that decides it.** Law-firm guides say crypto notices usually show only the last four characters of the wallet, for example "anonymous Wallet XXXX33F4 held at Tether Holdings Limited". Not verified on forfeiture.gov itself. Four characters plus an amount cannot identify an address safely.

**Terms.** DOJ work, US government. Not checked further.

**Proposed wording.** Not proposed.

**Build effort.** Large (PDF scraping) for little value. Skip. CourtListener and DOJ press releases carry full addresses.

### ENS primary name

**What it offers.** The name an Ethereum address has chosen for itself, such as `vitalik.eth`.

**How to get it.** One `eth_call` to ENS's Universal Resolver `reverse()`, which also checks that the name points back to the address (per ENSIP-19). Tested: `vitalik.eth` came back for Vitalik Buterin's address; Binance 14 has none. Fits the eth_call path we already use through Blockscout.

**Terms.** None; public contract state. ENS contracts are MIT.

**Proposed wording.** "This address has set the ENS name "foo.eth" as its primary name (checked in both directions on 2026-11-20). Anyone can register an ENS name; the name does not show who controls the address."

**Build effort.** Small.

**Caveats.** Scam addresses rarely set one, and when they do the name is often chosen to mislead.

### Blockscout address tags

**What it offers.** Name tags and category tags for addresses: exchange hot wallets, routers, bridges, mixers, hacks, sanctions and stablecoin blocks. Examples from 2026-11-20: Binance 14 is tagged "Binance: Hot Wallet", "Exchange"; Uniswap's router "Universal Router V1.2", "DEX"; the Tornado Cash router "Tornado.Cash: Router", "BLOCKED" ("Address blocked by a custodial stablecoin provider"). Several tags carry an icon credited to the Open Labels Initiative (OLI). (Verified.)

**How to get it.** Blockscout's metadata service. `https://metadata.services.blockscout.com/api/v1/metadata?addresses=A,B,C&chainId=1` answered without a key and takes several addresses at once. The documented PRO route is `https://api.blockscout.com/metadata/api/v1/...` with our key ([docs](https://docs.blockscout.com/devs/metadata-service)).

**Update frequency.** Not stated.

**Terms.** The docs give no terms or sources for the tags. The PRO API terms we already reviewed (`evm-sources.md`) apply to the PRO route and do not mention tags. OLI's repository is MIT, but that covers its code; a licence for the label data is not stated. Add tags to the questions we already plan to send Blockscout.

**Proposed wording.** "Blockscout tags this address "Binance: Hot Wallet" (exchange)." Where the tag credits OLI: "Blockscout shows the tag "...", credited to the Open Labels Initiative." Never repeat a risk tag as our own words: "Blockscout tags this address "Exploit"", not "this is an exploit address".

**Build effort.** Small to medium: one batch call per report covering the address and its first-hop destinations.

**Caveats.** Mixed sources of uneven quality. Ethereum and a few L2s; Tron and Bitcoin not covered.

### Chainabuse (added)

**What it offers.** User scam reports by address and domain, run by TRM Labs.

**Access.** "Every Chainabuse user can generate a standard API Key with a usage limit of 10 calls per month", each covering up to 50 reports. Partners get up to 5,000 calls an hour by application ([TRM docs](https://docs.trmlabs.com/guides/chainabuse/getting-started-2-1), verified). Ten calls a month is unusable.

**Terms.** Not found. chainabuse.com links only TRM's privacy and cookie policies; the usual terms URLs return 404. The TRM docs list the API under a "Proprietary API" licence without text.

**Proposed wording if access is granted.** "N reports about this address have been filed on Chainabuse, a public reporting site run by TRM Labs. Reports are made by users; Chainabuse marks M of them as verified."

**Verdict.** Worth an email asking for partner access for a free victim tool. Nothing to build until then.

### Etherscan labels (not allowed)

Etherscan's [terms](https://etherscan.io/terms) (last updated August 13, 2026) bar users to "Engage in the reproduction of any content posted (such as public labels or name tags) or extracted from our APIs" and "Engage in Automated Data Collection (scraping) unless such Automated Data Collection is confined solely to search indexing". (Verified 2026-11-20.) GitHub datasets built by scraping Etherscan's label cloud carry the same problem whatever licence the repository shows: `dawsbot/eth-labels` (MIT on the code, data from Etherscan and sister explorers) and `brianleect/etherscan-labels` (MIT, last updated 2023). Do not use.

## Sources for destinations (first-hop addresses)

### L2BEAT

**What it offers.** For about 315 rollups and bridges, a `discovered.json` file listing every contract with its role: escrows, gateways, bridges. (Verified layout.)

**How to get it.** The `l2beat/l2beat` GitHub repository, `packages/config/src/projects/<project>/`. Updated daily.

**Terms.** MIT licence. (Verified.)

**Proposed wording.** "L2BEAT lists this contract as part of Across (bridge escrow)."

**Build effort.** Medium: parse hundreds of files into one table, rebuild weekly.

**Caveats.** Bridges and L2s only, mostly on Ethereum. No exchanges or mixers.

### DefiLlama adapters

**What it offers.** Protocol contract addresses and, in `projects/helper/cex.js` and the `cex/` folder, exchange wallet lists used for DefiLlama's CEX transparency pages, including Bitcoin addresses.

**Terms.** The repository has no licence file, which by default means all rights reserved. DefiLlama's own terms bar republishing data without permission (see `data-sources.md`). Fold this into the permission request already planned for DefiLlama prices.

**Build effort.** Medium.

### Dune Spellbook labels (not allowed until 2027)

Spellbook's licence is Business Source License 1.1. Its use grant says "you may not use the Licensed Work for a Data or Analytics Platform", meaning anything that "allows third parties ... to access the functionality or outputs of the Licensed Work". Our public report is exactly that. The licence changes to GPL-3.0 or later on **Mar 3, 2027**. (Verified 2026-11-20.) Revisit then.

## What I could not verify

- The IOSCO I-SCAN API agreement text, and the I-SCAN CSV export (iosco.org blocked automated reading).
- The size and last update of the SEC PAUSE list, and any SEC site licence beyond the federal-works rule.
- CourtListener's terms page, and whether RECAP search finds wallet addresses inside PDF text (needs a token).
- That forfeiture.gov crypto notices show only the last four characters of an address (law-firm sources only).
- Chainabuse's terms of use (none found).
- crt.sh's current rate limit and any terms.
- The licence of Blockscout's tag data and of the OLI label pool.
- Terms of RDAP registries other than Verisign.

## Emails worth sending

1. Blockscout: may a free public site show metadata tags, and store them? (Add to the email already planned.)
2. Free Law Project: does a free, unfunded victim tool qualify for membership API access, and at what rate?
3. TRM Labs / Chainabuse: partner access for a non-commercial victim tool, and the terms that come with it.
4. FCA: written permission to use the warning list, or confirmation that I-SCAN is the route.
5. IOSCO: the API agreement, before signing.

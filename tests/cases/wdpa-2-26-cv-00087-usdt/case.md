# W.D. Pa. 2:26-cv-00087: 5,680,000 USDT (address ending 74bf)

- **Court:** United States District Court for the Western District of Pennsylvania
- **Docket:** 2:26-cv-00087 (printed "Civil Action No. 2:26cv87")
- **Filed:** 2026-01-15
- **Caption:** United States of America v. 5,680,000 USDT Tokens Stored Within
  Virtual Currency Address 0x7f6Ed26BB1488D1b420E10D722ef8bDf7D845B31 That Are
  in the Custody or Control of the United States Marshals Service
- **Document:** https://storage.courtlistener.com/recap/gov.uscourts.pawd.326700/gov.uscourts.pawd.326700.1.0.pdf
  (15 pages; sha256 in `expected.json`). No justice.gov press release was found.

## Addresses

The complaint prints 11 addresses in full, all Ethereum-format. Each one's
mixed-case EIP-55 checksum is valid, which confirms the transcription
character by character.

The complaint never names the network, so they are checked on Ethereum. It
names tokens (USDT, USDC), not contracts; the checker reads Tether's and
Circle's Ethereum contracts.

`0xFA0a50Dc366E2F0a5381E4d4b5fEdE589Ab633A9` is printed twice: in lower case
in paragraph 16, and with its checksum in paragraph 22. No partial address
appears without its full form somewhere in the complaint.

## Facts

There are 18 facts, all in `expected.json` with paragraph numbers and quotes.

- **F1 and F1b: the first payment, stated twice.** Paragraph 16 gives a
  Coinbase screenshot's figure, 242,209.53 USDC. Paragraph 22 says
  "approximately 242,209 USDC" on May 10, 2025. Both are kept. F1b anchors the
  chain, so a difference in the screenshot's exact cents can't stop the rest
  of the chain from being checked.
- **F1b to F11: a chain of transfers.** It starts with that payment on
  2025-05-10. It runs
  through nine addresses to the 74bf address (paragraph 31). After the first,
  each hop is timed relative to the one before ("less than one hour later",
  "approximately nine hours later"). The checker looks for each transfer
  within that time of the last one it found.
- **F12 and F13: the 74bf address's USDT balance** of 5,680,000 on
  2025-09-15 (paragraph 20) and 2025-09-09 (paragraph 39). They are read from
  the USDT contract at the last block of each day.
- **F14: Tether "burned" the 5,680,000 USDT** (paragraph 39). The on-chain
  consequence checked is a balance of 0 now. The complaint gives no burn date
  (after 2025-09-22, before 2026-01-13). It doesn't say "frozen".
- **F15: the reissue of 5,680,000 USDT** to the USMS address on 2026-01-13
  (paragraph 39).
- **F16 and F17 can't be checked:** 5,000,000 USDT deposited "in the span of
  one week" has no dates, and a US-dollar value has no date.

Left out, because they are tracing attributions, not on-chain facts: the
amounts the complaint traces to particular victims and to Huione Pay
(paragraphs 20 and 37).

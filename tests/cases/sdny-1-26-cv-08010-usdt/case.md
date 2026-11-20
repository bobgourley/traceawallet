# S.D.N.Y. 1:26-cv-08010: USDT in ten Tron addresses

- **Court:** United States District Court for the Southern District of New York
- **Docket:** 1:26-cv-08010 (printed "26 Civ. 8010")
- **Filed:** 2026-09-14
- **Caption:** United States of America v. All USD Tether Held in the Following
  Cryptocurrency Addresses (the ten addresses below)
- **Document:** https://www.justice.gov/usao-sdny/media/1461216/dl (25 pages;
  sha256 in `expected.json`)

## Addresses

The caption and paragraph 1 print all ten "Target Addresses" in full. Each
passes the Tron base58 checksum. They are checked on Tron with Tether's Tron
USDT contract (`TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t`), since the complaint
names USDT and the addresses are Tron-format.

One more address, "TWzSi", is printed only in part. It is never completed.
F14 matches a sender by those five characters and nothing more.

## Facts

There are 17 facts in `expected.json`, each with its paragraph and quote.

- **F1 to F10: the paragraph 52 balances.** The complaint says the addresses
  "currently contain approximately" these amounts. TronGrid can't read a
  balance at a past block, so the checker rebuilds each balance from the
  address's complete USDT transfer history, as of the end of the filing date
  (2026-09-14 UTC).
- **F11: the total**, 61,192,367.59 USDT, is the sum of F1 to F10.
- **F12: the freeze dates** in the paragraph 52 table need the stablecoin
  freeze loader, which isn't built yet.
- **F13 and F14: paragraph 51(b).** The TTc6T address was activated on
  2025-06-14 and that day received about 2 million USDT from an address
  beginning "TWzSi".
- **F15 and F16: paragraph 51(c).** Seven addresses were activated on
  2025-05-14 "by the same address". On Tron an account is created by its first
  incoming TRX transfer, so that is what the checker reads. A token transfer
  can arrive earlier without creating the account (TVmYF received 5 USDT 18
  seconds before its first TRX).
- **F17** names only "TWzSi" and unprinted pass-through addresses, so it can't
  be checked.

## Result (2026-11-20, TronGrid)

All 13 checkable facts were confirmed.

- F1 to F10: every balance matches to the cent. No USDT moved in or out of
  these addresses after July 2025, so the balance on the filing date is also
  the balance today.
- F13 and F15: the activation dates match. TronGrid's account creation times
  give the same dates.
- F14: 1,999,000 USDT arrived at 10:14 UTC on 2025-06-14 from an address
  beginning "TWzSi" (tx `994f3cba…8057`).
- F16: all seven activating transfers came from one address,
  `TDRqES2NRsSWimMFsJNkCQj2DdejBcBY9w`. The complaint doesn't print it; it is
  what the chain shows.

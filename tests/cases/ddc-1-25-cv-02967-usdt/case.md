# D.D.C. 1:25-cv-02967: about 868,247 USDT on Tron

- **Court:** United States District Court for the District of Columbia
- **Docket:** 1:25-cv-02967 (printed "25-cv-2967")
- **Filed:** 2025-09-02
- **Caption:** United States of America v. Approximately 868,247 USDT
- **Document:** https://www.justice.gov/usao-dc/media/1412676/dl (25 pages;
  sha256 in `expected.json`)

## Addresses

The two "Subject Virtual Currency Addresses" are printed in full in paragraph
16: KNWAN (`TC66d59uus8AWhYDvAgWM55T8NRa7KNWAN`) and 7McvZ
(`TH4qXPSp3S5g3HnDr1pgnEq4XAoJh7McvZ`). Both pass the Tron checksum. The
complaint calls the token "USDT_TRX", so it is checked with Tether's Tron
contract.

Eight Bitcoin addresses are also printed in full (paragraphs 43 to 52 and the
table on page 22). They are recorded with their paragraphs. No Bitcoin fact is
checked yet.

## Facts

There are 12 facts in `expected.json`.

- **F1 and F2: what each address "held"** (paragraph 16). The checker
  confirms if the transfer history brings the balance to exactly that amount.
- **F3: the total**, 868,247.366804 USDT, is F1 plus F2 exactly.
- **F4: 359,217.08 USDT received on 2024-10-29** through SwapSpace
  (paragraphs 45 and 46). The complaint gives a total, not the transfers. The
  checker looks for consecutive receipts on that date that add up to it.
- **F5: that is "approximately 73.98%"** of all USDT KNWAN received.
- **F6 and F7: two withdrawals of 10,000 USDT** from KNWAN, on or about
  2024-10-30 and 2024-11-02.
- **F9 and F10: paragraph 54's split** of 11 SwapSpace conversions: 334,491.98
  USDT to 7McvZ and 87,137.04 USDT to KNWAN. These are checked the same way as
  F4.
- **Not checkable yet:** the freeze and blacklisting dates (F8, F12) need the
  freeze loader. The 68.57% share for 7McvZ (F11) has no stated numerator.

Left out because they are tracing attributions, not on-chain facts: how much
of each balance is traced to a particular victim (paragraphs 46 and 53).

## Result (2026-11-20, TronGrid)

- **Confirmed:**
  - F1: KNWAN's transfers end at 465,514.638375, its last receipt on
    2024-11-02.
  - F2: 7McvZ's end at 402,732.728429 on 2024-11-06.
  - F4: six consecutive receipts on 2024-10-29, from 08:14 to 13:00 UTC, total
    359,217.081682.
  - F5: that is 73.9869% of the 485,514.638375 USDT KNWAN received, which is
    within the 1% allowed for "approximately". To two places it rounds to
    73.99%.
  - F6 and F7: both 10,000 USDT withdrawals.
  - F10: KNWAN's first two receipts on 2024-10-29 total 87,137.044086.
- **Not found:** F9. No run of consecutive receipts among 7McvZ's 70 adds up
  to 334,491.98. The complaint doesn't identify the 11 transfers, so a set
  that isn't consecutive is not ruled out.

Both addresses hold 0 USDT today. Paragraph 17 says the FBI "facilitated the
transfer of the Defendant Property from Tether". No transfer out of either
address appears in the USDT history after November 2024.

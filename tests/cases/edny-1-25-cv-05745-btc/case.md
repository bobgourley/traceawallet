# E.D.N.Y. 1:25-cv-05745: approximately 127,271 BTC

- **Court:** United States District Court for the Eastern District of New York
- **Docket:** 1:25-cv-05745
- **Filed:** 2025-10-14
- **Caption:** United States of America v. Approximately 127,271 Bitcoin ("BTC")
  Previously Stored at the Virtual Currency Addresses Listed in Attachment A,
  and All Proceeds Traceable Thereto
- **Document:** https://www.justice.gov/usao-edny/media/1416266/dl (68 pages;
  Attachment A is page 42; sha256 in `expected.json`)

## Addresses

Attachment A lists 25 Bitcoin addresses (all P2SH, `3…`), each printed in full,
with no amounts. Paragraph 44's table lists the same 25 addresses in the same
order, with amounts. Footnote 7 says the table shows where the BTC was
"stored as of December 2020".

The complaint also prints, in full:

- the "364 Wallet" (paragraphs 52 and 55)
- the "3QL", "32D" and "3DE" wallets (paragraph 55)
- four addresses that received BTC from Exchange-1 (paragraph 56)

All 33 pass the base58 checksum.

## Which addresses are checked

Rows 15, 21 and 23 were chosen for modest transaction counts: 15, 16 and 15
transactions when listed on 2026-11-19. Several other rows have hundreds or
thousands, too many for a first test.

| Row | Address | Paragraph 44 amount |
|---|---|---|
| 15 | `34MFtk9iMxYcUPZWXHfiGfqz4o7X3kpJbV` | 0.5084661 BTC |
| 21 | `39B6oSa58qNpFMGpuowtRHAYp3fM4ghXRq` | 212.5930613 BTC |
| 23 | `3BA3PEF4BMoy9y3kdMRUdMhL8Gp24vikhF` | 2.16989588 BTC |

## Facts

- **F1 to F3: each address's paragraph 44 amount "as of December 2020".** The
  checker rebuilds the balance from our rows and confirms if the address held
  that amount at some point in December 2020. It reports the balance at the
  start and end of the month.
- **F4 to F6: paragraph 58, the BTC "was subsequently transferred in its
  entirety".** So each address holds 0 now.
- **F7 to F9: the paragraph 47 cluster totals** for the clusters these
  addresses form on their own (6, 9 and 11). They can't be checked: no date
  is given. Cluster 9's 212.50 BTC also differs from paragraph 44's
  212.5930613.

The complaint's dated transfers (paragraphs 50, 55 and 56) involve row 1, row 6
and the cluster totals, not these three addresses. Row 6 and row 1 have 1,566
and 1,643 transactions, which is why they aren't checked here.

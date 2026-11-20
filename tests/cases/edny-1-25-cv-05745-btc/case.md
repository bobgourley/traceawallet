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

## Discrepancies

We tried to work out what the paragraph 44 amounts measure for rows 15, 21 and
23. No on-chain measure we tried gives them. The complaint doesn't say how its
figures were computed. Both numbers are recorded in `expected.json`
(`discrepancies`, D1 to D3), and the checker reports F1 to F3 as DIFFERS with a
pointer to them.

Read on 2026-11-20 from mempool.space. Blockstream returned the same
transactions, and row 21's balance and count checks pass, so the history we
read is complete.

Each address received its funds in 2019 or early 2020 and then held a steady
balance until 2020-12-28. That day two transactions emptied it. Each had one
input and one output and paid a fee of 0.00075 BTC.

| Measure tried | Row 15 | Row 21 | Row 23 |
|---|---|---|---|
| Paragraph 44 | 0.5084661 | 212.5930613 | 2.16989588 |
| Total ever received | 0.6067160 | 625.1975312 | 2.27436552 |
| Moved out on 2020-12-28, including fees | 0.4989664 | 212.5897816 | 2.16661592 |
| Moved out on 2020-12-28, outputs only | 0.4974664 | 212.5882816 | 2.16511592 |
| Balance, 1 to 28 December 2020 | 0.4989664 | 212.5897816 | 2.16661592 |
| Balance, 29 to 31 December 2020 | 0 | 0 | 0 |

The closest figure is the balance held through December until the transfers
out, the same number as the amount moved out including fees. Paragraph 44 is
higher than it by 0.0094997 BTC for row 15, 0.0032797 BTC for row 21 and
0.00327996 BTC for row 23. Rows 21 and 23 differ by almost exactly the same
amount, and row 15 by about three times that. We have no explanation for the
pattern and record it as found.

Also tried and not matching: the largest balance each address ever held, the
largest single output, and the fees counted in other ways (one fee, both fees,
or each transaction's share).

## OFAC SDN list

All 33 addresses the complaint prints in full were checked against OFAC's SDN
list published 2026-11-19 (SDN.XML, sha256 in `expected.json`), using the
loader in `taw/adapters/labels/ofac.py`. None of them is on it. That is a
statement about the list on that date only. It says nothing about who
controls the addresses.

# Ground-truth cases

On-chain facts stated in US forfeiture complaints, used to check our adapters
against an independent account of what happened. Each usable case has:

- `case.md`: the court, docket, filing date, link, which facts were taken and
  why.
- `expected.json`: every address the complaint prints in full, and each
  checkable on-chain fact. Each fact carries the paragraph number and the
  complaint's own words. A fact we can't check yet records why.

Rules (from the owner, 2026-11-20):

- Record only what the complaint states. No address is completed or looked up
  from another source.
- Complaints state allegations. These files hold on-chain facts only: amounts,
  dates, balances, who sent what to whom. Nothing here asserts that any
  address belongs to a scam. `tests/test_cases_files.py` enforces that, and
  checks every address's checksum.
- `scripts/check_cases.py` runs the facts through our adapters, live, and
  reports each as confirmed, contradicted, not found, or not checkable yet.

Every quote was matched against the text extracted from the complaint PDF
(sha256 in `expected.json`) when the file was written.

| Case | Status |
|---|---|
| `wdpa-2-26-cv-00087-usdt`: W.D. Pa., 5,680,000 USDT, address ending 74bf | Usable: 11 full addresses, a dated chain of 11 transfers |
| `edny-1-25-cv-05745-btc`: E.D.N.Y., about 127,271 BTC | Usable: 25 Attachment A addresses; three checked |
| `ddc-1-25-cv-01907-usdt-gb`: D.D.C., 225,364,961 USDT, victim G.B. | Usable: one transfer between two fully printed addresses |
| N.D. Ohio, 325,060 USDT, 4:25-cv-01780, filed 2025-08-27 | **Unusable**: prints no address in full |
| N.D. Ohio, 679,981.22 USDT, 1:25-cv-01137, filed 2025-06-02 | **Unusable**: prints no address in full |

**Why the two N.D. Ohio cases were not built.** Both complaints print every
address as "beginning/ending X . . . Y". In 4:25-cv-01780 the defendant address
is `TUpj2 . . . LaDkkMB` (also printed `TUpj2C . . . kkMB`). In 1:25-cv-01137
the two defendant addresses are `TGZdF . . . mYHyPDF` and `THr7k . . . q9gskdN`.
Both are scanned PDFs with no text layer. Their stated facts, such as freeze
dates, balances at freeze and dated transfers, would be good tests if a source
printing the full addresses turned up.

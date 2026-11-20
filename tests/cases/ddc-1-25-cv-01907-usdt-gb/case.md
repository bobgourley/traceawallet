# D.D.C. 1:25-cv-01907: approximately 225,364,961 USDT (victim G.B.)

- **Court:** United States District Court for the District of Columbia
- **Docket:** 1:25-cv-01907 (printed "Civil Action No. 25-cv-1907")
- **Filed:** 2025-06-18
- **Caption:** United States of America v. Approximately 225,364,961 USDT
- **Document:**
  https://www.secretservice.gov/sites/default/files/reports/2025-06/Verified-Complaint-25-cv-1907.pdf
  (75 pages, with exhibit graphics; sha256 in `expected.json`). A text-only
  copy is on CourtListener.

## The case study

The complaint names victims by initials. G.B. (paragraphs 196 to 204) is the
only case study where a stated transfer runs between two addresses printed in
full. Address "b9ac", `0x0c0996d0c3af2892a7a8fc621eb7347ec9b6b9ac` (defined in
paragraph 177), "deposited approximately 4,529,900 USDT into USDT Token Group
E" (paragraph 203). Group E is
`0xc76afbf0f69ae9be5d855239c50673252cf3f26b`, defined in paragraph 15.

The other case studies print only partial addresses, except M.S. through
footnote 30. M.S.'s own payment has no amount or date.

## Addresses

The complaint prints 19 addresses in full, all in lower case, so there is no
checksum to confirm the transcription. Each was compared with the page images
when this case was built:

- the seven USDT Token Groups (paragraph 15)
- the two consolidation addresses (paragraph 177)
- the 0x414f deposit address (footnote 30)
- nine addresses in the paragraphs on OKX deposits (178 to 186)

Paragraph 186's `0x029b…` OKX deposit address is never tied to M.C.'s
"0x029b" account in paragraph 95. The two are not linked here.

## Facts

- **F1:** b9ac's transfer of about 4,529,900 USDT into Group E (paragraph
  203). No date is given.
- **F2:** Group E's 2,137,276.136 USDT (paragraph 15). The checker confirms
  that its transfer history reaches that balance.
- **F3:** the balance was held "before the tokens were burned" (paragraph
  224). So Group E holds 0 USDT now.
- **F4 to F6 can't be checked:**
  - the freeze request on or around 2023-11-20 needs the freeze loader;
  - G.B.'s 37,566 USDT went to an address that isn't printed;
  - b9ac's 15,890,000 USDT went to an address printed only as "2c91".

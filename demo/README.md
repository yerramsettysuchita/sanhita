# Test cases

Twelve files for exercising the compliance journey against real SEBI duties.

## What is real here, and what is not

**Real.** Every obligation id, clause number, page, deadline period and
artifact type in these files is read out of the compiled rulebook, which came
from SEBI's Master Circular for Stock Brokers of 17 June 2025. The duties are
genuine and each traces to a clause you can open in the circular.

| Duty | Clause | Page | Cycle |
|---|---|---:|---|
| Review the implementation of the BCP and SOP | 19.5.5.12 | 55 | Monthly |
| Put a mechanism for uploading the required data | 15.9.1 | 33 | Monthly |
| Formulate a cyber security and resilience policy | 62.4 | 165 | Annual |
| Every year identify internal auditors | 15.6.4.1 | 29 | Annual |

**Not real.** The firm names, and whether a firm filed on a given date.

That second one is invented for a reason worth stating. A firm's filing
register is internal: it is not published, and no outsider can know it. Naming
a real, registered broker and putting invented gaps against it would be a false
statement about a regulated company, which is not a thing to do on a
submission. So the regulation is real, the duties are real, the firms are not.

## The ten registers

Each lands on a different part of the engine, so the set can check behaviour
rather than produce one screenshot. Counts are as of 15 August 2026.

| File | Firm | What it demonstrates | Satisfied | Gaps | Unverifiable |
|---|---|---|---:|---:|---:|
| `01-clean-firm.csv` | Ashwini Broking Services | Everything filed on time | 14 | **0** | 26 |
| `02-chronically-late.csv` | Kaveri Securities | Nothing missing, everything late | 0 | **13** | 27 |
| `03-nothing-filed.csv` | Trident Stock Broking | Occasions recorded, nothing filed | 0 | **14** | 26 |
| `04-realistic-mixture.csv` | Meridian Capital Services | What a stretched team looks like | 9 | **5** | 26 |
| `05-records-stopped-in-march.csv` | Nandini Financial Services | Diligent, then silence | 16 | **8** | 28 |
| `06-newly-registered.csv` | Sarvottam Broking | One month old, almost all unknown | 2 | **0** | 28 |
| `07-single-duty-tracked.csv` | Chandra Equities | A year on one duty, nothing else | 11 | **1** | 29 |
| `08-full-year-register.csv` | Vaishnavi Securities | A full year across four duties | 20 | **6** | 26 |
| `09-annual-done-monthly-ignored.csv` | Harita Capital Markets | Policies exist, discipline does not | 2 | **2** | 27 |
| `10-rows-that-must-be-refused.csv` | Ganga Broking | Three bad rows the importer rejects | 3 | 0 | 29 |

### Which to reach for

**Recording the demonstration:** `04-realistic-mixture.csv`. Nine satisfied,
five confirmed gaps, twenty six not verifiable. Enough going on to be
interesting, few enough findings to follow on camera.

**Showing the product is not just an accuser:** `01-clean-firm.csv`. Zero gaps.
A tool that can only find fault has not been tested.

**Showing breach against unknown:** `06-newly-registered.csv`. Two satisfied,
**zero** gaps, twenty eight unverifiable. A month old firm is not
non-compliant; it simply has not been asked for most of this yet. This is the
distinction most compliance tools get wrong.

**Showing evidence health:** `05-records-stopped-in-march.csv`. The firm filed
diligently and then stopped. The health screen exists to notice that before a
quarter end does.

**Showing the importer refuses:** `10-rows-that-must-be-refused.csv`. Three
rows, three refusals, each with a reason:

- an artifact filed before the event that required it
- an obligation id that is not in the rulebook
- no entity named

## The two documents

A CSV is what a back office exports. A PDF is what a compliance team already
has: prose and tables laid out for a human, with no column called
`obligation_id` anywhere in it.

Sanhita does not match document text against clause wording. Two passages being
similar is not evidence that a duty was discharged, and inferring one from the
other is how a tool ends up telling a firm it is compliant when it is not. So
it finds lines that look like filings, records the page each came from, and
asks a person which duty each answers.

| File | What comes back |
|---|---|
| `11-filing-register-with-rule-ids.pdf` | **7 candidates, all STATED.** The register names Sanhita's own rule ids, so each line is matched and offered for confirmation |
| `12-compliance-summary-no-rule-ids.pdf` | **8 candidates: 3 PROBABLE, 5 UNRESOLVED.** A human readable statement naming no rule ids. Dates and references are found; which duty each answers is left to a person |

The second is the honest case, and the more common one. Most of what a firm
already has looks like that.

## Using one

1. Open the site and press **Check my company's compliance**
2. Press **Start my company's compliance** to reach step one
3. Enter the firm name from the table above, intermediary type **stock broker**
4. Declare the **Stock Brokers Master Circular**
5. Upload the file
6. Create an account when asked, then **Run assessment**

An account is needed to run the assessment, and not before. Recording a
compliance position puts a named person against the act, which is the point of
the audit trail.

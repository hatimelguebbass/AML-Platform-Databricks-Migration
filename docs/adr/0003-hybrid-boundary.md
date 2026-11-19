# ADR 0003: The hybrid boundary: what crosses, how it is checked, how it travels

- Status: accepted

## Context

The bank's customers are Moroccan residents; law 09-08 governs their personal data, and the CNDP
authorizes transfers abroad case by case. ADR 0001 keeps on premises everything that needs a direct
identifier: the core banking system, the identity table, customer matching and sanctions screening
(they compare names). The cloud rebuilds the rest from exports.

Step 1 classified every source column (`migration/classification.yaml`): `direct` (identifies a
person on its own), `indirect` (identifies only combined with other data) or `none`. Eight objects
are exported (`migration/waves.yaml`); the cloud models rebuilt from them never read a direct
identifier. This ADR decides what of those eight objects crosses, the controls on the way out, and
the transport.

## Decision

**A reviewed column policy.** `migration/export.yaml` states, for every exported object, its
selection, its row order and a treatment for each `indirect` column; there is no default.
`docs/assessment/boundary.md` renders it column by column for the data protection officer. CI fails
when the policy, the classification, the waves and the inventory disagree: an exported object
without a policy, a direct identifier listed, an indirect one without a treatment.

| Treatment | Used for | Why |
|---|---|---|
| `keep` | Internal keys (customer, account, card, party ids), demographic attributes the rules and the risk dimensions use | A surrogate key means nothing outside the bank; the attributes are the analytical content |
| `pseudonymize` | Account numbers (also the originator and beneficiary accounts of transfers) and card tokens | They are known to other banks and to the card processor. HMAC-SHA256 under a key that never leaves the premises: not reversible, and not recomputable from a guessed account number as a plain hash would be. One namespace per kind of identifier, so transfers still join to accounts exactly |
| `year` | Birth date | The models use the birth year only |
| `drop` | RIB, last four card digits, postal code | No cloud model reads them |

Direct identifiers are never read: the export selects only the columns that cross, so names, ID
numbers and contact details never reach the export process from the tables it ships.

**A PII gate on the values.** A column list cannot see a phone number typed into a free-text field.
After the treatments, every string value of the delivery is scanned for e-mail addresses, Moroccan
phone numbers, RIB and IBAN, CIN and passport numbers, payment card numbers (scheme prefix and Luhn
checksum) and the full names of the bank's customers, read from the identity table at the same
source commit. One match blocks the whole delivery; the report shows masked examples only. A
reviewed exemption names one column and one detector with its reason; exempted matches are
counted in the manifest, not hidden.

A column added to a source table also stops the export, whatever its name: the live schema must
equal the inventoried one, so a new column is classified and given a treatment in a reviewed
change before anything ships it.

**Deliveries.** One delivery per business date: every exported object read at one commit of the
source lakehouse (Nessie versions the whole catalog, so the tables are a consistent cut even if the
daily pipeline publishes meanwhile), one Parquet file per table, and `_manifest.json` with the
source commit, the policy digest, the key identifier, the gate summary, the schema and treatment of
every column, the files with their SHA-256, and control totals per column (rows, nulls, sums,
ranges, distinct counts) that the cloud recomputes after loading (step 3). The delivery id is the
date and a digest of the content: exporting the same data again yields the same id; a corrected
export yields a new id beside the first instead of overwriting files.

Rows are selected per object: by business date for the daily feeds, by change-capture run for the
core banking change logs (a run belongs to the date the daily pipeline gave it), whole for the two
small on-premises results (party membership, sanctions hits). The initial core banking snapshot
has no business date and goes with the first delivery (`first_business_date`); an undated capture
made after the daily runs began stops the export rather than vanish.

**Export engine: PyIceberg and Arrow**, in a Python process next to the source, rather than a Spark
job: one day is under 300,000 rows, the gate and the pseudonyms are vectorized Arrow computations
testable without a JVM, and Arrow writes the Parquet the cloud reads.

**Transport: the Unity Catalog Files API into a volume** (`landing.deliveries`), managed on Free
Edition and external on ADLS Gen2 in Azure. The sender needs WRITE VOLUME on one volume and no
storage key; every write is in the Unity Catalog audit log; authentication is the workspace's
(token in `dev`, service principal in `prod`). Data files go first, each checked by size, the
manifest last and never overwritten: a delivery without its manifest is incomplete and ignored, a
rerun completes it or does nothing.

## Alternatives considered

| Option | Why not |
|---|---|
| Ship everything, protect it in the cloud with column masks | The data would have left the country; masks control who reads, not where data is |
| Classification only, no value scan | Misses identifiers in columns allowed to cross, the most likely leak after a source change |
| Plain SHA-256 of account numbers | Account numbers are short and structured: hashing every candidate reverses them |
| Tokenization service with a vault | A service and a database to run for identifiers the cloud only joins on; a keyed hash gives the same joins |
| Format-preserving encryption | Reversible by design; the cloud never needs the clear value back |
| Spark job in the source platform | A JVM and 2 GB per run for a few hundred thousand rows; the gate would need a cluster in its tests |
| Debezium straight to a cloud Kafka | Ships the change events with every column, names included, before any check |
| Azure Data Factory with a self-hosted integration runtime | A Windows service on premises and per-activity billing, for a daily file copy |
| AzCopy or the storage SDK into ADLS | Needs a storage credential on premises and bypasses Unity Catalog's permissions and audit; only works in `prod` |

## Consequences

- Cloud models read `birth_year` instead of `birth_date`; pseudonymized columns keep their names, so
  the ported joins are unchanged (step 4).
- The parity checks (step 7) compare pseudonymized columns by pseudonymizing the source side with
  the same key, on premises (`lakemig.export.pseudonym.pseudonym`).
- Rotating the key changes every pseudonym: the cloud must refuse a delivery whose key identifier
  differs from the previous one (step 3) until a full reload.
- The key is a file on the export host (`.secrets/pseudonymization.key`, mode 600, never in the
  repository), backed up on premises; losing it breaks the joins of new deliveries with old ones.
- Step 3 accepts a delivery only with its manifest, checks every file's SHA-256 and the control
  totals, and keeps the latest delivery of a date when a correction arrives.

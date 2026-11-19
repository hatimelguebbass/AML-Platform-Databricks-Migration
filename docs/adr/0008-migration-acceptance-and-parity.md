# ADR 0008: Migration acceptance by a parallel run and a hashed, column-level parity check

- Status: accepted

## Context

The port (ADR 0005, 0006) rebuilds silver and gold on Databricks from the same deliveries the
source's tables come from. Before the cutover (step 9) the migration needs a criterion that says
"the target computes what the source computes", measured, repeatable and written down before the
numbers are known.

Three constraints shape the comparison:

- **The two sides cannot meet.** The source holds direct identifiers that never cross (ADR 0003);
  the target holds pseudonyms whose key never leaves the premises. Copying the target's 8.1 million
  silver and gold rows on premises would turn a check into a second transfer, through a SQL
  warehouse priced by the minute.
- **The tables do not hold the same values for the same facts.** Account numbers and card tokens
  are pseudonymized by the export; the source's `listed_name` stays on premises; each platform
  writes its own lineage (`_pipeline_run_id`, `_loaded_at`, `_delivery_id`).
- **Two engines print values differently.** Trino and Databricks agree on a decimal, not on how a
  double, a time zone or an array is rendered.

Options considered:

1. **Row counts and sums per table.** Cheap and common, and blind to a swapped amount, a wrong
   account on a transfer or an alert raised on the wrong date with the same totals.
2. **Copy one side to the other and `EXCEPT ALL`** (the measure of ADR 0005, done inside one
   engine). Exact, but here it means moving either personal data to the cloud or the whole target
   on premises.
3. **Data-diff by hashing**, as tools such as `data-diff` and `datacompy` do: each side hashes its
   rows, only digests travel, differing segments are read row by row. Exact on every column, and
   what moves is proportional to the differences, not to the tables.

## Decision

**Option 3, implemented in `lakemig parity` (`src/lakemig/parity/`) and run on premises next to
the source**, against the target's published tables through the SQL warehouse.

- **Canonical text.** Each value is rendered to one text per target type, by Arrow on premises and
  by Databricks SQL in the cloud (`canonical.py`, both renderings side by side). Instants compare
  as microseconds since the epoch, local timestamps as printed to the microsecond, decimals at the
  target's scale, doubles rounded to a stated number of decimals, arrays element by element.
  `tests/integration/test_parity_canonical.py` runs both renderings on the same edge cases on the
  warehouse in CI, so a change of engine version that breaks the agreement fails a build, not a
  parity run.
- **Source values are brought to the target's form, never the reverse**: the columns the export
  pseudonymizes are pseudonymized with the same key before hashing; columns kept on premises are
  not even read.
- **Fingerprints.** For each table, and for each business date of a dated table, both sides compute
  a column profile (nulls, distinct values, sums of numbers, minimum and maximum of dates and
  times) and spread their rows over 256 buckets by the SHA-256 of the key; a bucket's digest is
  its row count and the sum of the first 60 bits of its rows' SHA-256. Only the buckets whose
  digests differ are read row by row, at most 16 per slice, and each differing row is judged column
  by column: missing, unexpected, different beyond its tolerance, or within it.
- **Every column is accounted for.** `migration/parity.yaml` lists the tables, keys and slices;
  a column of either side that is neither compared, nor lineage, nor kept on premises with a reason
  fails the run. `lakemig assess` checks that the spec covers exactly the tables the
  transformations publish and that `docs/assessment/parity.md` renders the last report.
- **The target is read where readers read it**: published tables only, never the `_staging`
  schemas, with the publication run of each date recorded in the report.
- A new account group, **`aml-migration`**, holds the operators who run the on-premises tooling: it
  writes the landing volume, reads silver, gold and the publication log, and is let through the
  pseudonym masks. It is the source's companion and goes with it at decommissioning (step 10).

### Acceptance criteria

The migration is accepted on a business date range when, for every compared table and slice:

1. the row counts are equal and no key repeats on either side;
2. every column profile is equal;
3. every row of the source is in the target and none other, and every compared column is equal,
   except doubles listed with an absolute tolerance;
4. **the AML alerts, their evidence, the rule catalogue and the sanctions hits
   (`gold.aml_rule`, `gold.aml_alert`, `gold.aml_alert_transfer`, `gold.sanctions_hit`) differ in
   nothing**: the spec refuses a tolerance on them;
5. a difference found is either fixed in the port and the run repeated, or explained in the spec
   and in this ADR before the cutover.

The only tolerance granted: `silver.fx_rate.rate_to_reporting` and `relative_spread`, 1e-12, since
both engines compute them in floating point.

## Consequences

- The comparison costs two aggregate queries per table on the warehouse plus one per slice with
  differences, and one read of the source; nothing personal leaves the premises.
- The bucket digest is a sum, not a cryptographic hash of the bucket: two different sets of rows
  with equal counts and equal sums of 60-bit prefixes would pass. With SHA-256 prefixes this needs
  a deliberate collision; the column profiles are a second, independent check.
- Doubles without a tolerance compare at 12 decimals; a difference below that is not seen. The
  only doubles of the zero-difference tables (`sanctions_hit.name_similarity`) cross as Parquet
  doubles and are not recomputed.
- At most 16 differing buckets are read per slice: a wholesale difference reports a sample of its
  rows and fails all the same, since an uninspected difference counts as one.
- The report (`migration/parity.json`) is committed as evidence; rerunning it after a source or
  port change is part of the change.
- First measurement, business dates 2022-09-01 to 2022-09-18: the 12 tables, 8,132,982 rows on
  each side, are equal, in 4 min 48 s end to end ([report](../assessment/parity.md)).

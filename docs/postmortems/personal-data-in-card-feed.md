# Postmortem: customer names and phone numbers in the card feed's merchant name (drill)

- Duration: 21:36 to 21:40 UTC
- Kind: planned drill on the source's staging branch; no delivery left the premises
- Severity if real: high (direct identifiers of customers in the cloud, law 09-08)
- Status: closed; one detection gap found and fixed in the same change

## Summary

The drill staged an upstream change of the card processor: a peer-to-peer product that writes the
recipient into the merchant descriptor, `P2P <FIRST NAME> <LAST NAME> <phone>`, on 40 of the
100,000 card transactions of 2022-09-18. The export gate blocked the date on the phone numbers in
4 seconds. A second variant without the phone number, names only, **was not blocked**: the gate
compared a whole value with the customers' full names, and `P2P SALMA BENJELLOUN` is not a full
name. The export of that variant was deleted on the export host before any upload. The gate now
looks for a full name as whole consecutive words of a value; the same variant is blocked, and a
dry run of the 18 business dates in service (10.67 million rows) finds no false positive.

## Timeline (UTC)

| Time | Event |
|---|---|
| 21:36:10 | 40 rows of `bronze.card_transaction` (2022-09-18) on the staging branch `etl` get `merchant_name = 'P2P ' + full name + phone` of 40 customers of `restricted.customer_identity` |
| 21:36:26 | `make export FROM=2022-09-18 BRANCH=etl` |
| 21:36:30 | Blocked, exit code 2: `bronze.card_transaction.merchant_name: phone in 40 rows`; masked report in `exports/_blocked/2022-09-18.json`; nothing written for the date |
| 21:36:47 | Variant: phone numbers removed, names kept |
| 21:36:51 | Export **passes**: delivery `2022-09-18-e76815a68c30` written to `exports/`, with 40 customer names |
| 21:37 | Delivery deleted on the export host; not uploaded (`make deliver` was not run) |
| 21:37:31 | Gate changed to find names inside values; same variant blocked: `person_name in 40 rows` |
| 21:38 | Dry run of 2022-09-01..18 from `main`: 18 deliveries, 10,675,262 rows, no finding, 2 min 14 s |
| 21:39:59 | `etl` aligned with `main` again (`make bootstrap` of the source) |

## Detection

The gate runs on every export before anything is written (ADR 0003). The phone detector caught
the first variant. Nothing would have caught the second one before step 3's ingestion, which
checks files and totals, not content: the names would have reached `bronze` and `silver`.

## Root cause

The name detector was designed against false positives: a single first or last name is a common
word (a street, a merchant, a city), so only a value equal to a full name matched. Free-text
columns do not hold a name alone: a descriptor, a memo or a reference puts words around it. The
detector's unit test list had `SALMA BENJELLOUN` and `Benjelloun, Salma`, never a name inside a
longer value.

## What went well

- The block was fast and complete: no partial delivery, a masked report that says where and what
  without showing a value, and an exit code the scheduler stops on.
- The second variant was tried because the first one passed for the wrong reason (the phone), not
  because the gap was known.
- The fix was measured against all real data before being kept: no false positive, export 15 %
  slower (2 min 14 s against 1 min 57 s at step 2).

## What went badly

- A full name inside a value would have crossed the boundary on the first real P2P transaction.
- The gate has no test that writes realistic free text around an identifier; every case was an
  identifier alone.

## Action items

| Action | Status |
|---|---|
| Match a known full name as whole consecutive words of a value (`NameIndex._holds_name`), with the drill's descriptor as a unit test | done in this change |
| Keep `merchant_name` crossing: card processors' descriptors are needed by the alert evidence; any exemption would be a reviewed change of `migration/export.yaml` | decided |
| Ask the card processor to announce descriptor format changes as a contract change of the feed (source project) | open, outside this repository |
| Re-run the dry run of every date in service whenever a detector changes, before merging | written in `docs/runbook.md` |

## How the drill was staged

On the source, staging branch only (`lakehouse_etl`), never on `main`:

```sql
MERGE INTO lakehouse_etl.bronze.card_transaction t
USING (
  WITH txn AS (SELECT txn_ref, row_number() OVER (ORDER BY txn_ref) AS n
               FROM lakehouse_etl.bronze.card_transaction
               WHERE _business_date = DATE '2022-09-18' ORDER BY txn_ref LIMIT 40),
       person AS (SELECT first_name, last_name, phone, row_number() OVER (ORDER BY customer_id) AS n
                  FROM lakehouse.restricted.customer_identity
                  WHERE first_name IS NOT NULL AND phone IS NOT NULL)
  SELECT txn.txn_ref, 'P2P ' || upper(person.first_name || ' ' || person.last_name) || ' ' || person.phone AS descriptor
  FROM txn JOIN person ON person.n = txn.n * 7
) s
ON t.txn_ref = s.txn_ref AND t._business_date = DATE '2022-09-18'
WHEN MATCHED THEN UPDATE SET merchant_name = s.descriptor
```

then `make export FROM=2022-09-18 BRANCH=etl`, and `make bootstrap` in the source to align `etl`
with `main` again.

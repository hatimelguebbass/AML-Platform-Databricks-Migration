# Operations runbook

Day-to-day procedures of the migrated platform. The cutover itself: [cutover.md](cutover.md).
Commands run on premises, next to the source, with `TARGET=prod` and the operator's `az login`.

## Daily run

The on-premises scheduler runs, for business date `D`, once the source has published it:

```bash
make export FROM=D && make deliver TARGET=prod && make daily TARGET=prod DATE=D
```

`make daily` ingests what landed, builds staging, audits and publishes ([ADR 0006](adr/0006-orchestration-and-write-audit-publish.md)).
A failed run publishes nothing; repair it from the failed audit's cause with
`make repair TARGET=prod RUN=<run id>` (from `open_staging`, never from the failed task alone).

## The export gate blocked a date

`make export` exits with 2 and writes a masked report to `exports/_blocked/<date>.json`: which
detector matched, in which table and column, how many values; never the values. Nothing was
written to `exports/` for that date and nothing was delivered.

1. Do not exempt the column to get the date through. Find which upstream change put personal data
   there (the column name and the first matching date usually point at it).
2. If the column must cross and the matches are false positives, add an exemption with its
   reason to `migration/export.yaml` in a reviewed change (`gate.exemptions`).
3. Otherwise get the producer to stop and correct the source, re-run the source's pipeline for
   the dates, then export and deliver them. The target simply has no new date until then; the
   report keeps showing the last published one.

A change to a detector is merged only after a dry run of every business date in service finds
no false positive: `uv run lakemig export --from <first> --until <last> --out <scratch directory>`
(nothing is delivered from there).

Drill and postmortem: [postmortems/personal-data-in-card-feed.md](postmortems/personal-data-in-card-feed.md).

## Pseudonymization key

The key (`.secrets/pseudonymization.key`, `LAKEMIG_PSEUDONYMIZATION_KEY` for another path) turns
account numbers and card tokens into the pseudonyms every cloud table joins on. It never leaves
the premises. Without it, no date can be delivered again: the ingestion refuses deliveries under
another key than the one it accepted.

- Back it up when it is created, encrypted, to the bank's on-premises secret store, under the
  migration's change ticket. A second copy is not optional: the export host is not a vault.
- `make cutover PHASE=preflight` checks that the key on the export host is the one the target
  accepted (`key_custody`).
- Show the id of the key in place: `uv run lakemig key --id`.

### Rotating the key

When the key is lost, compromised, or due for rotation. The target's pseudonymized columns change
value for every date; nothing else does.

```bash
uv run lakemig key                          # new key; back it up before anything else
make export FROM=<first date> UNTIL=<last date>
make deliver TARGET=prod
make rekey TARGET=prod                      # every accepted date, under the new key, all or nothing
make daily TARGET=prod                      # every date rebuilt from the re-keyed bronze
make parity TARGET=prod FROM=<first date> UNTIL=<last date>
```

`make rekey` refuses before writing anything when a date it holds was not delivered again, or was
delivered under another key, or when any delivery fails its checks: a bronze half under each key
would split every account across dates. Afterwards, `landing.delivery_log` shows the new key on
every accepted delivery; the deliveries of the old key stay in the landing volume and the log.
The Power BI report imports no pseudonymized column, so it needs only a refresh.

Postmortem of the rotation of step 9: [postmortems/pseudonymization-key-lost.md](postmortems/pseudonymization-key-lost.md).

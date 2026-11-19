# Postmortem: the pseudonymization key accepted by prod was lost with its export host

- Duration: lost after step 7, found at the cutover's preflight (day 2, 01:08 UTC), resolved 02:27 UTC
- Kind: real incident of the migration's own operations; no data exposed, nothing published wrong
- Severity: high for the migration (no date could be delivered to prod again), none for the report
- Status: closed by a key rotation inside the cutover's final sync

## Summary

Prod's bronze held pseudonyms of key `ef299b2a1e13b020`, created on the export host of step 7.
That host was an ephemeral workspace; it was reclaimed after the step with the key in it, and the
key had never been copied anywhere else. The host rebuilt for step 9 had a new key,
`b79a2e8dafdb40c6`. From then on, every delivery to prod was refused by the ingestion, by design
(ADR 0004): a pseudonym under one key never joins a pseudonym under another.

Nothing was wrong in the cloud: the target kept publishing the 18 dates of step 7, consistent
with each other. What was lost was the ability to deliver again. Without a way to rotate, the only
way out would have been to drop prod's bronze and rebuild it from nothing, losing the delivery log
that proves what was accepted and when.

The cutover's final sync became the rotation: the 18 dates were exported from the freeze tag under
the new key, delivered, and ingested by `make rekey` in one all-or-nothing run (459 s), then
rebuilt (745 s). Parity against the frozen source: 12 tables equal, 8,132,982 rows.

## Timeline (UTC; day 1 is the day of step 7)

| Time | Event |
|---|---|
| Day 1, 14:39 to 14:44 | Step 7: 18 deliveries accepted under key `ef299b2a1e13b020` |
| Day 1, after step 7 | The step's export host is reclaimed with `.secrets/pseudonymization.key`; no copy exists |
| Day 1, 21:35 | Step 9's export host is rebuilt; `lakemig key` creates `b79a2e8dafdb40c6` |
| Day 2, 01:08:20 | `make cutover PHASE=preflight` fails; `key_custody` cannot even read the delivery log (the operators' group had no `SELECT` on it, fixed in this change) |
| 01:09:57 | Freeze: tag `cutover-prod` at the source's `main@39d4e47681fa` |
| 01:10 to 01:15 | Export of the 18 dates from the tag under the new key (2 min 18 s), delivery to prod (2 min 19 s) |
| 01:16:18 to 01:16:56 | `make ingest` refuses all 18: `pseudonymization key b79a2e8dafdb40c6 is not ef299b2a1e13b020, the key of the accepted deliveries` |
| 01:42:20 | First `make rekey` refuses before writing anything: the 18 deliveries were already decided (refused at 01:16), so it found nothing to rotate to |
| 01:43 to 02:02 | Fix: a rotation decides again the new key's deliveries an ordinary run refused; deployed to prod |
| 02:02:45 | `make rekey`: 18 dates re-ingested under the new key in one run, 459 s |
| 02:10:43 | `make daily`: every date rebuilt from the re-keyed bronze, 745 s |
| 02:23:22 | Parity against the tag: 12 tables equal on 18 dates, 4 min 14 s |
| 02:27:47 | Preflight passes, `key_custody` included; switch at 02:27:58 |

## Detection

Late. The key was gone hours before anyone needed it, and the first signal was a refused delivery.
In daily operations that is the morning after, with the reporting deadline running. The preflight's
`key_custody` check would have caught it the day before the cutover, but it failed for another
reason first: the operators could not read the delivery log it checks.

## Root cause

The key lived in one place, the working directory of the export host, and that host was treated as
disposable. Nothing in the process said otherwise: `lakemig key` wrote the key and stopped, and no
runbook step asked for a backup. Every design decision around the key (it never leaves the
premises, the ingestion refuses another one) protected its secrecy; nothing protected its
existence.

The rotation path had a gap of its own: deliveries of the new key, refused by a regular ingestion
before the rotation, counted as decided, so the rotation found nothing to do. That is the order in
which an operator who does not yet know the key changed meets the problem.

## What went well

- The refusal worked as designed: no bronze half under each key, no wrong join, no wrong alert.
- The rotation is all or nothing: the first attempt refused before writing a row, so retrying it
  after the fix was safe.
- Doing the rotation inside the final sync cost one export and one rebuild, which the cutover
  runs anyway.

## What went badly

- The key had no backup, on a host known to be ephemeral.
- The custody check, written for this exact case, could not run with the operators' rights.
- Two deploys to prod were needed during the cutover window (the log grant, the rotation fix).

## Action items

| Action | Status |
|---|---|
| `make rekey` / `--rekey`: rotate every accepted date in one run, refuse before writing if any date is missing or under another key | done in this change |
| A rotation decides again the new key's deliveries that an ordinary run refused, with a unit test | done in this change |
| `key_custody` in the cutover's preflight and switch; `aml-migration` reads `landing.delivery_log` | done in this change |
| Backup of the key, encrypted, to the on-premises secret store when it is created; `lakemig key --id` to compare in place | written in `docs/runbook.md` |
| Run `key_custody` in the daily run before the export, not only at the cutover | open |

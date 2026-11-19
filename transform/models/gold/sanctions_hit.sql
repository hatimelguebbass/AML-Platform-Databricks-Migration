{#- Screening compares listed names with customer names, so it runs on premises (ADR 0003):
    the cloud takes the delivered snapshot of the hits, without either name. -#}

with snapshot as (
    {{ latest_snapshot(source('bronze', 'sanctions_hit')) }}
)

select
    hit_id,
    customer_id,
    party_id,
    list_name,
    entry_id,
    entry_type,
    name_type,
    match_type,
    name_similarity,
    _delivery_id,
    {{ lineage_columns() }}
from snapshot

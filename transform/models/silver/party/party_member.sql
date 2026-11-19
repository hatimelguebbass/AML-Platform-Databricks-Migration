{#- Parties are resolved on premises, where the names and identity documents are (ADR 0003):
    the cloud takes the delivered snapshot as it is. -#}

with snapshot as (
    {{ latest_snapshot(source('bronze', 'party_member')) }}
)

select
    customer_id,
    party_id,
    party_size,
    _delivery_id,
    {{ lineage_columns() }}
from snapshot

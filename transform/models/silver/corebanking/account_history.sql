{%- set tracked = [
    'customer_id', 'bank_id', 'account_number', 'account_type', 'currency', 'status', 'opened_on', 'closed_on',
] -%}

with events as (
    {{ change_events(source('bronze', 'corebanking_account')) }}
),

projected as (
    select
        account_id,
        customer_id,
        {{ normalize_bank_id('bank_id') }} as bank_id,
        account_number,
        account_type,
        currency,
        status,
        opened_on,
        closed_on,
        _op,
        _event_date,
        _kafka_offset,
        _source_committed_at
    from events
),

{{ scd2_versions('projected', 'account_id', tracked) }}

select
    account_id,
    {%- for column in tracked %}
    {{ column }},
    {%- endfor %}
    _effective_date as valid_from,
    _next_version_date as valid_to,
    _next_version_date is null as is_current,
    _source_committed_at as recorded_at,
    {{ lineage_columns() }}
from versions
where not _is_deleted

{%- set tracked = ['account_id', 'card_token', 'scheme', 'product', 'status', 'issued_on', 'expires_on'] -%}

with events as (
    {{ change_events(source('bronze', 'corebanking_card')) }}
),

projected as (
    select
        card_id,
        account_id,
        card_token,
        scheme,
        product,
        status,
        issued_on,
        expires_on,
        _op,
        _event_date,
        _kafka_offset,
        _source_committed_at
    from events
),

{{ scd2_versions('projected', 'card_id', tracked) }}

select
    card_id,
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

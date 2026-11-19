{#- Analytical attributes only: names, identity documents and contact details stay on
    premises, and the birth date crosses as its year (ADR 0003). -#}
{%- set tracked = [
    'customer_type', 'gender', 'birth_year', 'legal_form', 'incorporation_year', 'nationality',
    'residence_country', 'city', 'occupation', 'risk_rating', 'is_pep', 'customer_since',
] -%}

with events as (
    {{ change_events(source('bronze', 'corebanking_customer')) }}
),

projected as (
    select
        customer_id,
        customer_type,
        gender,
        cast(birth_year as bigint) as birth_year,
        legal_form,
        cast(year(incorporation_date) as bigint) as incorporation_year,
        nationality,
        residence_country,
        city,
        occupation,
        risk_rating,
        is_pep,
        customer_since,
        _op,
        _event_date,
        _kafka_offset,
        _source_committed_at
    from events
),

{{ scd2_versions('projected', 'customer_id', tracked) }}

select
    customer_id,
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

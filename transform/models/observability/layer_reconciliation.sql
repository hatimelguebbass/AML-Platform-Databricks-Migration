{{ config(**dated_table_config()) }}

{#- Record counts and control totals of each dated feed, from the delivery's manifest to
    silver. The ingestion checks each delivered file against its manifest, totals included
    (ADR 0004); this follows the records on, so a filter or join that drops or repeats rows
    between layers cannot pass. The source reconciled its layers against its own ingestion
    log; here the landed count is what the manifest of the slice's delivery declared. -#}

{%- set feeds = {
    'payments': {'table': 'payment_transfer', 'bronze_amount': 'amount_paid'},
    'cards': {'table': 'card_transaction', 'bronze_amount': 'amount_minor'},
} %}

with decisions as (
    select delivery_id, max_by(summary, logged_at) as summary
    from {{ source('landing', 'delivery_log') }}
    group by delivery_id
),

{% for feed, spec in feeds.items() %}
bronze_{{ feed }} as (
    select
        _delivery_date as business_date,
        min(_delivery_id) as delivery_id,
        count(*) as record_count,
        sum({{ spec.bronze_amount }}) as amount
    from {{ source('bronze', spec.table) }}
    where {{ business_date_filter('_delivery_date') }}
    group by _delivery_date
),
{% endfor %}

silver_payments as (
    select business_date, count(*) as record_count, sum(amount_paid) as amount
    from {{ ref('payment_transfer') }}
    where {{ business_date_filter('business_date') }}
    group by business_date
),

silver_cards as (
    -- Back to minor units, the unit of the processor's control totals. The factor is cast to an
    -- integer so the product stays decimal: `power` returns a double, which would round.
    select
        cards.business_date,
        count(*) as record_count,
        sum(cards.amount * cast(power(10, currencies.minor_unit) as bigint)) as amount
    from {{ ref('card_transaction') }} as cards
    left join {{ ref('iso_currency') }} as currencies on currencies.alpha_code = cards.currency
    where {{ business_date_filter('cards.business_date') }}
    group by cards.business_date
),

feeds as (
    {%- for feed, spec in feeds.items() %}
    select
        '{{ feed }}' as feed,
        coalesce(bronze.business_date, silver.business_date) as business_date,
        cast(get_json_object(decisions.summary, '$.tables.{{ spec.table }}') as bigint) as landed_records,
        bronze.record_count as bronze_records,
        silver.record_count as silver_records,
        cast(bronze.amount as decimal(38, 6)) as bronze_amount,
        cast(silver.amount as decimal(38, 6)) as silver_amount
    from bronze_{{ feed }} as bronze
    full outer join silver_{{ feed }} as silver on silver.business_date = bronze.business_date
    left join decisions on decisions.delivery_id = bronze.delivery_id
    {%- if not loop.last %}
    union all
    {%- endif %}
    {%- endfor %}
)

select
    business_date,
    feed,
    coalesce(landed_records, 0) as landed_records,
    coalesce(bronze_records, 0) as bronze_records,
    coalesce(silver_records, 0) as silver_records,
    coalesce(bronze_amount, 0) as bronze_amount,
    coalesce(silver_amount, 0) as silver_amount,
    coalesce(landed_records, 0) = coalesce(bronze_records, 0)
        and coalesce(bronze_records, 0) = coalesce(silver_records, 0)
        and coalesce(bronze_amount, 0) = coalesce(silver_amount, 0) as is_reconciled,
    {{ lineage_columns() }}
from feeds

{#- The audit of bronze before a run builds on it: every bronze slice of the business date comes
    from one delivery, the ingestion's latest decision on that delivery is an acceptance, and each
    table holds the rows its manifest declared. A date with no slice at all was never ingested.
    The delivery log alone is not enough: a later delivery of the date may have been rejected
    while the slice still holds the accepted one. -#}

{%- set tables = [
    'payment_transfer', 'card_transaction', 'corebanking_customer', 'corebanking_account',
    'corebanking_card', 'change_batch', 'party_member', 'sanctions_hit',
] -%}

with slices as (
    {%- for table in tables %}
    select '{{ table }}' as table_name, _delivery_date as business_date, _delivery_id as delivery_id, count(*) as row_count
    from {{ source('bronze', table) }}
    where {{ business_date_filter('_delivery_date') }}
    group by _delivery_date, _delivery_id
    {%- if not loop.last %}
    union all
    {%- endif %}
    {%- endfor %}
),

deliveries as (
    select business_date, delivery_id
    from slices
    group by business_date, delivery_id
),

decisions as (
    select delivery_id, max_by(status, logged_at) as status, max_by(summary, logged_at) as summary
    from {{ source('landing', 'delivery_log') }}
    group by delivery_id
),

expected_dates as (
    {%- if business_date() is none %}
    select distinct business_date from deliveries
    {%- else %}
    select date'{{ business_date() }}' as business_date
    {%- endif %}
),

declared as (
    select deliveries.business_date, deliveries.delivery_id, tables.table_name,
        cast(get_json_object(decisions.summary, '$.tables.' || tables.table_name) as bigint) as row_count
    from deliveries
    inner join decisions on decisions.delivery_id = deliveries.delivery_id
    cross join (select explode(array({{ "'" ~ tables | join("', '") ~ "'" }})) as table_name) as tables
)

select expected_dates.business_date, null as delivery_id, 'no bronze slice' as problem
from expected_dates
left anti join deliveries on deliveries.business_date = expected_dates.business_date

union all

select business_date, null, 'slices from ' || count(*) || ' deliveries'
from deliveries
group by business_date
having count(*) > 1

union all

select deliveries.business_date, deliveries.delivery_id, 'latest decision ' || coalesce(decisions.status, 'missing')
from deliveries
left join decisions on decisions.delivery_id = deliveries.delivery_id
where decisions.status is distinct from 'accepted'

union all

select declared.business_date, declared.delivery_id,
    declared.table_name || ': ' || coalesce(slices.row_count, 0) || ' rows, manifest ' || coalesce(cast(declared.row_count as string), 'none')
from declared
left join slices
    on slices.delivery_id = declared.delivery_id
    and slices.table_name = declared.table_name
where coalesce(slices.row_count, 0) is distinct from declared.row_count

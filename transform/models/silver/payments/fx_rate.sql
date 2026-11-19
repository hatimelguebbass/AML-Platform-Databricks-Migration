{#- Rates the payments hub applies, read off its own cross-currency transfers, so that amounts
    in different currencies compare the way the hub settled them. Their spread is
    tested: a hub that starts applying daily rates fails the run instead of skewing amounts. -#}

{#- Amounts are rounded to the cent: below 100 units of the reporting currency the rounding
    alone moves the observed rate by more than the tolerated spread. -#}
{%- set min_quote = 100 %}

with quotes as (
    select
        payment_currency as currency,
        cast(amount_received as double) / cast(amount_paid as double) as rate
    from {{ ref('payment_transfer') }}
    where receiving_currency = {{ dbt.string_literal(var('reporting_currency')) }}
        and payment_currency <> receiving_currency
        and amount_received >= {{ min_quote }}

    union all

    select
        receiving_currency,
        cast(amount_paid as double) / cast(amount_received as double)
    from {{ ref('payment_transfer') }}
    where payment_currency = {{ dbt.string_literal(var('reporting_currency')) }}
        and payment_currency <> receiving_currency
        and amount_paid >= {{ min_quote }}
),

sorted_quotes as (
    select currency, array_sort(array_agg(rate)) as rates
    from quotes
    group by currency
),

{#- Nearest-rank percentiles on the sorted quotes. approx_percentile merges partial digests in
    whatever order the splits finish: two builds from the same transfers disagreed in the
    tenth digit and moved converted amounts by a cent (source platform, ADR 0010). -#}
observed as (
    select
        currency,
        element_at(rates, cast(ceiling(0.5 * cardinality(rates)) as integer)) as rate,
        element_at(rates, cast(ceiling(0.05 * cardinality(rates)) as integer)) as low_rate,
        element_at(rates, cast(ceiling(0.95 * cardinality(rates)) as integer)) as high_rate,
        cast(cardinality(rates) as bigint) as quotes
    from sorted_quotes
)

select
    currency,
    rate as rate_to_reporting,
    (high_rate - low_rate) / rate as relative_spread,
    quotes,
    {{ lineage_columns() }}
from observed

union all

select
    {{ dbt.string_literal(var('reporting_currency')) }},
    1e0,
    0e0,
    cast(0 as bigint),
    {{ lineage_columns() }}

{#- Data observability of each business date (wave 4), ported from the source's
    macros/observability.sql. The checks are the source's: same baselines, bounds and indexes. -#}

{#- Record counts of `relation` per business date: one `total` row for every date of
    `dates`, zero included, then one row per bin of each dimension, a dimension being a name
    and the SQL expression that bins a record. -#}
{% macro profile(relation, dataset, dimensions, dates) -%}
    select dates.business_date, '{{ dataset }}' as dataset, 'total' as dimension, 'all' as bin,
        count(records.business_date) as record_count
    from {{ dates }} as dates
    left join {{ relation }} as records on records.business_date = dates.business_date
    group by dates.business_date
    {%- for name, expression in dimensions.items() %}
    union all
    select business_date, '{{ dataset }}', '{{ name }}', cast({{ expression }} as string), count(*)
    from {{ relation }}
    group by business_date, cast({{ expression }} as string)
    {%- endfor %}
{%- endmacro %}


{#- Saturday and Sunday are the weekend in Morocco. Spark numbers days from Sunday (1) to
    Saturday (7), where the source's Trino numbered them from Monday (1) to Sunday (7). Bank
    holidays are not modelled: an expected low day is acknowledged in the acknowledged_anomaly seed. -#}
{% macro day_kind(business_date) -%}
    if(dayofweek({{ business_date }}) in (1, 7), 'weekend', 'weekday')
{%- endmacro %}


{#- Order-of-magnitude band of an amount, from "<10" to ">=1M". -#}
{% macro amount_band(amount) -%}
    case
        when {{ amount }} is null then null
        when {{ amount }} < 10 then '<10'
        when {{ amount }} < 100 then '10-100'
        when {{ amount }} < 1000 then '100-1K'
        when {{ amount }} < 10000 then '1K-10K'
        when {{ amount }} < 100000 then '10K-100K'
        when {{ amount }} < 1000000 then '100K-1M'
        else '>=1M'
    end
{%- endmacro %}


{#- Business dates a check compares the loaded date with: for each dataset, the
    `baseline_days` loaded dates of the same kind before it that nobody acknowledged as
    anomalous. Payments clear on working days, so weekend volumes are less than half of a
    weekday's: weekends are compared with weekends. A known outage left in the baseline
    would make the next days look normal. -#}
{% macro baseline_dates(profile_relation) -%}
    {%- set settings = var('observability') -%}
    with loaded as (
        select distinct profile.dataset, profile.business_date
        from {{ profile_relation }} as profile
        where profile.dimension = 'total'
            and not exists (
                select 1
                from {{ ref('acknowledged_anomaly') }} as acknowledged
                where acknowledged.dataset = profile.dataset
                    and profile.business_date between acknowledged.from_date and acknowledged.to_date
            )
    ),

    checked as (
        select distinct dataset, business_date
        from {{ profile_relation }}
        where dimension = 'total' and {{ business_date_filter('business_date') }}
    )

    select dataset, business_date, baseline_date
    from (
        select
            checked.dataset,
            checked.business_date,
            loaded.business_date as baseline_date,
            row_number() over (
                partition by checked.dataset, checked.business_date order by loaded.business_date desc
            ) as recency
        from checked
        inner join loaded
            on loaded.dataset = checked.dataset
            and loaded.business_date < checked.business_date
            and {{ day_kind('loaded.business_date') }} = {{ day_kind('checked.business_date') }}
    )
    where recency <= {{ settings.baseline_days }}
{%- endmacro %}

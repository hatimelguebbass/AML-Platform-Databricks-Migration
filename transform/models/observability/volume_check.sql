{{ config(**dated_table_config()) }}

{#- Each dataset's volume on the loaded date against the median of its baseline dates.
    Every input file can be complete and valid while the day is still wrong: a feed cut at
    the source, a join that stopped matching, a rule that stopped firing. -#}

{%- set settings = var('observability') %}

with baseline_dates as (
    {{ baseline_dates(ref('dataset_profile')) }}
),

totals as (
    select dataset, business_date, record_count
    from {{ ref('dataset_profile') }}
    where dimension = 'total'
),

baselines as (
    select
        baseline_dates.dataset,
        baseline_dates.business_date,
        count(*) as baseline_days,
        array_sort(collect_list(totals.record_count)) as counts
    from baseline_dates
    inner join totals
        on totals.dataset = baseline_dates.dataset
        and totals.business_date = baseline_dates.baseline_date
    group by baseline_dates.dataset, baseline_dates.business_date
),

checked as (
    select
        totals.business_date,
        totals.dataset,
        totals.record_count,
        coalesce(baselines.baseline_days, 0) as baseline_days,
        -- Exact median: the mean of the one or two middle values of the sorted counts. `div` is the
        -- integer division Trino's `/` did on integers (Spark's `/` returns a fraction); it returns
        -- a bigint, and element_at takes an int.
        cast(
            (element_at(baselines.counts, cast((size(baselines.counts) + 1) div 2 as int))
            + element_at(baselines.counts, cast((size(baselines.counts) + 2) div 2 as int))) / 2.0
            as double
        ) as baseline_median
    from totals
    left join baselines
        on baselines.dataset = totals.dataset
        and baselines.business_date = totals.business_date
    where {{ business_date_filter('totals.business_date') }}
),

bounds as (
    {%- for dataset, bound in settings.volume.items() %}
    select
        '{{ dataset }}' as dataset,
        {{ bound.min_ratio }} as min_ratio,
        {{ bound.max_ratio }} as max_ratio,
        {{ 'true' if bound.blocking else 'false' }} as is_blocking
    {%- if not loop.last %}
    union all
    {%- endif %}
    {%- endfor %}
),

rated as (
    select
        checked.*,
        case when checked.baseline_median > 0 then checked.record_count / checked.baseline_median end as volume_ratio,
        bounds.min_ratio,
        bounds.max_ratio,
        bounds.is_blocking
    from checked
    inner join bounds on bounds.dataset = checked.dataset
)

select
    rated.business_date,
    rated.dataset,
    rated.record_count,
    cast(rated.baseline_days as int) as baseline_days,
    rated.baseline_median,
    rated.volume_ratio,
    cast(rated.min_ratio as double) as min_ratio,
    cast(rated.max_ratio as double) as max_ratio,
    rated.is_blocking,
    case
        when rated.baseline_days < {{ settings.min_baseline_days }} then 'NO_BASELINE'
        when rated.baseline_median = 0 and rated.record_count = 0 then 'OK'
        when rated.volume_ratio is not null and rated.volume_ratio between rated.min_ratio and rated.max_ratio then 'OK'
        when acknowledged.dataset is not null then 'ACKNOWLEDGED'
        else 'ANOMALY'
    end as status,
    acknowledged.reason as acknowledgement,
    {{ lineage_columns() }}
from rated
left join {{ ref('acknowledged_anomaly') }} as acknowledged
    on acknowledged.dataset = rated.dataset
    and rated.business_date between acknowledged.from_date and acknowledged.to_date

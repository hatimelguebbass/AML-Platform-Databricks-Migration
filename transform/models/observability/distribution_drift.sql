{{ config(**dated_table_config()) }}

{#- Population stability index of each profiled dimension on the loaded date against its
    baseline dates pooled: sum over bins of (p - q) * ln(p / q). Below 0.1 the mix is
    stable, from 0.1 a shift, from the drift threshold a change to look into. A date with
    no record has no distribution: EMPTY, and volume_check reports it. -#}

{%- set settings = var('observability') %}
{#- Floor on a bin's share, so a bin absent on one side counts as rare rather than infinite. -#}
{%- set epsilon = 0.0001 %}

with baseline_dates as (
    {{ baseline_dates(ref('dataset_profile')) }}
),

bins as (
    select dataset, dimension, bin, business_date, record_count
    from {{ ref('dataset_profile') }}
    where dimension <> 'total'
),

current_bins as (
    select dataset, dimension, bin, business_date, record_count
    from bins
    where {{ business_date_filter('business_date') }}
),

baseline_bins as (
    select
        baseline_dates.dataset,
        bins.dimension,
        bins.bin,
        baseline_dates.business_date,
        sum(bins.record_count) as record_count
    from baseline_dates
    inner join bins
        on bins.dataset = baseline_dates.dataset
        and bins.business_date = baseline_dates.baseline_date
    group by baseline_dates.dataset, bins.dimension, bins.bin, baseline_dates.business_date
),

baseline_size as (
    select dataset, business_date, count(*) as baseline_days
    from baseline_dates
    group by dataset, business_date
),

compared as (
    select
        coalesce(current_bins.dataset, baseline_bins.dataset) as dataset,
        coalesce(current_bins.dimension, baseline_bins.dimension) as dimension,
        coalesce(current_bins.business_date, baseline_bins.business_date) as business_date,
        cast(coalesce(current_bins.record_count, 0) as double) as current_count,
        cast(coalesce(baseline_bins.record_count, 0) as double) as baseline_count
    from current_bins
    full outer join baseline_bins
        on baseline_bins.dataset = current_bins.dataset
        and baseline_bins.dimension = current_bins.dimension
        and baseline_bins.bin = current_bins.bin
        and baseline_bins.business_date = current_bins.business_date
),

shares as (
    select
        dataset,
        dimension,
        business_date,
        greatest(current_count / nullif(sum(current_count) over (partition by dataset, dimension, business_date), 0), {{ epsilon }}) as p,
        greatest(baseline_count / nullif(sum(baseline_count) over (partition by dataset, dimension, business_date), 0), {{ epsilon }}) as q
    from compared
),

indexed as (
    select
        dataset,
        dimension,
        business_date,
        count(*) as bins,
        sum((p - q) * ln(p / q)) as stability_index
    from shares
    group by dataset, dimension, business_date
)

select
    indexed.business_date,
    indexed.dataset,
    indexed.dimension,
    cast(indexed.bins as int) as bins,
    cast(coalesce(baseline_size.baseline_days, 0) as int) as baseline_days,
    indexed.stability_index,
    case
        when coalesce(baseline_size.baseline_days, 0) < {{ settings.min_baseline_days }} then 'NO_BASELINE'
        when indexed.stability_index is null then 'EMPTY'
        when indexed.stability_index < 0.1 then 'STABLE'
        when indexed.stability_index < {{ settings.drift_threshold }} then 'SHIFT'
        when acknowledged.dataset is not null then 'ACKNOWLEDGED'
        else 'DRIFT'
    end as status,
    {{ lineage_columns() }}
from indexed
left join baseline_size
    on baseline_size.dataset = indexed.dataset
    and baseline_size.business_date = indexed.business_date
left join {{ ref('acknowledged_anomaly') }} as acknowledged
    on acknowledged.dataset = indexed.dataset
    and indexed.business_date between acknowledged.from_date and acknowledged.to_date

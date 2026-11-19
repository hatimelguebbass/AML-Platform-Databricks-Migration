{#- Versions of core banking rows, derived from their exported change logs (source ADR 0008).

    The business date of a change is the date of the pipeline run that captured it, which
    the change batches record: commit times are wall-clock times of the capture, not the
    business calendar being loaded. A snapshot read carries the row as it was last
    modified, so it dates from updated_at. Outside the orchestrator, the commit date.
    Commit and update times are instants; their date is taken in UTC, as on the source. -#}
{% macro change_events(relation) -%}
    select
        event.*,
        case
            when event._op = 'r' then {{ utc_date('event.updated_at') }}
            else coalesce(batch.business_date, {{ utc_date('event._source_committed_at') }})
        end as _event_date
    from {{ relation }} as event
    left join {{ source('bronze', 'change_batch') }} as batch
        on batch.run_id = event._ingest_run_id
        and batch.topic = event._kafka_topic
        and batch.kafka_partition = event._kafka_partition
{%- endmacro %}


{#- Type 2 history of `key` at daily grain, from a relation of change events that has the
    `tracked` columns plus _op, _event_date, _kafka_offset, _source_committed_at.

    A key's events always share one partition (change topics are never repartitioned),
    so their offsets order them. A day's last event is the row's state at the end of
    that day. A version starts when a tracked column or the deleted state changes, which
    also folds the duplicates of an at-least-once delivery or a repeated snapshot. -#}
{% macro scd2_versions(events, key, tracked) -%}
    dated as (
        select
            *,
            -- An event never dates before one that precedes it.
            max(_event_date) over (
                partition by {{ key }} order by _kafka_offset rows between unbounded preceding and current row
            ) as _effective_date
        from {{ events }}
    ),

    end_of_day as (
        select *
        from (
            select
                *,
                row_number() over (partition by {{ key }}, _effective_date order by _kafka_offset desc) as _day_rank
            from dated
        )
        where _day_rank = 1
    ),

    changes as (
        select
            *,
            _op = 'd' as _is_deleted,
            lag(_effective_date) over by_date is null
            or (_op = 'd') is distinct from (lag(_op) over by_date = 'd')
            {%- for column in tracked %}
            or {{ column }} is distinct from lag({{ column }}) over by_date
            {%- endfor %} as _starts_version
        from end_of_day
        window by_date as (partition by {{ key }} order by _effective_date)
    ),

    versions as (
        select
            *,
            lead(_effective_date) over (partition by {{ key }} order by _effective_date) as _next_version_date
        from changes
        where _starts_version
    )
{%- endmacro %}


{#- The version of a type 2 history that held at the end of a business date. -#}
{% macro in_force_on(history, business_date) -%}
    {{ history }}.valid_from <= {{ business_date }}
    and ({{ history }}.valid_to is null or {{ history }}.valid_to > {{ business_date }})
{%- endmacro %}

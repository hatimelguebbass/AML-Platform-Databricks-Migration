{#- A type 2 history: versions of a key never overlap, only the last one may be open,
    and is_current marks exactly the open one. -#}
{% test valid_history(model, key) %}

with versions as (
    select
        {{ key }} as history_key,
        valid_from,
        valid_to,
        is_current,
        lead(valid_from) over (partition by {{ key }} order by valid_from) as next_valid_from,
        count(*) over (partition by {{ key }}, valid_from) as versions_starting_that_day
    from {{ model }}
)

select *
from versions
where valid_to <= valid_from
    or next_valid_from < valid_to
    or (valid_to is null and next_valid_from is not null)
    or is_current <> (valid_to is null)
    or versions_starting_that_day > 1

{% endtest %}

{#- Uniqueness over the business dates the run loaded: other dates were tested when they
    were loaded, so a daily run reads one date of the table, not its whole history. -#}
{% test unique_in_loaded_dates(model, column_name) %}

select {{ column_name }}, count(*) as occurrences
from {{ model }}
where {{ business_date_filter('business_date') }}
group by {{ column_name }}
having count(*) > 1

{% endtest %}

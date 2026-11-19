{#- With loaded_dates_only, the check covers the business dates the run loaded, as
    unique_in_loaded_dates does for a single column. -#}
{% test unique_combination(model, columns, loaded_dates_only=false) %}

select {{ columns | join(', ') }}, count(*) as occurrences
from {{ model }}
{%- if loaded_dates_only %}
where {{ business_date_filter('business_date') }}
{%- endif %}
group by {{ columns | join(', ') }}
having count(*) > 1

{% endtest %}

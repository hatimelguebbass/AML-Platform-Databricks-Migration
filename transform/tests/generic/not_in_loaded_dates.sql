{#- No row of the loaded business dates has `value` in the column, among the rows that
    also meet `condition` when given. -#}
{% test not_in_loaded_dates(model, column_name, value, condition=none) %}

select *
from {{ model }}
where {{ business_date_filter('business_date') }}
    and {{ column_name }} = {{ "'" ~ value ~ "'" if value is string else value }}
    {%- if condition %}
    and ({{ condition }})
    {%- endif %}

{% endtest %}

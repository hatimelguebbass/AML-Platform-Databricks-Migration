{% test at_most(model, column_name, max_value) %}

select {{ column_name }}
from {{ model }}
where {{ column_name }} > {{ max_value }}

{% endtest %}

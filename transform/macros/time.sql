{#- Date of an instant in UTC. A plain cast would take the date in the session time zone. -#}
{% macro utc_date(instant) -%}
    cast(convert_timezone('UTC', {{ instant }}) as date)
{%- endmacro %}

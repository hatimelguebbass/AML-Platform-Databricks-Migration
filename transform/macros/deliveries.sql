{#- Rows of the latest snapshot an on-premises table was delivered with, as of the business
    date when one is loaded: a rerun of a past date sees what that date saw. -#}
{% macro latest_snapshot(relation) -%}
    select *
    from {{ relation }}
    where _delivery_date = (
        select max(_delivery_date)
        from {{ relation }}
        {%- if business_date() is not none %}
        where _delivery_date <= date'{{ business_date() }}'
        {%- endif %}
    )
{%- endmacro %}

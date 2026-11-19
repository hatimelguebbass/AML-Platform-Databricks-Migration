{#- The payments hub writes the same bank as 010, 0010 or 10. -#}
{% macro normalize_bank_id(column) -%}
    regexp_replace({{ column }}, '^0+(?=.)', '')
{%- endmacro %}

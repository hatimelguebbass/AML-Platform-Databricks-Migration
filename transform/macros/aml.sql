{#- Transaction monitoring helpers. Rules read their parameters from the aml_rules var. -#}

{% macro aml_rule(key) -%}
    {%- set rules = var('aml_rules') -%}
    {%- if key not in rules -%}
        {{ exceptions.raise_compiler_error("no AML rule " ~ key ~ " in the aml_rules var") }}
    {%- endif -%}
    {{ return(rules[key]) }}
{%- endmacro %}

{#- Days of history before the loaded date that the rules read, for the longest of them. -#}
{% macro aml_lookback_days() -%}
    {%- set rules = var('aml_rules') -%}
    {%- set hour_windows = [rules.pass_through.parameters.window_hours, rules.round_trip.parameters.window_hours] -%}
    {{ return([rules.volume_spike.parameters.lookback_days, days_covering(hour_windows | max)] | max) }}
{%- endmacro %}

{#- Business dates a window of hours ending on the loaded date reaches back to. -#}
{% macro days_covering(hours) -%}
    {{ return((hours / 24) | round(0, 'ceil') | int) }}
{%- endmacro %}

{% macro sql_values(values) -%}
    {%- for value in values -%}
        {{ dbt.string_literal(value) }}{{ ", " if not loop.last }}
    {%- endfor -%}
{%- endmacro %}

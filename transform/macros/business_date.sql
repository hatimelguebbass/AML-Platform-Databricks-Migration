{#- Dated models load one business date per pipeline run and replace it atomically when the
    date is reloaded (Delta REPLACE WHERE). `all` rebuilds every date, as does leaving the
    variable unset outside the job. An empty value is refused: it is what the job passes when
    its parameter was lost (a repair without parameters), and it must not turn into a rebuild. -#}

{% macro business_date() -%}
    {%- set value = var('business_date') -%}
    {%- if value == 'all' -%}
        {{ return(none) }}
    {%- endif -%}
    {%- if value is not none and not modules.re.fullmatch('[0-9]{4}-[0-9]{2}-[0-9]{2}', value | string) -%}
        {{ exceptions.raise_compiler_error("business_date must be YYYY-MM-DD or all, got '" ~ value ~ "'") }}
    {%- endif -%}
    {{ return(value) }}
{%- endmacro %}

{% macro business_date_filter(column) -%}
    {%- if business_date() is none -%}
        true
    {%- else -%}
        {{ column }} = date'{{ business_date() }}'
    {%- endif -%}
{%- endmacro %}

{#- The loaded date and the days before it that rules looking back over history need. -#}
{% macro business_date_window(column, days_before) -%}
    {%- if business_date() is none -%}
        true
    {%- else -%}
        {{ column }} between date'{{ business_date() }}' - interval '{{ days_before }}' day and date'{{ business_date() }}'
    {%- endif -%}
{%- endmacro %}

{#- Config of a model holding one slice per business date. A rebuild of every date recreates
    the table, so that a changed model is never refused by its own previous columns. -#}
{% macro dated_table_config() -%}
    {{ return({
        'materialized': 'incremental',
        'incremental_strategy': 'replace_where',
        'incremental_predicates': [business_date_filter('business_date')],
        'full_refresh': business_date() is none,
    }) }}
{%- endmacro %}

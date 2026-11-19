{#- The load time is a UTC wall-clock time without zone, as on the source platform, so it reads
    the same whatever the session time zone of the reader. -#}
{% macro lineage_columns() -%}
    {{ dbt.string_literal(var('pipeline_run') or invocation_id) }} as _pipeline_run_id,
    convert_timezone('UTC', current_timestamp()) as _loaded_at
{%- endmacro %}

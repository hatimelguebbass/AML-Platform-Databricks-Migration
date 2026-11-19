{#- Layers are namespaces shared by every environment, so a custom schema is used as is
    instead of being prefixed with the target schema. dbt builds each layer in its staging
    schema; the published layer is written only by the publication, after the audits pass
    (ADR 0006). -#}
{% macro generate_schema_name(custom_schema_name, node) -%}
    {{ (custom_schema_name or target.schema) | trim }}{{ var('staging_suffix') }}
{%- endmacro %}

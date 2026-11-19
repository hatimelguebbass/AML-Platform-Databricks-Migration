{#- Unity Catalog policy functions the models attach to their columns (ADR 0005). Created at
    the start of every invocation, before any model that references them, and replaced in
    place: a changed rule takes effect on every masked column at once. -#}

{% macro create_policy_functions() %}
    {%- if execute and flags.WHICH in ('run', 'build', 'seed') -%}
        {%- set schema = target.database ~ '.' ~ var('governance_schema') -%}
        {%- call statement('create_mask_pseudonym') -%}
            create or replace function {{ schema }}.mask_pseudonym(value string)
            returns string
            comment 'Keyed pseudonym of an account number or card token: null for readers outside the privileged groups.'
            return if({{ privileged_reader() }}, value, null)
        {%- endcall -%}
    {%- endif -%}
    {{ return('') }}
{% endmacro %}

{% macro privileged_reader() -%}
    {%- for group in var('privileged_groups') -%}
        is_account_group_member({{ dbt.string_literal(group) }}){{ ' or ' if not loop.last }}
    {%- endfor -%}
{%- endmacro %}

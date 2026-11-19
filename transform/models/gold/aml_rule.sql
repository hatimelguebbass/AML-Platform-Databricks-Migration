{#- The rule catalogue, rendered from the aml_rules var the rules themselves read. -#}

{%- for key, rule in var('aml_rules').items() %}
select
    cast({{ dbt.string_literal(rule.id) }} as string) as rule_id,
    cast({{ dbt.string_literal(key) }} as string) as rule_key,
    cast({{ dbt.string_literal(rule.name) }} as string) as rule_name,
    cast({{ dbt.string_literal(rule.typology) }} as string) as typology,
    cast({{ dbt.string_literal(rule.description | trim) }} as string) as description,
    cast({{ dbt.string_literal(tojson(rule.parameters)) }} as string) as parameters,
    {{ lineage_columns() }}
{%- if not loop.last %}
union all
{%- endif %}
{%- endfor %}

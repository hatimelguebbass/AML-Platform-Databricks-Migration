{%- set rule = aml_rule('fan_in') -%}

with credits as (
    select transfer_id, business_date, beneficiary_account_id as account_id, originator_account_id as counterparty_id
    from {{ ref('monitored_transfer') }}
    where {{ business_date_filter('business_date') }}
        and payment_format in ({{ sql_values(rule.parameters.payment_formats) }})
),

flagged as (
    select account_id, business_date
    from credits
    group by account_id, business_date
    having count(distinct counterparty_id) >= {{ rule.parameters.min_counterparties }}
)

select
    {{ dbt.string_literal(rule.id) }} as rule_id,
    credits.business_date,
    credits.account_id,
    credits.transfer_id,
    'CREDIT' as evidence_role
from credits
inner join flagged
    on flagged.account_id = credits.account_id
    and flagged.business_date = credits.business_date

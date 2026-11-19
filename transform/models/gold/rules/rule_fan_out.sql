{%- set rule = aml_rule('fan_out') -%}

with debits as (
    select transfer_id, business_date, originator_account_id as account_id, beneficiary_account_id as counterparty_id
    from {{ ref('monitored_transfer') }}
    where {{ business_date_filter('business_date') }}
        and payment_format in ({{ sql_values(rule.parameters.payment_formats) }})
),

flagged as (
    select account_id, business_date
    from debits
    group by account_id, business_date
    having count(distinct counterparty_id) >= {{ rule.parameters.min_counterparties }}
)

select
    {{ dbt.string_literal(rule.id) }} as rule_id,
    debits.business_date,
    debits.account_id,
    debits.transfer_id,
    'DEBIT' as evidence_role
from debits
inner join flagged
    on flagged.account_id = debits.account_id
    and flagged.business_date = debits.business_date

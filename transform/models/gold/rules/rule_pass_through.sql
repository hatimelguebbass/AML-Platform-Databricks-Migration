{%- set rule = aml_rule('pass_through') -%}

{#- A debit of the loaded date matched to the credits it may forward: earlier, within the
    window, from someone else than the payee, and at most the amount received. The debits
    are the smaller side, so they are the join's build side. -#}

with credits as (
    select *
    from {{ ref('monitored_transfer') }}
    where {{ business_date_window('business_date', days_covering(rule.parameters.window_hours)) }}
        and payment_format in ({{ sql_values(rule.parameters.payment_formats) }})
),

debits as (
    select *
    from {{ ref('monitored_transfer') }}
    where {{ business_date_filter('business_date') }}
        and payment_format in ({{ sql_values(rule.parameters.payment_formats) }})
),

forwarded as (
    select
        debits.business_date,
        debits.originator_account_id as account_id,
        credits.transfer_id as credit_id,
        debits.transfer_id as debit_id
    from credits
    inner join debits
        on debits.originator_account_id = credits.beneficiary_account_id
        and debits.booked_at > credits.booked_at
        and debits.booked_at <= credits.booked_at + interval '{{ rule.parameters.window_hours }}' hour
        and debits.beneficiary_account_id <> credits.originator_account_id
        and debits.amount between credits.amount * {{ rule.parameters.min_forwarded_ratio }} and credits.amount
)

select {{ dbt.string_literal(rule.id) }} as rule_id, business_date, account_id, credit_id as transfer_id, 'CREDIT' as evidence_role
from forwarded

union

select {{ dbt.string_literal(rule.id) }}, business_date, account_id, debit_id, 'DEBIT'
from forwarded

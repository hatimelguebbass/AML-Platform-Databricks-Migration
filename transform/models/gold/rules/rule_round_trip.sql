{%- set rule = aml_rule('round_trip') -%}

{#- A transfer of the loaded date that pays back an account which paid the same counterparty
    within the window. The alert is on the account the funds came back to. Transfers of the
    loaded date are the smaller side, so they are the join's build side. -#}

with outbound as (
    select *
    from {{ ref('monitored_transfer') }}
    where {{ business_date_window('business_date', days_covering(rule.parameters.window_hours)) }}
),

returns as (
    select *
    from {{ ref('monitored_transfer') }}
    where {{ business_date_filter('business_date') }}
),

returned as (
    select
        returns.business_date,
        returns.beneficiary_account_id as account_id,
        outbound.transfer_id as outbound_id,
        returns.transfer_id as return_id
    from outbound
    inner join returns
        on returns.beneficiary_account_id = outbound.originator_account_id
        and returns.originator_account_id = outbound.beneficiary_account_id
        and returns.booked_at > outbound.booked_at
        and returns.booked_at <= outbound.booked_at + interval '{{ rule.parameters.window_hours }}' hour
)

select {{ dbt.string_literal(rule.id) }} as rule_id, business_date, account_id, outbound_id as transfer_id, 'DEBIT' as evidence_role
from returned

union

select {{ dbt.string_literal(rule.id) }}, business_date, account_id, return_id, 'CREDIT'
from returned

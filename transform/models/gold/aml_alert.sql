{{ config(**dated_table_config()) }}

{#- One alert per rule, account and business date, with what triage sorts on: who the
    customer was on that date, and how much and how many counterparties are involved. -#}

with evidence as (
    select *
    from {{ ref('aml_alert_transfer') }}
    where {{ business_date_filter('business_date') }}
),

alerts as (
    select
        alert_id,
        rule_id,
        business_date,
        account_id,
        count(*) as transfer_count,
        count(distinct counterparty_account_id) as counterparty_count,
        sum(amount) filter (where evidence_role = 'CREDIT') as credited_amount,
        sum(amount) filter (where evidence_role = 'DEBIT') as debited_amount,
        min(booked_at) as first_booked_at,
        max(booked_at) as last_booked_at
    from evidence
    group by alert_id, rule_id, business_date, account_id
)

select
    alerts.alert_id,
    alerts.rule_id,
    alerts.business_date,
    alerts.account_id,
    accounts.customer_id,
    members.party_id,
    customers.risk_rating as customer_risk_rating,
    customers.is_pep,
    alerts.transfer_count,
    alerts.counterparty_count,
    cast(coalesce(alerts.credited_amount, 0) as decimal(38, 2)) as credited_amount,
    cast(coalesce(alerts.debited_amount, 0) as decimal(38, 2)) as debited_amount,
    alerts.first_booked_at,
    alerts.last_booked_at,
    {{ lineage_columns() }}
from alerts
left join {{ ref('account_history') }} as accounts
    on accounts.account_id = alerts.account_id
    and {{ in_force_on('accounts', 'alerts.business_date') }}
left join {{ ref('customer_history') }} as customers
    on customers.customer_id = accounts.customer_id
    and {{ in_force_on('customers', 'alerts.business_date') }}
left join {{ ref('party_member') }} as members
    on members.customer_id = accounts.customer_id

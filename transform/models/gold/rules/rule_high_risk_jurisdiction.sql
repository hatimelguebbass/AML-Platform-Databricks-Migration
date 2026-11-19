{%- set rule = aml_rule('high_risk_jurisdiction') -%}

{#- Customer attributes and FATF lists as they stood on each business date, so that a rerun
    of a past date gives the alerts that date would have given. -#}

with movements as (
    select transfer_id, business_date, originator_account_id as account_id, amount, 'DEBIT' as evidence_role
    from {{ ref('monitored_transfer') }}
    where {{ business_date_filter('business_date') }}

    union all

    select transfer_id, business_date, beneficiary_account_id, amount, 'CREDIT'
    from {{ ref('monitored_transfer') }}
    where {{ business_date_filter('business_date') }}
),

exposed as (
    select distinct accounts.account_id, movements.business_date
    from movements
    inner join {{ ref('account_history') }} as accounts
        on accounts.account_id = movements.account_id
        and {{ in_force_on('accounts', 'movements.business_date') }}
    inner join {{ ref('customer_history') }} as customers
        on customers.customer_id = accounts.customer_id
        and {{ in_force_on('customers', 'movements.business_date') }}
    inner join {{ ref('high_risk_jurisdiction') }} as listed
        on listed.country_code in (customers.residence_country, customers.nationality)
        and listed.in_force_from <= movements.business_date
        and (listed.in_force_to is null or listed.in_force_to > movements.business_date)
    where listed.country_code <> {{ dbt.string_literal(var('home_country')) }}
),

flagged as (
    select movements.account_id, movements.business_date
    from movements
    inner join exposed
        on exposed.account_id = movements.account_id
        and exposed.business_date = movements.business_date
    group by movements.account_id, movements.business_date
    having sum(movements.amount) >= {{ rule.parameters.min_amount }}
)

select
    {{ dbt.string_literal(rule.id) }} as rule_id,
    movements.business_date,
    movements.account_id,
    movements.transfer_id,
    movements.evidence_role
from movements
inner join flagged
    on flagged.account_id = movements.account_id
    and flagged.business_date = movements.business_date

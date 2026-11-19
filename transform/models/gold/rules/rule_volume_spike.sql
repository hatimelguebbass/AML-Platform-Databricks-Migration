{%- set rule = aml_rule('volume_spike') -%}
{%- set lookback = rule.parameters.lookback_days -%}

with daily as (
    select originator_account_id as account_id, business_date, sum(amount) as amount
    from {{ ref('monitored_transfer') }}
    group by originator_account_id, business_date
),

{#- Days without outflow count as zero in the average: an account quiet for a week that
    suddenly pays out is the case to catch. An account needs a few active days for its
    average to be a baseline at all. -#}
compared as (
    select
        today.account_id,
        today.business_date,
        today.amount,
        -- Rounded to the cent as the source platform's decimal division does: a threshold compared
        -- with an average at another scale would flip boundary cases between the platforms.
        cast(coalesce(sum(history.amount), 0) / {{ lookback }} as decimal(38, 2)) as average_amount,
        count(history.business_date) as active_days
    from daily as today
    left join daily as history
        on history.account_id = today.account_id
        and history.business_date between today.business_date - interval '{{ lookback }}' day
            and today.business_date - interval '1' day
    where {{ business_date_filter('today.business_date') }}
    group by today.account_id, today.business_date, today.amount
),

flagged as (
    select account_id, business_date
    from compared
    where active_days >= {{ rule.parameters.min_active_days }}
        and amount >= {{ rule.parameters.min_amount }}
        and amount >= average_amount * {{ rule.parameters.min_multiple }}
)

select
    {{ dbt.string_literal(rule.id) }} as rule_id,
    debits.business_date,
    debits.originator_account_id as account_id,
    debits.transfer_id,
    'DEBIT' as evidence_role
from {{ ref('monitored_transfer') }} as debits
inner join flagged
    on flagged.account_id = debits.originator_account_id
    and flagged.business_date = debits.business_date

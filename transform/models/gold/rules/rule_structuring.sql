{%- set rule = aml_rule('structuring') -%}
{%- set threshold = rule.parameters.threshold -%}

with near_threshold as (
    select transfer_id, business_date, originator_account_id as account_id
    from {{ ref('monitored_transfer') }}
    where {{ business_date_filter('business_date') }}
        and amount >= {{ threshold }} * {{ rule.parameters.band_ratio }}
        and amount < {{ threshold }}
),

flagged as (
    select account_id, business_date
    from near_threshold
    group by account_id, business_date
    having count(*) >= {{ rule.parameters.min_transfers }}
)

select
    {{ dbt.string_literal(rule.id) }} as rule_id,
    near_threshold.business_date,
    near_threshold.account_id,
    near_threshold.transfer_id,
    'DEBIT' as evidence_role
from near_threshold
inner join flagged
    on flagged.account_id = near_threshold.account_id
    and flagged.business_date = near_threshold.business_date

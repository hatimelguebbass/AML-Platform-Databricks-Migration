{{ config(**dated_table_config()) }}

{#- Record counts of the loaded date's datasets, in total and by the dimensions their
    drift is watched on. Small enough to keep every date: the checks read their baselines
    here instead of rescanning the history of silver and gold. A dataset with no record on
    a loaded date still has its total, zero: a rule that stopped firing is an anomaly. -#}

with payments as (
    select
        transfers.business_date,
        transfers.payment_format,
        transfers.payment_currency,
        {{ amount_band('transfers.amount_paid * rates.rate_to_reporting') }} as amount_band,
        transfers.originator_account_id is not null and transfers.beneficiary_account_id is not null as is_resolved
    from {{ ref('payment_transfer') }} as transfers
    left join {{ ref('fx_rate') }} as rates
        on rates.currency = transfers.payment_currency
    where {{ business_date_filter('transfers.business_date') }}
),

cards as (
    select business_date, channel, txn_type, is_approved
    from {{ ref('card_transaction') }}
    where {{ business_date_filter('business_date') }}
),

alerts as (
    select business_date, rule_id
    from {{ ref('aml_alert') }}
    where {{ business_date_filter('business_date') }}
),

loaded_dates as (
    select business_date from payments
    union
    select business_date from cards
),

profiles as (
    {{ profile('payments', 'silver.payment_transfer', {
        'payment_format': 'payment_format',
        'payment_currency': 'payment_currency',
        'amount_band': 'amount_band',
        'accounts_resolved': "if(is_resolved, 'yes', 'no')",
    }, 'loaded_dates') }}
    union all
    {{ profile('cards', 'silver.card_transaction', {
        'channel': 'channel',
        'txn_type': 'txn_type',
        'approved': "if(is_approved, 'yes', 'no')",
    }, 'loaded_dates') }}
    union all
    {{ profile('alerts', 'gold.aml_alert', {'rule_id': 'rule_id'}, 'loaded_dates') }}
)

select
    business_date,
    dataset,
    dimension,
    coalesce(bin, 'null') as bin,
    record_count,
    {{ lineage_columns() }}
from profiles

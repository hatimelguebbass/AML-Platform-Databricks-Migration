{#- Clustered on the business date each run replaces, then on the card an investigation
    reads across dates (ADR 0009). -#}
{{ config(liquid_clustered_by=['business_date', 'card_id'], **dated_table_config()) }}

{#- Card processor transactions in major units with signed amounts, resolved to the card
    and account of the core banking. Terminal local time is kept as delivered: the
    processor does not send the terminal's zone. -#}

with transactions as (
    select
        transactions.*,
        currencies.alpha_code as currency_code,
        cast(
            cast(transactions.amount_minor as decimal(22, 6)) / cast(power(10, currencies.minor_unit) as bigint)
            as decimal(22, 6)
        ) as amount
    from {{ source('bronze', 'card_transaction') }} as transactions
    left join {{ ref('iso_currency') }} as currencies
        on currencies.numeric_code = transactions.currency
    where {{ business_date_filter('transactions._business_date') }}
),

cards as (
    select card_id, account_id, card_token
    from {{ ref('card_history') }}
    where is_current
)

select
    transactions.txn_ref,
    transactions._business_date as business_date,
    transactions.txn_local_time as txn_local_at,
    transactions.txn_type,
    transactions.channel,
    cards.card_id,
    cards.account_id,
    transactions.card_token,
    transactions.mcc,
    transactions.merchant_id,
    transactions.merchant_name,
    transactions.merchant_city,
    transactions.merchant_country,
    transactions.currency_code as currency,
    transactions.amount,
    -- Refunds credit the card; withdrawals and purchases debit it.
    if(transactions.txn_type = 'REF', -transactions.amount, transactions.amount) as signed_amount,
    transactions.response_code,
    transactions.response_code = '00' as is_approved,
    transactions.auth_code,
    transactions._source_file,
    {{ lineage_columns() }}
from transactions
left join cards on cards.card_token = transactions.card_token

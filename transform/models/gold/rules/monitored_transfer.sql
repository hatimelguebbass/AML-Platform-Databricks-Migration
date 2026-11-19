{#- Transfers the rules see: between two different accounts of the core banking, over the
    loaded date and the history the rules look back on, in the reporting currency. -#}

select
    transfers.transfer_id,
    transfers.business_date,
    transfers.booked_at,
    transfers.originator_account_id,
    transfers.beneficiary_account_id,
    transfers.payment_format,
    cast(transfers.amount_paid * rates.rate_to_reporting as decimal(22, 2)) as amount
from {{ ref('payment_transfer') }} as transfers
inner join {{ ref('fx_rate') }} as rates
    on rates.currency = transfers.payment_currency
where {{ business_date_window('transfers.business_date', aml_lookback_days()) }}
    and transfers.originator_account_id <> transfers.beneficiary_account_id

-- Every card the processor reports was issued by the core banking, in a known currency.
select txn_ref, business_date, card_token, currency
from {{ ref('card_transaction') }}
where {{ business_date_filter('business_date') }}
    and (card_id is null or currency is null)

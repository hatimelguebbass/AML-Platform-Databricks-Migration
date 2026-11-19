-- Every account of the payments hub is held at the bank: a transfer whose account does not
-- resolve to the core banking is a conformance defect, not an outside party.
select transfer_id, business_date, originator_account, beneficiary_account
from {{ ref('payment_transfer') }}
where {{ business_date_filter('business_date') }}
    and (originator_account_id is null or beneficiary_account_id is null)

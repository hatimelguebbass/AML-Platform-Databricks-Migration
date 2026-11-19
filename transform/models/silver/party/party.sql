{#- Customer 360: one row per party with what its records hold together, as of now. -#}

with members as (
    select
        member.party_id,
        member.customer_id,
        customer.customer_type,
        customer.residence_country,
        customer.risk_rating,
        customer.is_pep,
        customer.customer_since
    from {{ ref('party_member') }} as member
    inner join {{ ref('customer_history') }} as customer
        on customer.customer_id = member.customer_id
        and customer.is_current
),

accounts as (
    select
        member.party_id,
        count(*) as account_count,
        count_if(account.status = 'ACTIVE') as active_account_count
    from {{ ref('party_member') }} as member
    inner join {{ ref('account_history') }} as account
        on account.customer_id = member.customer_id
        and account.is_current
    group by member.party_id
)

select
    members.party_id,
    -- The surviving record's type: a party is a person or an organization, never both.
    max_by(members.customer_type, members.customer_id = members.party_id) as customer_type,
    array_sort(array_agg(members.customer_id)) as customer_ids,
    count(*) as customer_count,
    case max(case members.risk_rating when 'HIGH' then 3 when 'MEDIUM' then 2 when 'LOW' then 1 end)
        when 3 then 'HIGH' when 2 then 'MEDIUM' when 1 then 'LOW'
    end as risk_rating,
    bool_or(members.is_pep) as is_pep,
    min(members.customer_since) as customer_since,
    array_sort(array_distinct(array_agg(members.residence_country))) as residence_countries,
    coalesce(max(accounts.account_count), 0) as account_count,
    coalesce(max(accounts.active_account_count), 0) as active_account_count,
    {{ lineage_columns() }}
from members
left join accounts on accounts.party_id = members.party_id
group by members.party_id

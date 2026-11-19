{{ config(**dated_table_config()) }}

{#- The transfers behind each alert: what an investigator opens first, and what the rules
    are measured on. The alert id is derived from what the alert is about, so a rerun of a
    date gives the same ids and case references stay valid. -#}

with evidence as (
    {%- for key in var('aml_rules') %}
    select * from {{ ref('rule_' ~ key) }}
    {%- if not loop.last %}
    union all
    {%- endif %}
    {%- endfor %}
)

select
    md5(evidence.rule_id || '|' || cast(evidence.account_id as string) || '|' || cast(evidence.business_date as string)) as alert_id,
    evidence.rule_id,
    evidence.business_date,
    evidence.account_id,
    evidence.transfer_id,
    evidence.evidence_role,
    transfers.booked_at,
    case evidence.evidence_role
        when 'DEBIT' then transfers.beneficiary_account_id
        else transfers.originator_account_id
    end as counterparty_account_id,
    transfers.amount,
    {{ lineage_columns() }}
from evidence
inner join {{ ref('monitored_transfer') }} as transfers
    on transfers.transfer_id = evidence.transfer_id

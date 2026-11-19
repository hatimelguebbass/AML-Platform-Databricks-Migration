{#- Every alert of the loaded dates is backed by its transfers, and no evidence is orphaned. -#}

with alerts as (
    select alert_id, transfer_count
    from {{ ref('aml_alert') }}
    where {{ business_date_filter('business_date') }}
),

evidence as (
    select alert_id, count(*) as transfer_count
    from {{ ref('aml_alert_transfer') }}
    where {{ business_date_filter('business_date') }}
    group by alert_id
)

select coalesce(alerts.alert_id, evidence.alert_id) as alert_id, alerts.transfer_count, evidence.transfer_count as evidence_count
from alerts
full outer join evidence
    on evidence.alert_id = alerts.alert_id
where alerts.transfer_count is distinct from evidence.transfer_count

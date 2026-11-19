"""Write-audit-publish on Delta: dbt builds and tests each layer in its staging schema; the
publication copies the audited rows into the layer readers see (ADR 0006)."""

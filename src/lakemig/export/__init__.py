"""On-premises side of the hybrid boundary (ADR 0003).

A delivery is one business date of every exported object, read at one commit of the source
lakehouse, reduced to the columns the export policy lets cross, checked by the PII gate and
written as Parquet with a manifest. Nothing in this package runs on Databricks.
"""

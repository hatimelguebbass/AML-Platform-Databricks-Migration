from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from lakemig.cost.estimate import Assumptions
from lakemig.cost.prices import UnitPrice
from lakemig.operations import benchmark, evidence

ROOT = Path(__file__).resolve().parents[2]
DEPLOYER = "68305764-9369-40a4-8f89-36f0bea9086d"


def _node(schema: str, name: str, parents: list[str], materialized: str = "table", kind: str = "model") -> Any:
    return {
        "resource_type": kind,
        "schema": schema,
        "name": name,
        "alias": name,
        "config": {"materialized": materialized},
        "depends_on": {"nodes": parents},
    }


MANIFEST = {
    "sources": {
        "source.aml.bronze.payment_transfer": {
            "resource_type": "source",
            "schema": "bronze",
            "name": "payment_transfer",
            "identifier": "payment_transfer",
        }
    },
    "nodes": {
        "seed.aml.iso_currency": {
            **_node("reference_staging", "iso_currency", [], "seed", "seed"),
            "depends_on": {"macros": []},
        },
        "model.aml.payment_transfer": _node(
            "silver_staging", "payment_transfer", ["source.aml.bronze.payment_transfer"]
        ),
        "model.aml.monitored_transfer": _node(
            "gold_staging", "monitored_transfer", ["model.aml.payment_transfer", "seed.aml.iso_currency"], "ephemeral"
        ),
        "model.aml.aml_alert": _node("gold_staging", "aml_alert", ["model.aml.monitored_transfer"]),
        "test.aml.unique": {
            "resource_type": "test",
            "schema": "silver_staging",
            "name": "unique",
            "config": {"materialized": "test"},
            "depends_on": {"nodes": ["model.aml.aml_alert"]},
        },
    },
}


def test_declared_lineage_goes_through_ephemeral_models_and_ends_in_the_published_layer() -> None:
    edges = evidence.declared_lineage(MANIFEST, "aml")

    assert edges == {
        ("aml.bronze.payment_transfer", "aml.silver_staging.payment_transfer"),
        ("aml.silver_staging.payment_transfer", "aml.gold_staging.aml_alert"),
        ("aml.reference_staging.iso_currency", "aml.gold_staging.aml_alert"),
        ("aml.silver_staging.payment_transfer", "aml.silver.payment_transfer"),
        ("aml.gold_staging.aml_alert", "aml.gold.aml_alert"),
        ("aml.reference_staging.iso_currency", "aml.reference.iso_currency"),
    }


def test_lineage_reports_declared_edges_never_recorded() -> None:
    declared = {("a", "b"), ("b", "c")}

    result = evidence.lineage(declared, [("a", "b"), ("x", "c")])

    assert not result.complete
    assert result.missing == ("b -> c",)
    assert result.undeclared == ("x -> c",)
    assert evidence.lineage(declared, [("a", "b"), ("b", "c")]).complete


def test_runs_are_grouped_by_scope_and_timed_on_successes_only() -> None:
    rows = [
        ("aml_daily", "1", "SUCCEEDED", "all", 1000),
        ("aml_daily", "2", "SUCCEEDED", "2022-09-18", 300),
        ("aml_daily", "3", "SUCCEEDED", "2022-09-17", 500),
        ("aml_daily", "4", "FAILED", "2022-09-16", 20),
        ("platform_check", "5", "SUCCEEDED", "", 44),
    ]

    by_key = {(r.job, r.scope): r for r in evidence.runs(rows)}

    one_date = by_key[("aml_daily", "one date")]
    assert (one_date.runs, one_date.succeeded, one_date.failed) == (3, 2, 1)
    assert (one_date.median_seconds, one_date.max_seconds) == (400, 500)
    assert by_key[("aml_daily", "every date")].median_seconds == 1000
    assert by_key[("platform_check", "")].runs == 1


def test_access_names_roles_never_people() -> None:
    changes = (
        '[{"principal": "aml-migration", "add": ["USE_SCHEMA"], "remove": ["ALL_PRIVILEGES"]}, '
        '{"principal": "someone@example.com", "add": ["SELECT"]}]'
    )
    rows = [
        ("2026-11-16T10:00:00Z", DEPLOYER, "getTable", "200", "aml.silver.party", None),
        ("2026-11-16T10:00:01Z", "someone@example.com", "getTable", "403", "aml.bronze.party_member", None),
        ("2026-11-16T10:00:02Z", "someone@example.com", "updatePermissions", "200", "aml.silver", changes),
        ("2026-11-16T10:00:03Z", "System-User", "createTable", "200", "aml.gold.aml_alert", None),
    ]

    counts, permission_changes = evidence.access(rows, DEPLOYER)

    assert {(c.actor, c.kind, c.events, c.denied) for c in counts} == {
        ("deployer service principal (aml-pipeline)", "read", 1, 0),
        ("operator", "read", 1, 1),
        ("operator", "permissions", 1, 0),
        ("Databricks", "write", 1, 0),
    }
    assert [(p.actor, p.securable, p.changes) for p in permission_changes] == [
        ("operator", "aml.silver", "aml-migration: +USE_SCHEMA -ALL_PRIVILEGES; operator: +SELECT")
    ]
    assert "example.com" not in repr(counts) + repr(permission_changes)


def test_measured_usage_is_compared_with_the_estimate_scaled_to_the_window() -> None:
    assumptions = Assumptions.read(ROOT / "migration" / "cost-assumptions.yaml")
    prices = [UnitPrice("jobs_serverless_dbu", "francecentral", "p", "s", "m", "1 Hour", 0.5, "USD", "2026-01-01")]
    report = evidence.Report(
        format_version=1,
        measured_at="2026-11-16T00:00:00+00:00",
        catalog="aml",
        window=("2026-11-15", "2026-11-16"),
        usage=(
            evidence.Usage("JOBS", "PREMIUM_JOBS_SERVERLESS_COMPUTE_FRANCE_CENTRAL", "DBU", 2.0, 1.0, "", ""),
            evidence.Usage(
                "PREDICTIVE_OPTIMIZATION", "PREMIUM_JOBS_SERVERLESS_COMPUTE_FRANCE_CENTRAL", "DBU", 0.5, 0.25, "", ""
            ),
            evidence.Usage("SQL", "PREMIUM_SERVERLESS_SQL_COMPUTE_FRANCE_CENTRAL", "DBU", 1.0, 0.91, "", ""),
        ),
        jobs_cost=(),
        azure=(),
        runs=(),
        lineage=evidence.Lineage(0, 0, (), ()),
        access=(),
    )

    jobs, sql = evidence.against_estimate(report, assumptions, prices, "francecentral")

    assert jobs.measured == 2.5
    # Daily pipeline: 27 once + 1.5 a day; deployments: 0.3 a day; two days.
    assert jobs.estimated.expected == pytest.approx(27 + 2 * 1.5 + 2 * 0.3)
    assert sql.measured == 1.0
    assert sql.estimated.expected == pytest.approx(2 * 6)


def test_benchmark_queries_bypass_the_result_cache_and_refuse_a_cached_answer() -> None:
    spec = benchmark.Spec(
        2, ("silver.t",), (benchmark.Query("q", "purpose", "SELECT count(*) FROM {catalog}.silver.t"),)
    )
    executed: list[str] = []
    metrics: dict[str, Mapping[str, Any]] = {}

    def execute(sql: str) -> str:
        executed.append(sql)
        metrics[str(len(executed))] = {
            "result_from_cache": False,
            "read_files_count": 4,
            "pruned_files_count": 0,
            "read_bytes": 100,
            "rows_read_count": 10,
            "total_time_ms": 100 * len(executed),
        }
        return str(len(executed))

    (result,) = benchmark.measure(spec, "aml", execute, metrics.__getitem__)

    assert len(set(executed)) == 2
    assert all(s.startswith("SELECT '") and s.endswith("count(*) FROM aml.silver.t") for s in executed)
    assert (result.files_read, result.bytes_read, result.duration_ms) == (4, 100, 150)

    with pytest.raises(benchmark.CachedResultError):
        benchmark.measure(spec, "aml", execute, lambda _: {"result_from_cache": True})


def test_the_benchmark_spec_names_the_tables_its_queries_read() -> None:
    spec = benchmark.Spec.read(ROOT / "migration" / "benchmark.yaml")

    for query in spec.queries:
        assert any(f"{{catalog}}.{table}" in query.sql for table in spec.tables), query.name
        assert query.sql.startswith("SELECT ")

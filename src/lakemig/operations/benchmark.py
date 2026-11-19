"""What the warehouse reads for the queries of `migration/benchmark.yaml`, per table layout (ADR 0009).

Bytes and files read are the measure: they follow the layout and nothing else, where durations also
follow the warehouse's cache and load. Every run of a query returns one more column, a fresh value,
so the result cache cannot answer it (the cache ignores comments), and a measurement the query
history reports as cached is refused.
"""

from __future__ import annotations

import json
import statistics
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml

FORMAT_VERSION = 1


@dataclass(frozen=True)
class Query:
    name: str
    purpose: str
    sql: str


@dataclass(frozen=True)
class Spec:
    repetitions: int
    tables: tuple[str, ...]
    queries: tuple[Query, ...]

    @classmethod
    def read(cls, path: Path) -> Spec:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        return cls(
            repetitions=int(raw["repetitions"]),
            tables=tuple(raw["tables"]),
            queries=tuple(Query(q["name"], " ".join(q["purpose"].split()), q["sql"]) for q in raw["queries"]),
        )


@dataclass(frozen=True)
class Layout:
    table: str
    clustering: tuple[str, ...]
    files: int
    bytes: int


@dataclass(frozen=True)
class Measurement:
    query: str
    rows_read: int
    files_read: int
    files_pruned: int
    bytes_read: int
    # Median over the repetitions; the first one may also start the warehouse.
    duration_ms: int


@dataclass(frozen=True)
class Run:
    label: str
    measured_at: str
    catalog: str
    engine_version: str
    layouts: tuple[Layout, ...]
    measurements: tuple[Measurement, ...]


class CachedResultError(RuntimeError):
    """The warehouse answered from its result cache: nothing was read, nothing measured."""


# Runs `statement` and returns its id once it finished.
Execute = Callable[[str], str]
# The query history's metrics of a finished statement.
Metrics = Callable[[str], Mapping[str, Any]]


def measure(spec: Spec, catalog: str, execute: Execute, metrics: Metrics) -> list[Measurement]:
    results = []
    for query in spec.queries:
        runs = []
        for _ in range(spec.repetitions):
            statement_id = execute(_uncached(query.sql.format(catalog=catalog)))
            observed = metrics(statement_id)
            if observed.get("result_from_cache"):
                raise CachedResultError(f"{query.name}: answered from the result cache")
            runs.append(observed)
        # Bytes and files are the same on every repetition; the median duration smooths the start.
        first = runs[0]
        results.append(
            Measurement(
                query=query.name,
                rows_read=int(first.get("rows_read_count") or 0),
                files_read=int(first.get("read_files_count") or 0),
                files_pruned=int(first.get("pruned_files_count") or 0),
                bytes_read=int(first.get("read_bytes") or 0),
                duration_ms=int(statistics.median(int(r.get("total_time_ms") or 0) for r in runs)),
            )
        )
    return results


def _uncached(sql: str) -> str:
    head, select, rest = sql.partition("SELECT ")
    if head or not select:
        raise ValueError(f"a benchmark query starts with SELECT: {sql}")
    return f"SELECT '{uuid.uuid4()}' AS benchmark_run, {rest}"


def layout(detail: Mapping[str, Any], table: str) -> Layout:
    """Files and clustering columns of a table, from its DESCRIBE DETAIL row."""
    clustering = detail.get("clusteringColumns") or "[]"
    return Layout(
        table=table,
        clustering=tuple(json.loads(clustering) if isinstance(clustering, str) else clustering),
        files=int(detail["numFiles"]),
        bytes=int(detail["sizeInBytes"]),
    )


def describe_detail(client: Any, warehouse_id: str, table: str) -> Mapping[str, Any]:
    """DESCRIBE DETAIL of `table` by column name: its columns change across runtime versions."""
    response = client.statement_execution.execute_statement(
        statement=f"DESCRIBE DETAIL {table}", warehouse_id=warehouse_id, wait_timeout="50s"
    )
    if response.result is None or response.manifest is None or response.manifest.schema is None:
        raise RuntimeError(f"DESCRIBE DETAIL {table} returned nothing")
    names = [c.name for c in response.manifest.schema.columns or []]
    return dict(zip(names, (response.result.data_array or [[]])[0], strict=True))


@dataclass(frozen=True)
class Report:
    format_version: int
    runs: tuple[Run, ...]

    def with_run(self, run: Run) -> Report:
        """The report with `run` in place of an earlier run of the same label."""
        return Report(self.format_version, (*(r for r in self.runs if r.label != run.label), run))

    def write(self, path: Path) -> None:
        path.write_text(json.dumps(asdict(self), indent=2) + "\n", encoding="utf-8")

    @classmethod
    def read(cls, path: Path) -> Report:
        if not path.exists():
            return cls(FORMAT_VERSION, ())
        raw = json.loads(path.read_text(encoding="utf-8"))
        runs = tuple(
            Run(
                label=r["label"],
                measured_at=r["measured_at"],
                catalog=r["catalog"],
                engine_version=r["engine_version"],
                layouts=tuple(Layout(x["table"], tuple(x["clustering"]), x["files"], x["bytes"]) for x in r["layouts"]),
                measurements=tuple(Measurement(**m) for m in r["measurements"]),
            )
            for r in raw["runs"]
        )
        return cls(raw["format_version"], runs)


def benchmark_md(report: Report, spec: Spec) -> str:
    lines = [
        "# Liquid clustering, measured",
        "",
        "Generated by `make benchmark` from `migration/benchmark.json` (ADR 0009); do not edit.",
        "",
    ]
    if not report.runs:
        return "\n".join([*lines, "Not measured yet: `make benchmark LABEL=...`.", ""])
    lines += ["## Layouts", "", "| Run | Table | Clustering | Files | MB |", "|---|---|---|---:|---:|"]
    for run in report.runs:
        for t in run.layouts:
            keys = ", ".join(f"`{c}`" for c in t.clustering) or "none"
            lines.append(f"| {run.label} | `{t.table}` | {keys} | {t.files} | {t.bytes / 1_000_000:.1f} |")
    header = "| Query | " + " | ".join(f"{r.label}: files read / pruned | {r.label}: MB read" for r in report.runs)
    lines += ["", "## What each query read", "", header + " |", "|---" + "|---:" * (2 * len(report.runs)) + "|"]
    for query in spec.queries:
        cells = []
        for run in report.runs:
            m = next((m for m in run.measurements if m.query == query.name), None)
            cells += [f"{m.files_read} / {m.files_pruned}", f"{m.bytes_read / 1_000_000:.1f}"] if m else ["not run", ""]
        lines.append(f"| `{query.name}` | " + " | ".join(cells) + " |")
    durations = "| Query | " + " | ".join(f"{r.label}: ms" for r in report.runs) + " |"
    lines += ["", "Median duration of the repetitions, for reference (it follows the warehouse's load too):", ""]
    lines += [durations, "|---" + "|---:" * len(report.runs) + "|"]
    for query in spec.queries:
        cells = [
            str(next((m.duration_ms for m in run.measurements if m.query == query.name), "")) for run in report.runs
        ]
        lines.append(f"| `{query.name}` | " + " | ".join(cells) + " |")
    lines += ["", "## Queries", ""]
    lines += [f"- `{q.name}`: {q.purpose}" for q in spec.queries]
    return "\n".join(lines) + "\n"


def statement_runner(client: Any, warehouse_id: str, catalog: str, poll_seconds: float = 2) -> Execute:
    from databricks.sdk.service.sql import StatementState

    def execute(statement: str) -> str:
        response = client.statement_execution.execute_statement(
            statement=statement, warehouse_id=warehouse_id, catalog=catalog, wait_timeout="50s"
        )
        while response.status is not None and response.status.state in (StatementState.PENDING, StatementState.RUNNING):
            time.sleep(poll_seconds)
            response = client.statement_execution.get_statement(response.statement_id or "")
        if response.status is None or response.status.state != StatementState.SUCCEEDED:
            error = response.status.error.message if response.status and response.status.error else None
            raise RuntimeError(f"{error or 'statement failed'}\n{statement}")
        return str(response.statement_id)

    return execute


def history_metrics(client: Any, warehouse_id: str, attempts: int = 30, poll_seconds: float = 2) -> Metrics:
    from databricks.sdk.service.sql import QueryFilter

    def metrics(statement_id: str) -> Mapping[str, Any]:
        # The history records a statement a few seconds after it finished.
        for _ in range(attempts):
            found = list(
                client.query_history.list(
                    filter_by=QueryFilter(statement_ids=[statement_id], warehouse_ids=[warehouse_id]),
                    include_metrics=True,
                ).res
                or []
            )
            # Metrics arrive in two steps: timings first, then what was read and whether it was cached.
            if found and found[0].metrics is not None and found[0].metrics.result_from_cache is not None:
                return dict(found[0].metrics.as_dict())
            time.sleep(poll_seconds)
        raise TimeoutError(f"statement {statement_id} is not in the query history")

    return metrics

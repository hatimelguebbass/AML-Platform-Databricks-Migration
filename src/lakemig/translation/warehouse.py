"""Statements on a Databricks SQL warehouse through the Statement Execution API."""

from __future__ import annotations

import json
import time
from collections.abc import Callable

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.sql import StatementState

from lakemig.translation.report import Execute

_RUNNING = (StatementState.PENDING, StatementState.RUNNING)


def runner(client: WorkspaceClient, warehouse_id: str, catalog: str, poll_seconds: float = 3) -> Execute:
    def execute(statement: str) -> tuple[list[list[str | None]] | None, str | None]:
        response = client.statement_execution.execute_statement(
            statement=statement, warehouse_id=warehouse_id, catalog=catalog, wait_timeout="50s"
        )
        while response.status is not None and response.status.state in _RUNNING:
            time.sleep(poll_seconds)
            response = client.statement_execution.get_statement(response.statement_id or "")
        status = response.status
        if status is None or status.state != StatementState.SUCCEEDED:
            error = status.error.message if status is not None and status.error is not None else None
            return None, error or f"statement ended {status.state if status else 'without status'}"
        rows = []
        chunk = response.result
        # Large results arrive in chunks: the first one inline, the others fetched by index.
        while chunk is not None:
            rows += chunk.data_array or []
            if chunk.next_chunk_index is None:
                break
            chunk = client.statement_execution.get_statement_result_chunk_n(
                response.statement_id or "", chunk.next_chunk_index
            )
        return [[None if v is None else str(v) for v in row] for row in rows], None

    return execute


def strict(execute: Execute) -> Callable[[str], list[list[str | None]]]:
    """`execute` that raises on a failed statement."""

    def query(statement: str) -> list[list[str | None]]:
        rows, error = execute(statement)
        if rows is None:
            raise RuntimeError(f"{error}\n{statement}")
        return rows

    return query


def engine_version(execute: Execute) -> str:
    rows, error = execute("select current_version()")
    if rows is None:
        raise RuntimeError(f"cannot read the warehouse version: {error}")
    version = json.loads(str(rows[0][0]))
    return f"Databricks SQL {version['dbsql_version']}"

"""
Research Experiment Persistence Layer (Phase 11).

Provides persistence for Strategy Lab experiment configurations, results,
and reproducibility hashes. Reuses the existing project SQLite infrastructure
without interfering with live trading journal or Phase 9 audit tables.
"""
from __future__ import annotations

import abc
import json
import os
import sqlite3
from contextlib import contextmanager
from typing import Optional

from app.strategy_lab.models import ExperimentResult


class ResearchStore(abc.ABC):
    """Abstract interface for storing and retrieving strategy research experiments."""

    @abc.abstractmethod
    def save_experiment(self, result: ExperimentResult) -> None:
        """Persist an experiment result."""
        ...

    @abc.abstractmethod
    def get_experiment(self, experiment_id: str) -> Optional[ExperimentResult]:
        """Retrieve an experiment by its unique ID."""
        ...

    @abc.abstractmethod
    def list_experiments(self, strategy_name: Optional[str] = None, limit: int = 50) -> list[ExperimentResult]:
        """List past experiments, optionally filtered by strategy."""
        ...


class InMemoryResearchStore(ResearchStore):
    """Ephemeral, in-memory store for unit tests and stateless runs."""

    def __init__(self):
        self._experiments: dict[str, ExperimentResult] = {}

    def save_experiment(self, result: ExperimentResult) -> None:
        self._experiments[result.experiment_id] = result

    def get_experiment(self, experiment_id: str) -> Optional[ExperimentResult]:
        return self._experiments.get(experiment_id)

    def list_experiments(self, strategy_name: Optional[str] = None, limit: int = 50) -> list[ExperimentResult]:
        results = list(self._experiments.values())
        if strategy_name:
            results = [r for r in results if r.config.get("strategy_name") == strategy_name]
        return results[:limit]


_RESEARCH_SCHEMA = """
CREATE TABLE IF NOT EXISTS research_experiments (
    experiment_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    strategy_name TEXT NOT NULL,
    status TEXT NOT NULL,
    error TEXT,
    dataset_period TEXT NOT NULL,
    metrics TEXT NOT NULL,
    benchmark TEXT NOT NULL,
    reproducibility_hash TEXT,
    config TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_research_strategy ON research_experiments(strategy_name);
CREATE INDEX IF NOT EXISTS idx_research_created ON research_experiments(created_at);
"""


class SqliteResearchStore(ResearchStore):
    """SQLite-backed store for research experiments.
    
    Can share the database file used by TradeJournal (e.g. data/trading_journal.db)
    while remaining completely partitioned from live trading audit tables.
    """

    def __init__(self, db_path: str = "./data/trading_journal.db"):
        self._path = db_path
        if self._path != ":memory:":
            dirname = os.path.dirname(self._path)
            if dirname:
                os.makedirs(dirname, exist_ok=True)
        self._init_db()

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self._path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_db(self):
        with self._connect() as conn:
            conn.executescript(_RESEARCH_SCHEMA)

    def save_experiment(self, result: ExperimentResult) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO research_experiments (
                    experiment_id, created_at, strategy_name, status, error,
                    dataset_period, metrics, benchmark, reproducibility_hash, config
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    result.experiment_id,
                    result.created_at,
                    result.config.get("strategy_name", "UNKNOWN"),
                    result.status,
                    result.error,
                    json.dumps(result.dataset_period),
                    json.dumps(result.metrics),
                    json.dumps(result.benchmark),
                    result.reproducibility_hash,
                    json.dumps(result.config),
                ),
            )

    def get_experiment(self, experiment_id: str) -> Optional[ExperimentResult]:
        with self._connect() as conn:
            cursor = conn.execute(
                "SELECT * FROM research_experiments WHERE experiment_id = ?",
                (experiment_id,),
            )
            row = cursor.fetchone()
            if not row:
                return None
            return self._row_to_result(row)

    def list_experiments(self, strategy_name: Optional[str] = None, limit: int = 50) -> list[ExperimentResult]:
        with self._connect() as conn:
            if strategy_name:
                cursor = conn.execute(
                    """
                    SELECT * FROM research_experiments
                    WHERE strategy_name = ?
                    ORDER BY created_at DESC LIMIT ?
                    """,
                    (strategy_name, limit),
                )
            else:
                cursor = conn.execute(
                    """
                    SELECT * FROM research_experiments
                    ORDER BY created_at DESC LIMIT ?
                    """,
                    (limit,),
                )
            return [self._row_to_result(row) for row in cursor.fetchall()]

    @staticmethod
    def _row_to_result(row: sqlite3.Row) -> ExperimentResult:
        return ExperimentResult(
            experiment_id=row["experiment_id"],
            created_at=row["created_at"],
            status=row["status"],
            error=row["error"],
            dataset_period=json.loads(row["dataset_period"]),
            metrics=json.loads(row["metrics"]),
            benchmark=json.loads(row["benchmark"]),
            reproducibility_hash=row["reproducibility_hash"],
            config=json.loads(row["config"]),
        )

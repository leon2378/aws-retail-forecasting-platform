"""Serializable SQLite storage for the local reference application.

All inventory, order, effect and outbox changes share one database transaction.
Each operation opens its own connection, so request threads never share cursors.
The AWS adapter uses the same mutation protocol with conditional DynamoDB writes.
"""

from pathlib import Path
from contextlib import closing, contextmanager
import json
import sqlite3

from .errors import Conflict


class SQLiteRepository:
    mode = "local_demo"

    def __init__(self, database="data/orderflow.db"):
        self.path = Path(database).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("CREATE TABLE IF NOT EXISTS workspace (id INTEGER PRIMARY KEY CHECK(id=1), version INTEGER NOT NULL, payload TEXT NOT NULL)")

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=20)
        connection.execute("PRAGMA busy_timeout=20000")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self, state):
        with self._connect() as connection:
            connection.execute("INSERT OR IGNORE INTO workspace VALUES (1, 0, ?)", (self._encode(state),))

    @staticmethod
    def _encode(state):
        return json.dumps(state, allow_nan=False, separators=(",", ":"), sort_keys=True)

    def load(self):
        with self._connect() as connection:
            row = connection.execute("SELECT payload FROM workspace WHERE id=1").fetchone()
        if row is None:
            raise Conflict("not_initialized", "Initialize the OrderFlow workspace first.")
        return json.loads(row[0])

    def transact(self, mutator):
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT payload FROM workspace WHERE id=1").fetchone()
            if row is None:
                raise Conflict("not_initialized", "Initialize the OrderFlow workspace first.")
            state = json.loads(row[0])
            result = mutator(state)
            connection.execute("UPDATE workspace SET version=version+1, payload=? WHERE id=1", (self._encode(state),))
            return result

    def backup(self, destination):
        target = Path(destination).resolve()
        if target == self.path:
            raise ValueError("Choose a backup path different from the live database.")
        if target.exists():
            raise ValueError("Backup destination already exists; choose a new filename.")
        target.parent.mkdir(parents=True, exist_ok=True)
        from .engine import validate_state
        with self._connect() as source, closing(sqlite3.connect(target)) as copy:
            source.backup(copy)
            if copy.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("Backup integrity check failed.")
            row = copy.execute("SELECT payload FROM workspace WHERE id=1").fetchone()
            if row is None:
                raise ValueError("Backup contains no workspace.")
            validate_state(json.loads(row[0]))
        return target

    def restore(self, source):
        candidate = Path(source).resolve()
        if candidate == self.path or not candidate.is_file():
            raise ValueError("Choose an existing backup different from the live database.")
        # Validate schema and domain invariants before the live file is touched.
        from .engine import validate_state
        with closing(sqlite3.connect(f"{candidate.as_uri()}?mode=ro", uri=True)) as copy:
            if copy.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("Backup integrity check failed.")
            row = copy.execute("SELECT payload FROM workspace WHERE id=1").fetchone()
            if row is None:
                raise ValueError("Backup contains no workspace.")
            state = json.loads(row[0])
        validate_state(state)
        self.transact(lambda live: (live.clear(), live.update(state)))
        return self.load()

import json
import os
import sqlite3
from contextlib import closing, contextmanager
from hashlib import sha256
from pathlib import Path
from typing import ContextManager, Protocol


class CheckpointStore(Protocol):
    def lock(self, conversation_id: str) -> ContextManager: ...
    def create(self, conversation_id: str, owner: str, state: dict): ...
    def load(self, conversation_id: str, owner: str) -> dict: ...
    def save(self, conversation_id: str, owner: str, state: dict): ...


class SQLiteCheckpoints:
    """Local POSIX store. The OS releases a conversation lock when its worker dies."""

    def __init__(self, path):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.close(os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600))
        self.locks = self.path.with_name(self.path.name + ".locks")
        self.locks.mkdir(mode=0o700, exist_ok=True)
        with self.connection() as db:
            db.execute("CREATE TABLE IF NOT EXISTS conversations(id TEXT PRIMARY KEY, owner TEXT NOT NULL, state TEXT NOT NULL)")

    @contextmanager
    def connection(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("PRAGMA synchronous=FULL")
            yield db

    @contextmanager
    def lock(self, conversation_id):
        import fcntl
        name = sha256(conversation_id.encode()).hexdigest()
        with os.fdopen(os.open(self.locks / name, os.O_CREAT | os.O_RDWR, 0o600), "r+") as file:
            fcntl.flock(file, fcntl.LOCK_EX)
            yield

    def create(self, conversation_id, owner, state):
        with self.connection() as db:
            db.execute("INSERT INTO conversations VALUES(?,?,?)", (conversation_id, owner, json.dumps(state, allow_nan=False)))

    def load(self, conversation_id, owner):
        with self.connection() as db:
            row = db.execute("SELECT state FROM conversations WHERE id=? AND owner=?", (conversation_id, owner)).fetchone()
        if row is None:
            raise PermissionError("Conversation unavailable for this session")
        return json.loads(row[0])

    def save(self, conversation_id, owner, state):
        with self.connection() as db:
            updated = db.execute("UPDATE conversations SET state=? WHERE id=? AND owner=?",
                                 (json.dumps(state, allow_nan=False), conversation_id, owner))
            if updated.rowcount != 1:
                raise PermissionError("Conversation unavailable for this session")

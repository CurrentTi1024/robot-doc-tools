import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from app.config import CRITERIA, DATABASE


LEASE_SECONDS = 120
MAX_ATTEMPTS = 3


class Conflict(Exception):
    pass


class Repository:
    def __init__(self, database=DATABASE):
        self.database = database
        self.postgres = database.startswith(("postgresql://", "postgres://"))

    @contextmanager
    def transaction(self, write=False):
        if self.postgres:
            import psycopg
            from psycopg.rows import dict_row
            conn = psycopg.connect(self.database, row_factory=dict_row, connect_timeout=5)
        else:
            Path(self.database).parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.database, timeout=15)
            conn.row_factory = sqlite3.Row

        try:
            if write and not self.postgres:
                conn.execute("BEGIN IMMEDIATE")

            def query(sql, *args):
                statement = sql.replace("?", "%s") if self.postgres else sql
                return conn.execute(statement, args)

            yield query
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def initialize(self):
        with self.transaction(write=True) as q:
            if self.postgres:
                q("SELECT pg_advisory_xact_lock(76342820)")
            q("""CREATE TABLE IF NOT EXISTS rw_v2_jobs (
                    id TEXT PRIMARY KEY, operation TEXT NOT NULL, stage TEXT NOT NULL,
                    state TEXT NOT NULL, submission_id TEXT NOT NULL, category TEXT NOT NULL,
                    upload_id TEXT REFERENCES rw_v2_jobs(id), parent_id TEXT REFERENCES rw_v2_jobs(id),
                    profile TEXT, model TEXT, rules TEXT NOT NULL, model_config TEXT NOT NULL,
                    source TEXT, evidence TEXT, result TEXT, error TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0, next_at DOUBLE PRECISION NOT NULL,
                    lease TEXT, lease_until DOUBLE PRECISION,
                    UNIQUE(upload_id, operation, rules, model_config),
                    UNIQUE(parent_id, profile))""")
            q("""CREATE TABLE IF NOT EXISTS rw_v2_judge_scores (
                    judge_run_id TEXT NOT NULL REFERENCES rw_v2_jobs(id),
                    criterion_id TEXT NOT NULL, result TEXT NOT NULL,
                    PRIMARY KEY (judge_run_id, criterion_id))""")
            q("""CREATE INDEX IF NOT EXISTS rw_v2_jobs_due
                 ON rw_v2_jobs(stage, state, next_at, lease_until)""")

    def insert_upload(self, submission_id, category, identifier, source, rules, models):
        with self.transaction(write=True) as q:
            q("""INSERT INTO rw_v2_jobs
                   (id, operation, stage, state, submission_id, category, source, rules, model_config, next_at)
                 VALUES (?, 'checklist', 'document', 'queued', ?, ?, ?, ?, ?, ?)""",
              identifier, submission_id, category, source, rules, models, time.time())

    def insert_scoring(self, uploads, settings):
        results = {}
        with self.transaction(write=True) as q:
            for upload_id in sorted(set(uploads)):
                upload = q("""SELECT * FROM rw_v2_jobs WHERE id=? AND operation='checklist'""",
                           upload_id).fetchone()
                if not upload or not upload["evidence"]:
                    raise Conflict(f"Evidence unavailable for upload {upload_id}")
                if upload["rules"] != settings.rules_digest:
                    raise Conflict(f"Upload {upload_id} was indexed with a different rules or template version")
                now = time.time()
                parent = uuid4().hex
                row = q("""INSERT INTO rw_v2_jobs
                       (id, operation, stage, state, submission_id, category, upload_id, rules, model_config, next_at)
                     VALUES (?, 'scoring', 'summary', 'waiting', ?, ?, ?, ?, ?, ?)
                     ON CONFLICT(upload_id, operation, rules, model_config) DO NOTHING RETURNING id""",
                        parent, upload["submission_id"], upload["category"], upload_id,
                        settings.rules_digest, settings.model_digest, now).fetchone()
                if row:
                    for profile in settings.judges:
                        q("""INSERT INTO rw_v2_jobs
                               (id, operation, stage, state, submission_id, category, parent_id, profile, model,
                                rules, model_config, next_at)
                             VALUES (?, 'judge', 'judge', 'queued', ?, ?, ?, ?, ?, ?, ?, ?)""",
                          uuid4().hex, upload["submission_id"], upload["category"], parent, profile,
                          settings.identity(profile), settings.rules_digest, settings.model_digest, now)
                else:
                    parent = q("""SELECT id FROM rw_v2_jobs
                                  WHERE upload_id=? AND operation='scoring' AND rules=? AND model_config=?""",
                               upload_id, settings.rules_digest, settings.model_digest).fetchone()["id"]
                results[upload_id] = parent
        return [results[upload] for upload in uploads]

    def snapshot(self, identifier, operation):
        with self.transaction() as q:
            row = q("""SELECT id, submission_id, state, stage, result, error, upload_id
                       FROM rw_v2_jobs WHERE id=? AND operation=?""", identifier, operation).fetchone()
            if not row:
                return None
            job = {"job_id": row["id"], "submission_id": row["submission_id"],
                   "status": row["state"], "stage": row["stage"]}
            if row["upload_id"]:
                job["upload_job_id"] = row["upload_id"]
            if row["error"]:
                job["error"] = row["error"]
            if row["result"]:
                job["result"] = json.loads(row["result"])
            elif operation == "scoring":
                runs = self._panel(q, identifier)
                job["judges"] = [{"judge": entry["profile"], "model": entry["model"],
                                  "status": entry["state"], "criteria_done": len(entry["scores"])}
                                 for entry in runs]
            return job

    def claim(self, stages):
        now = time.time()
        options = ", ".join("?" for _ in stages)
        lock = " FOR UPDATE SKIP LOCKED" if self.postgres else ""
        with self.transaction(write=True) as q:
            row = q(f"""SELECT * FROM rw_v2_jobs WHERE stage IN ({options}) AND
                         ((state='queued' AND next_at <= ?) OR (state='running' AND lease_until <= ?))
                         ORDER BY next_at, id LIMIT 1{lock}""", *stages, now, now).fetchone()
            if not row:
                return None
            job = dict(row)
            exhausted = row["attempts"] >= MAX_ATTEMPTS
            job.update(lease=uuid4().hex, attempts=row["attempts"] + (not exhausted),
                       exhausted=exhausted)
            q("""UPDATE rw_v2_jobs SET state='running', attempts=?, lease=?, lease_until=? WHERE id=?""",
              job["attempts"], job["lease"], now + LEASE_SECONDS, job["id"])
            return job

    def renew(self, job):
        with self.transaction(write=True) as q:
            q("""UPDATE rw_v2_jobs SET lease_until=? WHERE id=? AND lease=? AND state='running'""",
              time.time() + LEASE_SECONDS, job["id"], job["lease"])

    def _owned(self, q, job):
        lock = " FOR UPDATE" if self.postgres else ""
        row = q("SELECT state, lease FROM rw_v2_jobs WHERE id=?" + lock, job["id"]).fetchone()
        return row["state"] == "running" and row["lease"] == job["lease"]

    def document_for(self, job):
        with self.transaction() as q:
            identifier = job["id"]
            if job["parent_id"]:
                identifier = q("SELECT upload_id FROM rw_v2_jobs WHERE id=?",
                               job["parent_id"]).fetchone()["upload_id"]
            row = q("SELECT evidence FROM rw_v2_jobs WHERE id=?", identifier).fetchone()
            return json.loads(row["evidence"])

    def existing_scores(self, identifier):
        with self.transaction() as q:
            return {row["criterion_id"] for row in q(
                "SELECT criterion_id FROM rw_v2_judge_scores WHERE judge_run_id=?", identifier
            ).fetchall()}

    def save_score(self, job, result):
        with self.transaction(write=True) as q:
            if not self._owned(q, job):
                return False
            q("""INSERT INTO rw_v2_judge_scores (judge_run_id, criterion_id, result) VALUES (?, ?, ?)
                 ON CONFLICT(judge_run_id, criterion_id) DO NOTHING""",
              job["id"], result["criterion_id"], json.dumps(result, ensure_ascii=False))
        return True

    def panel(self, identifier):
        with self.transaction() as q:
            return self._panel(q, identifier)

    @staticmethod
    def _panel(q, identifier):
        children = q("""SELECT id, profile, model, state, error FROM rw_v2_jobs
                        WHERE parent_id=? ORDER BY profile""", identifier).fetchall()
        panel = []
        for child in children:
            scores = q("SELECT result FROM rw_v2_judge_scores WHERE judge_run_id=?", child["id"]).fetchall()
            panel.append({**dict(child), "scores": {value["criterion_id"]: value for
                          value in (json.loads(s["result"]) for s in scores)}})
        return panel

    def finish(self, job, *, evidence=None, result=None, error=None, retry=True):
        with self.transaction(write=True) as q:
            if job["parent_id"]:
                # Serialize the last child completions against the same parent.
                lock = " FOR UPDATE" if self.postgres else ""
                q("SELECT id FROM rw_v2_jobs WHERE id=?" + lock, job["parent_id"]).fetchone()
            if not self._owned(q, job):
                return
            now = time.time()
            if evidence is not None:
                q("""UPDATE rw_v2_jobs SET evidence=?, stage='checklist', state='queued', attempts=0,
                            error=NULL, lease=NULL, next_at=? WHERE id=?""",
                  json.dumps(evidence, ensure_ascii=False), now, job["id"])
                return
            if error and retry and job["attempts"] < MAX_ATTEMPTS:
                q("""UPDATE rw_v2_jobs SET state='queued', error=?, lease=NULL, next_at=? WHERE id=?""",
                  error, now + 2 ** job["attempts"], job["id"])
                return
            state = "completed" if result is not None or error is None else "failed"
            q("""UPDATE rw_v2_jobs SET state=?, result=?, error=?, lease=NULL WHERE id=?""",
              state, json.dumps(result, ensure_ascii=False) if result is not None else None, error, job["id"])
            if job["parent_id"]:
                pending = q("""SELECT 1 FROM rw_v2_jobs WHERE parent_id=? AND state IN ('queued','running')
                               LIMIT 1""", job["parent_id"]).fetchone()
                if not pending:
                    q("""UPDATE rw_v2_jobs SET state='queued', next_at=?
                         WHERE id=? AND state='waiting'""", now, job["parent_id"])

    def save_panel_result(self, job, result):
        # Audited scores remain visible while the synopsis call retries.
        with self.transaction(write=True) as q:
            if not self._owned(q, job):
                return False
            q("UPDATE rw_v2_jobs SET result=? WHERE id=?",
              json.dumps(result, ensure_ascii=False), job["id"])
        return True

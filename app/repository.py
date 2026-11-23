import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from .config import DATABASE


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
            if write:
                conn.execute("BEGIN IMMEDIATE")
        try:
            def query(sql, *args):
                return conn.execute(sql.replace("?", "%s") if self.postgres else sql, args)
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
                q("SELECT pg_advisory_xact_lock(76342819)")
            q("""CREATE TABLE IF NOT EXISTS submissions (
                   id TEXT PRIMARY KEY, revision INTEGER NOT NULL, category TEXT NOT NULL,
                   digest TEXT NOT NULL, pdf TEXT NOT NULL, evidence TEXT)""")
            q("""CREATE TABLE IF NOT EXISTS jobs (
                   id TEXT PRIMARY KEY, submission_id TEXT NOT NULL REFERENCES submissions(id),
                   revision INTEGER NOT NULL, operation TEXT NOT NULL, rules TEXT NOT NULL,
                   stage TEXT NOT NULL, state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                   next_at DOUBLE PRECISION NOT NULL, lease TEXT, lease_until DOUBLE PRECISION,
                   result TEXT, error TEXT,
                   UNIQUE(submission_id, revision, operation, rules))""")
            q("CREATE INDEX IF NOT EXISTS jobs_due ON jobs(stage, state, next_at, lease_until)")

    def submit(self, identifier, revision, category, digest, pdf, rules):
        with self.transaction(write=True) as q:
            q("""INSERT INTO submissions (id, revision, category, digest, pdf)
                 VALUES (?, ?, ?, ?, ?) ON CONFLICT(id) DO NOTHING""",
              identifier, revision, category, digest, pdf)
            current = self._submission(q, identifier, lock=True)
            if revision < current["revision"]:
                raise Conflict("Revision superseded")
            if revision == current["revision"] and (digest, category) != (current["digest"], current["category"]):
                raise Conflict("Revision already exists with different content or category")
            if revision > current["revision"]:
                q("""UPDATE submissions SET revision=?, category=?, digest=?, pdf=?, evidence=NULL WHERE id=?""",
                  revision, category, digest, pdf, identifier)
                q("""UPDATE jobs SET state='superseded', lease=NULL
                     WHERE submission_id=? AND revision < ?""", identifier, revision)
                return self._add(q, identifier, revision, "checklist", rules, "document")
            stage = "checklist" if current["evidence"] else "document"
            return self._add(q, identifier, revision, "checklist", rules, stage)

    def score(self, entries, rules):
        with self.transaction(write=True) as q:
            for identifier, revision in sorted(set(entries)):
                current = self._submission(q, identifier, lock=True)
                if not current or current["revision"] != revision or not current["evidence"]:
                    raise Conflict("Requested revision must be current with document evidence available")
            return [self._add(q, identifier, revision, "scoring", rules, "scoring")
                    for identifier, revision in entries]

    def _submission(self, q, identifier, lock=False):
        suffix = " FOR UPDATE" if self.postgres and lock else ""
        return q("SELECT * FROM submissions WHERE id=?" + suffix, identifier).fetchone()

    @staticmethod
    def _add(q, identifier, revision, operation, rules, stage):
        q("""INSERT INTO jobs (id, submission_id, revision, operation, rules, stage, state, next_at)
             VALUES (?, ?, ?, ?, ?, ?, 'queued', ?)
             ON CONFLICT(submission_id, revision, operation, rules) DO NOTHING""",
          uuid4().hex, identifier, revision, operation, rules, stage, time.time())
        return q("""SELECT id FROM jobs WHERE submission_id=? AND revision=? AND operation=? AND rules=?""",
                 identifier, revision, operation, rules).fetchone()["id"]

    def job(self, job_id):
        with self.transaction() as q:
            row = q("SELECT id, submission_id, revision, operation, state, error FROM jobs WHERE id=?",
                    job_id).fetchone()
            return dict(row) if row else None

    def retry_failed(self, job_id, rules):
        with self.transaction(write=True) as q:
            job = q("SELECT submission_id, revision, rules FROM jobs WHERE id=?", job_id).fetchone()
            if not job:
                return False
            current = self._submission(q, job["submission_id"], lock=True)
            if current["revision"] != job["revision"] or job["rules"] != rules:
                return False
            return q("""UPDATE jobs SET state='queued', attempts=0, next_at=?,
                        lease=NULL, lease_until=NULL, result=NULL, error=NULL
                        WHERE id=? AND state='failed'""", time.time(), job_id).rowcount == 1

    def result(self, identifier, operation, rules):
        with self.transaction() as q:
            row = q("""SELECT jobs.result FROM submissions JOIN jobs
                       ON jobs.submission_id=submissions.id AND jobs.revision=submissions.revision
                       WHERE submissions.id=? AND jobs.operation=? AND jobs.rules=? AND jobs.state='completed'""",
                    identifier, operation, rules).fetchone()
            return json.loads(row["result"]) if row else None

    def claim(self, stage):
        with self.transaction(write=True) as q:
            now = time.time()
            q("""UPDATE jobs SET state='failed', error='worker_lease_expired', lease=NULL
                 WHERE stage=? AND state='running' AND lease_until <= ? AND attempts >= 3""", stage, now)
            suffix = " FOR UPDATE SKIP LOCKED" if self.postgres else ""
            row = q("""SELECT * FROM jobs WHERE stage=? AND
                       ((state='queued' AND next_at <= ?) OR (state='running' AND lease_until <= ?))
                       ORDER BY next_at, id LIMIT 1""" + suffix, stage, now, now).fetchone()
            if not row:
                return None
            job = dict(row)
            job.update(lease=uuid4().hex, attempts=job["attempts"] + 1)
            q("""UPDATE jobs SET state='running', lease=?, lease_until=?, attempts=? WHERE id=?""",
              job["lease"], now + 900, job["attempts"], job["id"])
            current = self._submission(q, job["submission_id"])
            job["document"] = dict(current) if current["revision"] == job["revision"] else None
            return job

    def finish(self, job, *, evidence=None, result=None, error=None, retry=True):
        with self.transaction(write=True) as q:
            current = self._submission(q, job["submission_id"], lock=True)
            suffix = " FOR UPDATE" if self.postgres else ""
            row = q("SELECT state, lease FROM jobs WHERE id=?" + suffix, job["id"]).fetchone()
            if current["revision"] != job["revision"] or row["state"] != "running" or row["lease"] != job["lease"]:
                return
            if evidence is not None:
                q("UPDATE submissions SET evidence=COALESCE(evidence, ?) WHERE id=?",
                  evidence, job["submission_id"])
                q("""UPDATE jobs SET stage='checklist', state='queued', attempts=0, next_at=?,
                     lease=NULL, error=NULL WHERE id=?""", time.time(), job["id"])
            elif error:
                again = retry and job["attempts"] < 3
                q("UPDATE jobs SET state=?, error=?, lease=NULL, next_at=? WHERE id=?",
                  "queued" if again else "failed", error, time.time() + 2 ** job["attempts"], job["id"])
            else:
                q("UPDATE jobs SET state='completed', result=?, lease=NULL, error=NULL WHERE id=?",
                  json.dumps(result, ensure_ascii=False), job["id"])

    def extend_lease(self, job):
        with self.transaction(write=True) as q:
            q("UPDATE jobs SET lease_until=? WHERE id=? AND state='running' AND lease=?",
              time.time() + 900, job["id"], job["lease"])

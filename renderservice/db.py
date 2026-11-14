import json
import sqlite3
import threading
import time


class Database:
    def __init__(self, path):
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        with self.lock:
            self.conn.execute("pragma journal_mode=wal")
            self.conn.execute(
                "create table if not exists jobs ("
                "id text primary key, created real not null, status text not null, data text not null)"
            )
            self.conn.commit()

    def insert(self, job):
        with self.lock:
            self.conn.execute(
                "insert into jobs (id, created, status, data) values (?, ?, ?, ?)",
                (job["id"], job["created"], job["status"], json.dumps(job, ensure_ascii=False)),
            )
            self.conn.commit()

    def get(self, job_id):
        with self.lock:
            row = self.conn.execute("select data from jobs where id = ?", (job_id,)).fetchone()
        return json.loads(row["data"]) if row else None

    def update(self, job_id, **fields):
        with self.lock:
            job = self.get(job_id)
            if job is None:
                return None
            job.update(fields)
            job["updated"] = time.time()
            self.conn.execute(
                "update jobs set status = ?, data = ? where id = ?",
                (job["status"], json.dumps(job, ensure_ascii=False), job_id),
            )
            self.conn.commit()
            return job

    def delete(self, job_id):
        with self.lock:
            self.conn.execute("delete from jobs where id = ?", (job_id,))
            self.conn.commit()

    def list(self):
        with self.lock:
            rows = self.conn.execute("select data from jobs order by created desc").fetchall()
        return [json.loads(row["data"]) for row in rows]

    def claim_next(self):
        with self.lock:
            queued = [job for job in self.list() if job["status"] == "queued"]
            if not queued:
                return None
            job = min(queued, key=lambda item: item.get("queued_at") or 0)
            return self.update(
                job["id"],
                status="rendering",
                started=time.time(),
                attempts=job.get("attempts", 0) + 1,
            )

    def close(self):
        with self.lock:
            self.conn.close()

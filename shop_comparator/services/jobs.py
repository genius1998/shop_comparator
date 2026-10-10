"""Background jobs with per-shop exclusivity.

A Musinsa job and a KREAM job may run at the same time; two jobs that need
the same shop may not.  Every job has its own id, state, progress, stop
flag and summary, and every log line it writes carries the job id.
"""
import threading
import uuid
from datetime import datetime, timezone

from shop_comparator.core import app_logging as L
from shop_comparator.storage import store
from shop_comparator.core.crawl_errors import CrawlError

ACTIVE = {"running", "stopping"}
_SHOP_KEYS = {"무신사": "musinsa", "musinsa": "musinsa", "KREAM": "kream", "kream": "kream", "POIZON": "poizon"}
STATE_LABEL = {"running": "실행 중", "stopping": "중단 중", "done": "완료", "failed": "실패",
               "cancelled": "중단됨", "partial": "일부 실패", "interrupted": "서버 종료로 중단"}


class Cancelled(Exception):
    pass


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class JobContext:
    def __init__(self, job, manager):
        self._job, self._manager = job, manager
        self.cancel = job["cancel"]
        self.summary = job["summary"]

    @property
    def id(self):
        return self._job["id"]

    def check(self):
        if self.cancel.is_set():
            raise Cancelled()

    def wait(self, seconds):
        """Sleep that ends early (and raises) when the job is stopped."""
        if self.cancel.wait(seconds):
            raise Cancelled()

    def progress(self, stage=None, done=None, total=None):
        with self._manager.lock:
            p = self._job["progress"]
            if stage is not None:
                p["stage"] = stage
            if done is not None:
                p["done"] = done
            if total is not None:
                p["total"] = total

    def count(self, key, amount=1):
        with self._manager.lock:
            self.summary[key] = self.summary.get(key, 0) + amount

    def note(self, key, value):
        with self._manager.lock:
            self.summary[key] = value


class JobManager:
    def __init__(self, persist=True):
        self.lock = threading.RLock()
        self.jobs = {}
        self.order = []
        self.persist = persist

    def _public(self, job):
        return {k: job[k] for k in ("id", "kind", "title", "state", "resources", "startedAt", "finishedAt", "error", "errorKind")} | {
            "stateLabel": STATE_LABEL.get(job["state"], job["state"]),
            "progress": dict(job["progress"]), "summary": dict(job["summary"]),
        }

    def busy_resources(self):
        with self.lock:
            return {r: j["id"] for j in self.jobs.values() if j["state"] in ACTIVE for r in j["resources"]}

    def start(self, kind, title, resources, action):
        """Start `action(ctx)` unless a running job holds one of `resources`.

        Returns (job_id, None) or (None, conflicting job title).
        """
        with self.lock:
            for job in self.jobs.values():
                if job["state"] in ACTIVE and set(job["resources"]) & set(resources):
                    return None, job["title"]
            job = {
                "id": uuid.uuid4().hex[:10], "kind": kind, "title": title, "state": "running",
                "resources": sorted(resources), "startedAt": _now(), "finishedAt": None,
                "error": None, "errorKind": None, "cancel": threading.Event(),
                "progress": {"stage": "", "done": 0, "total": 0}, "summary": {},
            }
            self.jobs[job["id"]] = job
            self.order.append(job["id"])
            for old in self.order[:-40]:
                if self.jobs.get(old, {}).get("state") not in ACTIVE:
                    self.jobs.pop(old, None)
            self.order = [i for i in self.order if i in self.jobs]
        self._save(job)
        threading.Thread(target=self._run, args=(job, action), daemon=True, name=f"job-{job['id']}").start()
        return job["id"], None

    def _run(self, job, action):
        ctx = JobContext(job, self)
        with L.bind(job=job["id"], stage=job["kind"]):
            L.log(f"작업 시작: {job['title']}")
            state, error, kind, shop_label = "done", None, None, None
            try:
                action(ctx)
                if job["cancel"].is_set():
                    state = "cancelled"
                elif job["summary"].get("failed") or job["summary"].get("stageErrors"):
                    state = "partial"
            except Cancelled:
                state = "cancelled"
            except CrawlError as exc:
                state, error, kind = ("cancelled" if exc.code == "cancelled" else "failed"), str(exc), exc.code
                shop_label = exc.service
            except ValueError as exc:
                state, error, kind = "failed", str(exc), "input"
            except Exception as exc:  # unexpected: keep the type, never the payload
                if type(exc).__name__ in ("NoSuchWindowException", "InvalidSessionIdException", "WebDriverException"):
                    state, error, kind = "failed", "작업용 Chrome 창이 닫혀 작업을 끝내지 못했습니다. 창을 닫지 말고 다시 실행해 주세요(저장된 결과는 유지됩니다).", "browser"
                    L.error(error, kind)
                    with self.lock:
                        job.update(state=state, error=error, errorKind=kind, finishedAt=_now())
                    self._save(job)
                    return
                state, error, kind = "failed", f"{type(exc).__name__}: 예기치 않은 오류로 작업을 끝내지 못했습니다.", "internal"
                L.error(f"{type(exc).__name__}: {exc}", "internal")
            with self.lock:
                job.update(state=state, error=L.redact(error) if error else None, errorKind=kind, finishedAt=_now())
            if error and kind != "internal":
                L.error(error, kind, shop=_SHOP_KEYS.get(shop_label))
            L.log(f"작업 종료: {job['title']} · {STATE_LABEL.get(state, state)} · {dict(job['summary'])}",
                  level="info" if state in ("done", "cancelled") else "warning")
        self._save(job)

    def _save(self, job):
        if not self.persist:
            return
        try:
            store.save_job({**self._public(job), "summary": {**job["summary"], "error": job["error"], "errorKind": job["errorKind"]}})
        except Exception:
            pass

    def stop(self, job_id=None):
        """Stop one job, or every running job when no id is given."""
        stopped = []
        with self.lock:
            for job in self.jobs.values():
                if job["state"] == "running" and (job_id is None or job["id"] == job_id):
                    job["state"] = "stopping"
                    job["cancel"].set()
                    stopped.append(job["id"])
        return stopped

    def snapshot(self):
        with self.lock:
            return [self._public(self.jobs[i]) for i in reversed(self.order)]

    def get(self, job_id):
        with self.lock:
            job = self.jobs.get(job_id)
            return self._public(job) if job else None

    def wait(self, job_id, timeout=None):
        for thread in threading.enumerate():
            if thread.name == f"job-{job_id}":
                thread.join(timeout)
        return self.get(job_id)

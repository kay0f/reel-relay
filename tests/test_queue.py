"""The bounded worker queue processes enqueued pipeline jobs."""
import threading

from server import ingest


def test_worker_queue_runs_jobs():
    ingest.start_workers(2)
    done = threading.Event()
    got = []
    ingest.enqueue(lambda v: (got.append(v), done.set()), "job-ran")
    assert done.wait(5), "worker did not run the enqueued job"
    assert got == ["job-ran"]


def test_worker_survives_a_failing_job():
    ingest.start_workers(2)
    done = threading.Event()

    def boom():
        raise RuntimeError("intentional")

    ingest.enqueue(boom)                      # must not kill the worker
    ingest.enqueue(lambda: done.set())        # this one should still run
    assert done.wait(5), "worker died on a failing job"

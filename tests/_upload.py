"""#917 shared test helper: follow an async upload to its terminal state.

POST /admin/upload is a SUBMIT (202 + job handle) since #917 - parse/chunk/embed/index
run on the job worker (LAW 4), exactly like a connector crawl, and progress phases ride
GET /ingest/jobs/{job_id}. Tests that only care about the OUTCOME use this helper; tests
about the submit contract itself assert on the 202 directly.
"""
from __future__ import annotations

import time


def upload_and_wait(client, headers, *, files, data=None, timeout_s: float = 30.0) -> dict:
    """Upload, follow the job to a terminal state, and return a dict shaped like the old
    synchronous response ({external_id, title, acl, chunk_count}) plus {job}: the terminal
    job body. Raises AssertionError on a non-202 submit; a FAILED job is returned, not
    raised - callers assert on job["status"] when they care."""
    r = client.post("/admin/upload", headers=headers, files=files, data=data or {})
    assert r.status_code == 202, (r.status_code, r.text)
    body = r.json()
    job = _wait_job(client, headers, body["poll"], timeout_s=timeout_s)
    out = dict(body)
    out["job"] = job
    if job.get("status") == "succeeded":
        segs = client.get(
            f"/admin/documents/{body['external_id']}/segments", headers=headers)
        out["chunk_count"] = len(segs.json()) if segs.status_code == 200 else None
    return out


def settle(client, resp, *, cookies=None, headers=None, timeout_s: float = 30.0) -> dict:
    """Follow a raw submit response's job to its terminal state and return the job body.
    For a non-202 response (a synchronous guard refusal - 402/413/415/507) returns {} so
    the caller's own status-code assertion stays the whole story."""
    if resp.status_code != 202:
        return {}
    poll = resp.json()["poll"]
    deadline = time.monotonic() + timeout_s
    body: dict = {}
    while time.monotonic() < deadline:
        r = client.get(poll, cookies=cookies, headers=headers)
        assert r.status_code == 200, (r.status_code, r.text)
        body = r.json()
        if body.get("status") in ("succeeded", "failed"):
            return body
        time.sleep(0.05)
    raise AssertionError(f"ingest job did not settle within {timeout_s}s: {body}")


def _wait_job(client, headers, poll_path: str, timeout_s: float = 30.0) -> dict:
    deadline = time.monotonic() + timeout_s
    body: dict = {}
    while time.monotonic() < deadline:
        r = client.get(poll_path, headers=headers)
        assert r.status_code == 200, (r.status_code, r.text)
        body = r.json()
        if body.get("status") in ("succeeded", "failed"):
            return body
        time.sleep(0.05)
    raise AssertionError(f"ingest job did not settle within {timeout_s}s: {body}")

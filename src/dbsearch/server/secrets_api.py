"""POST /secrets - the write-once credential seam (#319, ADR 0010 s3).

The plaintext crosses this boundary exactly once, in a request body, and is never returned
by any path. Read-back is existence plus a four-character hint, which is enough to render
"password is set" without re-serving the credential.

Every route takes the LIVE identity dep (`current_user`), which 403s a `demo:*` principal by
construction - an anonymous demo visitor must never reach a credential store.

`secrets` may be None (no DBSEARCH_SECRET_KEY configured on this deployment, see app.py's
lazy construction). Every route then 503s with `unavailable_reason` instead of the server
refusing to boot - a deployment with self-serve credentials unconfigured still serves every
other feature; only this one surface is unavailable, and the message says why.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from dbsearch.router.secret_handles import format_handle, parse_handle


class PutSecretRequest(BaseModel):
    store_id: str
    field: str
    value: str


def build_secrets_api(secrets, tenant_id: str, current_user,
                      unavailable_reason: "str | None" = None) -> APIRouter:
    api = APIRouter(prefix="/secrets")

    def _require_secrets():
        if secrets is None:
            raise HTTPException(
                status_code=503,
                detail=unavailable_reason
                or "self-serve credentials are not configured on this deployment "
                   "(DBSEARCH_SECRET_KEY is unset)")
        return secrets

    def _owned(handle: str, user: str) -> dict:
        parsed = parse_handle(handle)
        if parsed is None:
            raise HTTPException(status_code=400, detail="malformed secret handle")
        if parsed["tenant"] != tenant_id or parsed["owner"] != user:
            # Same 403 for "not yours" and "does not exist": a different status would let a
            # prober enumerate which handles exist.
            raise HTTPException(status_code=403, detail="not your secret")
        return parsed

    def _name(p: dict) -> str:
        return f"{p['tenant']}/{p['owner']}/{p['store']}/{p['field']}"

    @api.post("")
    def put_secret(req: PutSecretRequest, user: str = Depends(current_user)) -> dict:
        store = _require_secrets()
        if not req.value:
            raise HTTPException(status_code=400, detail="value must not be empty")
        if "/" in req.store_id or "/" in req.field:
            raise HTTPException(status_code=400, detail="store_id and field must not contain '/'")
        try:
            handle = format_handle(tenant_id, user, req.store_id, req.field)
        except ValueError as exc:
            # format_handle's own message never includes req.value, only the offending
            # store_id/field segment - safe to surface directly.
            raise HTTPException(status_code=400, detail=str(exc))
        store.put_secret(_name(parse_handle(handle)), req.value)
        return {"handle": handle,
                "hint": req.value[-4:] if len(req.value) > 4 else "",
                "stored": True}

    @api.get("/{handle:path}")
    def describe(handle: str, user: str = Depends(current_user)) -> dict:
        store = _require_secrets()
        got = store.describe_secret(_name(_owned(handle, user)))
        return got or {"exists": False, "hint": ""}

    @api.delete("/{handle:path}")
    def delete(handle: str, user: str = Depends(current_user)) -> dict:
        store = _require_secrets()
        store.delete_secret(_name(_owned(handle, user)))
        return {"deleted": True}

    return api

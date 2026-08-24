"""EntraIdentity — IdentityPort backed by Microsoft Entra ID via Graph (LAW 2).

Requires: pip install azure-identity requests
Resolves a user's TRANSITIVE directory membership so the security-trim filter is faithful.
Uses POST /users/{oid}/getMemberObjects, which returns all transitive group oids AND the
directory roles and administrative units the user belongs to (#872) — getMemberGroups returns
groups alone, and a SharePoint library ACL'd to a directory role is then invisible to the very
people the source granted it to. Measured on prod, not inferred: see user_auth
.fetch_member_principals for the case that forced this.

Cache the result with a short TTL in front of this (see PERMISSIONS.md) — bounded
staleness is acceptable; erring toward fewer groups is safe (too many is a breach).
"""
from __future__ import annotations

from dbsearch.core.models import DirectoryPrincipal
from dbsearch.ports.base import IdentityPort

_GRAPH = "https://graph.microsoft.com/v1.0"


class EntraIdentity(IdentityPort):
    def __init__(self, tenant_id: str, client_id: str, client_secret: str) -> None:
        from azure.identity import ClientSecretCredential

        self._cred = ClientSecretCredential(tenant_id, client_id, client_secret)

    def _token(self) -> str:
        return self._cred.get_token("https://graph.microsoft.com/.default").token

    def expand_groups(self, user_oid: str) -> list[str]:
        import requests

        resp = requests.post(
            f"{_GRAPH}/users/{user_oid}/getMemberObjects",     # #872 — roles too, not groups alone
            headers={"Authorization": f"Bearer {self._token()}", "Content-Type": "application/json"},
            json={"securityEnabledOnly": False},
            timeout=15,
        )
        resp.raise_for_status()
        return [user_oid, *resp.json().get("value", [])]

    def _page(self, url: str) -> list[dict]:
        """Follow @odata.nextLink. A tenant with more than one page of groups would
        otherwise silently expose only the first ~100 in the ACL picker, and the operator
        would have no way to tell a missing group from a nonexistent one."""
        import requests

        token, out = self._token(), []
        while url:
            resp = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=15)
            resp.raise_for_status()
            body = resp.json()
            out.extend(body.get("value", []))
            url = body.get("@odata.nextLink", "")
        return out

    def list_directory(self) -> list[DirectoryPrincipal]:
        """Groups first (the unit an operator should be ACL-ing to), then users.

        Entries with no usable display name are DROPPED, not emitted with a blank label:
        a nameless row in a picker is a GUID the operator cannot verify, which is exactly
        the paste-a-raw-oid problem this replaces."""
        out: list[DirectoryPrincipal] = []
        for g in self._page(f"{_GRAPH}/groups?$select=id,displayName&$top=999"):
            if g.get("id") and (g.get("displayName") or "").strip():
                out.append(DirectoryPrincipal(oid=g["id"], name=g["displayName"].strip(),
                                              kind="group"))
        for u in self._page(f"{_GRAPH}/users?$select=id,displayName,userPrincipalName&$top=999"):
            label = (u.get("displayName") or u.get("userPrincipalName") or "").strip()
            if u.get("id") and label:
                out.append(DirectoryPrincipal(oid=u["id"], name=label, kind="user"))
        return out

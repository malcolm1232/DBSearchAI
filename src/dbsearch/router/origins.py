"""Human-readable source ORIGIN (#176): map a connector kind + its manifest config
into {system, location} a person can pinpoint. Pure, table-driven — a new connector
adds one entry. Consumed at compose (StoreProfile.origin) and rendered in the
answer's Sources list. Never raises; unknown kinds title-case gracefully.

#728 — WHERE THE VENDOR NAME COMES FROM, which is the whole point of this module.

This label is printed on the provenance card, three lines above the host it describes, and
that card's entire job is to be checkable. So it said "Amazon RDS (PostgreSQL)" directly
above an endpoint ending in `.postgres.database.azure.com`. A reviewer called it the only
element on either surface a sceptical reader could call untrue, and they were right: a
citation that contradicts itself on screen costs the reader their trust in every OTHER
citation, including the ones that are fine.

The cause is that the vendor was read off the KIND — the tile the user happened to click —
and a kind cannot know where a database lives. `rds_postgres` is a perfectly legitimate
choice for any reachable PostgreSQL, and `postgres` has always been usable against RDS; the
old map asserted "Amazon RDS" and "Azure Postgres" respectively, one of which was guaranteed
wrong in each case. So:

    the HOST decides the vendor    — it is the only field that actually knows
    the KIND decides the engine    — which is genuinely all a kind knows

and where the host is present but unrecognised, NO vendor is claimed at all: the label falls
back to the bare engine ("PostgreSQL"). That is the honest reading of a host we can see and
cannot place, and it is the case the old code got most confidently wrong.

Absent a host entirely, the kind's label still stands (SYSTEM below). That is not a loophole:
with no endpoint on screen there is nothing for the label to contradict, and it is the demo
fleet's situation — fixture-backed stores that carry a cloud badge and no host at all.
"""
from __future__ import annotations

import re

_PORT = re.compile(r":\d+$")

#: Fallback label when the config carries NO host to read a vendor off. Kept for the demo
#: fleet and for kinds that have no endpoint by nature (a CSV, a folder, an indexed corpus).
SYSTEM = {
    "azure_sql": "Azure SQL", "postgres": "Azure Postgres", "mysql": "Azure MySQL",
    "synapse": "Azure Synapse", "bigquery": "BigQuery", "redshift": "Redshift",
    "databricks": "Databricks", "cosmos": "Cosmos DB", "cosmos_db": "Cosmos DB",
    "csv": "Local CSV", "local": "Indexed docs", "folder": "Folder",
    "sharepoint": "SharePoint", "sharepoint_graph": "SharePoint", "graph_search": "SharePoint",
    "sharepoint_link": "SharePoint",   # #924: the anonymous-link path is still SharePoint to a reader
    # #770. A public Drive folder link carries no endpoint, so `gdrive` takes the no-host
    # fallback - precisely the case this table exists for, not a way around #728's
    # host-decides-the-vendor rule. Without the entry the last line of system_for
    # title-cased the kind and every Drive citation read "Gdrive", a name the product
    # uses nowhere else.
    "gdrive": "Google Drive",
    # #672. This map is what a person READS under an answer, so a wrong name here is a
    # citation that misattributes the source. #728 moved the decision to the host wherever
    # there is one; these remain the no-host fallback.
    "rds_postgres": "Amazon RDS (PostgreSQL)", "rds_mysql": "Amazon RDS (MySQL)",
}

#: What the KIND legitimately knows: the engine. Never a cloud, never a vendor - those are
#: the host's to say. Used both to compose a vendor label that needs one ("Amazon RDS" hosts
#: several engines) and as the whole label when the host names a cloud we do not recognise.
ENGINE = {
    "azure_sql": "SQL Server", "synapse": "Synapse SQL",
    "postgres": "PostgreSQL", "rds_postgres": "PostgreSQL",
    "mysql": "MySQL", "rds_mysql": "MySQL",
    "redshift": "Redshift", "bigquery": "BigQuery", "databricks": "Databricks",
    "cosmos": "Cosmos DB", "cosmos_db": "Cosmos DB",
}

#: hostname suffix -> the vendor it PROVES. `{engine}` is filled from ENGINE for vendors that
#: host more than one (RDS runs PostgreSQL, MySQL, SQL Server and more, so "Amazon RDS" alone
#: would under-describe it). Matched longest-suffix-first so the specific Azure endpoints beat
#: the general ones rather than depending on dict order.
_HOST_VENDOR = {
    ".postgres.database.azure.com": "Azure Postgres",
    ".mysql.database.azure.com": "Azure MySQL",
    ".mariadb.database.azure.com": "Azure MariaDB",
    ".database.windows.net": "Azure SQL",
    ".sql.azuresynapse.net": "Azure Synapse",
    ".documents.azure.com": "Cosmos DB",
    ".azuredatabricks.net": "Azure Databricks",
    ".cloud.databricks.com": "Databricks",
    ".redshift-serverless.amazonaws.com": "Redshift",
    ".redshift.amazonaws.com": "Redshift",
    ".rds.amazonaws.com": "Amazon RDS ({engine})",
}
_BY_LENGTH = sorted(_HOST_VENDOR.items(), key=lambda kv: -len(kv[0]))

_SQL_KINDS = {"azure_sql", "postgres", "mysql", "synapse", "bigquery",
              "redshift", "databricks", "cosmos", "cosmos_db",
              # #672: without these the origin's `location` falls to the doc-store branch
              # and reads config["site"] - absent on a SQL store - so a correctly composed
              # RDS store would cite a bare title with no host/database behind it.
              "rds_postgres", "rds_mysql"}


def _hostname(raw: str) -> str:
    """The bare hostname out of whatever shape a connector's config carries.

    Real values seen in manifests: `tcp:host,1433` and `host,1433` (SQL Server's port
    comma), `https://acct.documents.azure.com:443/` (Cosmos hands over a URL), and plain
    hostnames. Anything left unparsed simply fails to match a suffix, which is the safe
    direction - an unrecognised host claims no vendor."""
    h = str(raw or "").strip().lower()
    if "//" in h:
        h = h.split("//", 1)[1]     # scheme
    h = h.split("/", 1)[0]          # path
    if h.startswith("tcp:"):
        h = h[4:]                   # SQL Server's protocol prefix - NOT a port, and taking
                                    # it for one left the hostname as the bare word "tcp"
    h = h.split(",", 1)[0]          # SQL Server's ",1433"
    h = _PORT.sub("", h)            # a trailing :port, digits only so IPv6 is left alone
    return h.strip().strip(".")


def host_of(config: dict) -> str:
    """The endpoint field, whichever of the three spellings this connector uses."""
    return config.get("server") or config.get("host") or config.get("account") or ""


def vendor_for(host: str, engine: "str | None") -> "str | None":
    """The vendor the HOST proves, or None if it proves nothing.

    None is a real answer and the caller must not paper over it. "I can see this endpoint
    and I cannot tell you whose cloud it is" is true and useful; guessing from the kind is
    how the label came to contradict the host sitting under it."""
    h = _hostname(host)
    if not h:
        return None
    for suffix, label in _BY_LENGTH:
        if h.endswith(suffix) or h == suffix.lstrip("."):
            return label.replace("{engine}", engine) if "{engine}" in label else label
    return None


def system_for(kind: str, config: dict) -> str:
    """The vendor/engine label printed on the citation. See the module docstring for the rule."""
    engine = ENGINE.get(kind)
    host = host_of(config)
    if host:
        vendor = vendor_for(host, engine or kind.replace("_", " ").title())
        if vendor:
            return vendor
        # A host we can SEE and cannot place. Name the engine and claim no cloud - the kind's
        # guess is exactly what would be wrong here.
        if engine:
            return engine
    return SYSTEM.get(kind) or kind.replace("_", " ").title()


def origin_for(kind: str, config: dict, title: str) -> dict:
    system = system_for(kind, config)
    if kind in _SQL_KINDS:
        host = host_of(config)
        db = config.get("database") or config.get("dataset") or config.get("catalog") or ""
        location = " / ".join(p for p in (host, db) if p) or title
    else:
        location = config.get("site") or config.get("folder_path") or title or ""
    return {"system": system, "location": location}

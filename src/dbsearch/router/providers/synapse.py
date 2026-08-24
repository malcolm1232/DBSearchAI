"""`kind: synapse` — Azure Synapse Analytics (dedicated SQL pool) as a federated pushdown
store (#159, ADR 0007). Synapse's dedicated SQL pool speaks the SAME TDS protocol and T-SQL
dialect as Azure SQL and exposes the SAME INFORMATION_SCHEMA — so this connector REUSES
`AzureSqlEngine` verbatim (connect via pymssql, INFORMATION_SCHEMA introspection, read-only
guard + NL2SQL seam from FederatedSqlStore, serverless-resume retry, Entra-OBO delegated path
via pyodbc token). The only thing that differs from azure_sql is the ENDPOINT shape
(`<workspace>.sql.azuresynapse.net`, database = the dedicated pool name) — which is just the
`server`/`database` config, so nothing engine-level changes.

This is the "warehouse" target on the user's list: big analytics over a dedicated SQL pool,
queried IN PLACE (pushdown, LAW 1). Secrets arrive via ${ENV} in stores.yml, resolved
SERVER-side (LAW 1); pymssql/pyodbc import lazily (LAW 7) — see azure_sql.py.

Note: a Synapse SERVERLESS SQL pool (over ADLS external data) is a different shape — it has
no ordinary base tables to INSERT into and needs external-table/OPENROWSET setup per store —
so this connector targets the DEDICATED pool, where CREATE/INSERT/INFORMATION_SCHEMA behave
like a normal SQL database.
"""
from __future__ import annotations

from dbsearch.router.providers.azure_sql import AzureSqlEngine, AzureSqlProvider


class SynapseProvider(AzureSqlProvider):
    """Azure Synapse dedicated SQL pool — same TDS engine as azure_sql, its own kind so the
    router/catalog/canvas can name it distinctly. config: server/database/user/password
    (${ENV} refs) + optional `tables` allowlist + `aad_user`/delegated path (inherited).

    ONE Synapse-specific difference (else it would be byte-identical to azure_sql): the
    dedicated pool REJECTS the `USE <db>` statement that pymssql issues to select a database,
    so the SQL-login path connects over pyodbc (which selects the db via the connstring
    `Database=`, no USE). We force that by injecting `use_odbc` into the config."""

    kind = "synapse"
    modes = ("pushdown",)       # ADR 0008: queried in place, never copied

    def _make(self, config: dict):
        return super()._make({**config, "use_odbc": config.get("use_odbc", True)})


__all__ = ["SynapseProvider", "AzureSqlEngine"]

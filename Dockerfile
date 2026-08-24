# DBSearch.AI — self-host edition image (REST + GraphQL server).
FROM python:3.11-slim

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src

# `secrets` (#319, ADR 0010 s3, C2 review fix): without this extra, `cryptography` is
# absent, `EncryptedFileSecrets` raises ImportError at boot, app.py catches it and sets
# _secrets = None, and /secrets 503s EVEN WITH DBSEARCH_SECRET_KEY correctly set on the box.
# `azure-sql` (260728): pymssql for the azure_sql service (SQL-auth) path — without it a
# fully-configured azure_sql store probes "unreachable: No module named 'pymssql'".
# `mysql` (260728): pymysql, same story for the mysql pushdown store (psycopg for
# postgres is already in `server`).
# `azure` (260728): azure-identity for GraphSharePointConnector — without it the #148
# consent flow succeeds but /connectors/sharepoint/finish 500s (No module named 'azure').
# `gcp` (#654, 260812): google-cloud-bigquery. THE SAME STORY AS pymssql AND pymysql ABOVE,
# and it reached prod anyway — a bigquery store with project/dataset/require_signin all set
# correctly probed "unreachable: No module named 'google'". Note how much has to be right
# before that error is even reachable: the OAuth client, the consented bigquery scope, the
# vaulted refresh token, the composed store. Everything upstream said "connected".
# `aws` (#666, ADR 0024): boto3 for Redshift Data API + STS. Declared the same day as the
# feature, not discovered by a user (#654's lesson) — and /auth/me's aws_enabled reports
# implementation presence from this very install, so the account panel's Amazon row can
# never offer a key form this image cannot validate.
# `pyodbc` is a BINDING, not a driver: it needs the unixODBC runtime (libodbc.so.2) and a
# registered ODBC driver, neither of which python:3.11-slim ships and neither of which a pip
# extra can express. Without this layer EVERY ODBC path is dead in the built image - `synapse`
# entirely (providers/synapse.py forces use_odbc=True, because a dedicated pool rejects the
# `USE <db>` that pymssql issues), and any azure_sql delegated/Entra-OBO query that routes
# through pyodbc. Found on prod 260821 (#901): a fully-configured synapse store probed
# "libodbc.so.2: cannot open shared object file". A devbox has these system libs already, so
# this can ONLY be caught by testing inside the image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl gnupg ca-certificates \
    && curl -fsSL https://packages.microsoft.com/keys/microsoft.asc \
       | gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/microsoft-prod.gpg] https://packages.microsoft.com/debian/12/prod bookworm main" \
       > /etc/apt/sources.list.d/mssql-release.list \
    && apt-get update \
    && ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql18 unixodbc \
    && apt-get purge -y --auto-remove gnupg \
    && rm -rf /var/lib/apt/lists/*

# `cosmos` (#900, 260821): azure-cosmos. THE SAME STORY AS pymssql, pymysql AND google above,
# and it reached prod anyway - exactly the failure #654's comment describes, on a different
# kind. The canvas offered Cosmos DB as a droppable source and a fully-configured store with a
# valid endpoint, database, container and key probed "No module named 'azure.cosmos'" in 0ms,
# never touching the network. The lesson from #654 was applied to ONE dependency instead of to
# the CLASS; see the guard note below.
#
# GUARD OWED (#900/#901): nothing tests that the image can actually serve every kind the
# palette advertises. Walk the palette's kinds and assert each driver imports INSIDE the built
# image - a devbox venv has every extra installed and will pass while prod fails.
RUN pip install --no-cache-dir '.[server,secrets,azure-sql,mysql,azure,gcp,aws,cosmos]'

EXPOSE 8080
# Default to the real backend; docker-compose sets the env. Override SELFHOST_BACKEND=memory
# to run with no Postgres/Ollama (e.g. a quick demo).
ENV SELFHOST_BACKEND=pgvector-ollama
CMD ["uvicorn", "dbsearch.server.app:app", "--host", "0.0.0.0", "--port", "8080"]

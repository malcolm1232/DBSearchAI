"""#672 - RDS / Aurora are reachable under AWS, and every kind-keyed table knows them.

THE DEFECT was never capability: PostgresEngine has connected to "any PostgreSQL" since
#155, and RDS Postgres is Postgres over TLS. It was that `postgres` lived in a canvas group
labelled Azure while the AWS group offered only Redshift - so the most common thing anyone
means by "our database on AWS" looked unsupported - and that origins.SYSTEM cited every such
store as "Azure Postgres", which is a citation naming the wrong cloud.

THE REAL RISK IN THIS CHANGE is a kind that exists in one table and not another. A new kind
is only as safe as the least-updated map that is keyed by kind, and one of those maps
(SECRET_FIELDS) is a security guard: a kind missing from it accepts a plaintext password
into a durable manifest through a door that looks identical to the guarded one. So the
tests below sweep the REGISTRY rather than assert a hand-written list.

    PYTHONPATH=src python3 tests/selftest_672_rds_aliases.py
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dbsearch.router.origins import SYSTEM, origin_for  # noqa: E402
from dbsearch.router.providers.mysql import MySqlProvider, RdsMySqlProvider  # noqa: E402
from dbsearch.router.providers.postgres import (  # noqa: E402
    PostgresProvider, RdsPostgresProvider,
)
from dbsearch.router.secret_fields import SECRET_FIELDS  # noqa: E402

CANVAS = (ROOT / "src/dbsearch/server/static/js/surfaces/canvas.js").read_text()
CANVAS_CSS = (ROOT / "src/dbsearch/server/static/css/canvas.css").read_text()
ROUTER_API = (ROOT / "src/dbsearch/server/router_api.py").read_text()

RDS_KINDS = ("rds_postgres", "rds_mysql")


def test_the_aliases_share_the_store_machinery_and_add_only_auth():
    """#672 shipped these as pure aliases; ADR 0026 (#814) deliberately forked exactly ONE
    thing - authentication (IAM auth token from the caller's vaulted aws_keys, plus the
    ADR 0022 delegated-introspection surface). Everything else still comes from the
    parent: the wire protocol, the store construction, modes, probe/build."""
    assert issubclass(RdsPostgresProvider, PostgresProvider)
    assert issubclass(RdsMySqlProvider, MySqlProvider)
    assert RdsPostgresProvider.kind == "rds_postgres"
    assert RdsMySqlProvider.kind == "rds_mysql"
    assert RdsPostgresProvider.modes == PostgresProvider.modes
    for name in ("probe", "build"):
        assert getattr(RdsPostgresProvider, name) is getattr(PostgresProvider, name), name
    # the ADR 0026 surface - and the base kinds must NOT have grown it (the Entra rail
    # delegates differently; probe_as there would silently change azure behaviour)
    for cls, base in ((RdsPostgresProvider, PostgresProvider),
                      (RdsMySqlProvider, MySqlProvider)):
        for cap in ("probe_as", "build_as"):
            assert hasattr(cls, cap), f"{cls.__name__} lost its {cap} (ADR 0022/0026)"
            assert not hasattr(base, cap), (
                f"{base.__name__} grew {cap} - the aws_keys surface leaked into the "
                "Entra rail")
    print("  PASS  the RDS kinds share the parent store machinery; only auth is forked")


def test_every_sql_kind_has_a_plaintext_credential_guard():
    """THE ONE THAT MATTERS. SECRET_FIELDS is keyed by kind and is what turns "user pasted a
    password into the wrong box" into a 400 instead of a durable plaintext credential (LAW 6).
    Swept from the KINDS table rather than a hand-written list, so a kind added tomorrow with
    a password field and no guard entry fails here."""
    kinds = re.search(r"const KINDS\s*=\s*\{(.*?)\n  \};", CANVAS, re.S).group(1)
    # every kind whose panel declares a secret:true field must be guarded server-side
    for m in re.finditer(r"^\s*([a-z0-9_]+)\s*:\s*\{label:(.*?)\n", kinds, re.M):
        kind, body = m.group(1), m.group(2)
        if "secret:true" not in body:
            continue
        assert kind in SECRET_FIELDS, (
            f"canvas kind {kind!r} offers a secret field but SECRET_FIELDS has no entry - a "
            "plaintext password would ride into a durable manifest unguarded")
    for k in RDS_KINDS:
        assert SECRET_FIELDS[k] == frozenset({"password"}), SECRET_FIELDS.get(k)
    print("  PASS  every canvas kind with a secret field is guarded server-side")


def test_the_citation_names_the_right_cloud():
    """origins.SYSTEM is what a person READS under an answer. `postgres` -> 'Azure Postgres'
    was already wrong for an RDS host; the whole point of the alias is that the citation
    stops naming a cloud the data is not in."""
    assert SYSTEM["rds_postgres"] == "Amazon RDS (PostgreSQL)"
    assert SYSTEM["rds_mysql"] == "Amazon RDS (MySQL)"
    assert "Azure" not in SYSTEM["rds_postgres"], SYSTEM["rds_postgres"]
    print("  PASS  an RDS store cites Amazon RDS, not Azure")


def test_the_origin_carries_host_and_database_not_a_bare_title():
    """_SQL_KINDS decides which branch builds `location`. Left out, an RDS store falls to the
    document branch, reads config['site'] (absent), and cites a bare title - so the reader
    cannot tell WHICH database answered."""
    o = origin_for("rds_postgres",
                   {"host": "orders.abc123.ap-southeast-1.rds.amazonaws.com",
                    "database": "orders"}, "orders-db")
    assert o["system"] == "Amazon RDS (PostgreSQL)", o
    assert "orders.abc123" in o["location"] and "orders" in o["location"], o
    print("  PASS  the origin pinpoints host / database")


def test_the_canvas_offers_rds_under_aws_and_leads_with_it():
    """The actual defect: the AWS group offered a warehouse and nothing else."""
    row = re.search(r'\{key:"aws".*?\}', CANVAS, re.S).group(0)
    # Parse the kinds ARRAY, not the whole row: the row's `color:"var(--k-redshift)"` puts
    # the substring "redshift" ahead of everything, so a naive row.index() reports the wrong
    # order and fails on correct code. (It did, on the first run of this test.)
    kinds = re.search(r"kinds:\[(.*?)\]", row).group(1)
    listed = [k.strip().strip('"') for k in kinds.split(",")]
    for k in RDS_KINDS:
        assert k in listed, f"{k} is not in the AWS provider group: {listed}"
    assert listed.index("rds_postgres") < listed.index("redshift"), (
        f"Redshift still leads the AWS group - the common case (RDS) should come first: {listed}")
    print("  PASS  AWS offers RDS Postgres, RDS MySQL and Redshift, RDS first")


def test_the_rds_panels_do_not_seed_azure_env_names():
    """#664's lesson, applied before it can be reported: an ${AZURE_PG_HOST} placeholder on
    an AWS panel tells the user the system knows a value it has never heard of. The RDS
    panels carry human placeholders instead."""
    kinds = re.search(r"const KINDS\s*=\s*\{(.*?)\n  \};", CANVAS, re.S).group(1)
    for k in RDS_KINDS:
        body = re.search(rf"^\s*{k}\s*:\s*\{{.*?\n", kinds, re.M | re.S).group(0)
        assert "AZURE" not in body.upper(), f"{k} seeds an Azure env name: {body}"
        assert "${" not in body, (
            f"{k} seeds an ${{ENV}} placeholder; nothing sets AWS DB env vars on a hosted "
            f"box, so it would render as a variable that is never substituted: {body}")
    print("  PASS  the RDS panels carry human placeholders, no Azure env refs")


def test_the_rds_panels_do_not_offer_entra_delegation():
    """require_signin on the Azure rail means 'present an ENTRA token as the password' -
    redeeming a Microsoft credential against Amazon. Since ADR 0026 the RDS kinds are
    _ALWAYS_DELEGATED over aws_keys, so the switch stays absent for the #809 reason too:
    identity is not a choice here, and a switch that does nothing is the hollow-offer
    shape."""
    kinds = re.search(r"const KINDS\s*=\s*\{(.*?)\n  \};", CANVAS, re.S).group(1)
    for k in RDS_KINDS:
        body = re.search(rf"^\s*{k}\s*:\s*\{{.*?\n", kinds, re.M | re.S).group(0)
        assert "require_signin" not in body, (
            f"{k} offers require_signin - an always-delegated aws_keys kind has no "
            f"identity choice, and Entra semantics would be actively wrong here: {body}")
    print("  PASS  no delegation switch is offered where identity is not a choice")


def test_the_kinds_are_registered_and_planned():
    """A kind the canvas offers but the server does not register composes as 'no provider for
    kind' - the tile that always fails (#551)."""
    for k in RDS_KINDS:
        assert f'"{k}"' in ROUTER_API, f"{k} missing from router_api (PLANNED_KINDS)"
    assert "RdsPostgresProvider(" in ROUTER_API and "RdsMySqlProvider(" in ROUTER_API, (
        "the RDS providers are never registered on the registry")
    print("  PASS  both kinds are registered and in PLANNED_KINDS")


def test_the_nodes_have_an_accent_colour():
    """kindColor falls back to --faint for an unknown kind, so a new kind renders colourless
    against every other node on the canvas."""
    for k in RDS_KINDS:
        assert f"--k-{k}:" in CANVAS_CSS, f"--k-{k} is undefined; the node paints --faint"
    print("  PASS  both kinds define a --k-<kind> accent")


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
            except AssertionError as e:
                fails += 1
                print(f"  FAIL {name}: {e}")
    print("\nFAILED" if fails else "\n#672 RDS ALIAS SELF-TEST PASSED.")
    sys.exit(1 if fails else 0)

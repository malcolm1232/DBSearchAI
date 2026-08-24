"""Self-serve connect (#317, ADR 0010): a signed-in user's new connector node must NOT
arrive pre-wired to the SERVER's environment variables.

The defect this pins, observed live on dbsearch.ai as a real signed-in user:

    Cannot check 'azure_sql'.
    could not prepare check: "manifest references unset env var 'AZURE_SQL_SERVER'"

addNode seeded every field whose PLACEHOLDER looked like an env reference with that
reference as a real value, so a self-serve user's first node referenced variables that
exist only on an operator's machine. Operator deployments (dev rig / self-host, where
whoever runs the server does set those vars) keep the prefill.

    python3 tests/selftest_selfserve_connect.py
"""
import os
import re
import sys
from pathlib import Path

os.environ["SELFHOST_BACKEND"] = "memory"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "dbsearch"

from dbsearch.router.provisioning import resolve_env  # noqa: E402


def test_addnode_seeds_only_env_vars_the_server_can_resolve():
    canvas = (SRC / "server/static/js/surfaces/canvas.js").read_text()
    m = re.search(r"function addNode\(kind\)\{(.*?)\n  \}", canvas, re.S)
    assert m, "addNode not found - this test is pinned to it"
    body = m.group(1)

    assert "ENV_PRESENT" in body, \
        "addNode does not consult env_present; it will seed refs the server cannot resolve"
    # #423 (ADR 0011 s4): the seed is now gated on CFG_OPERATOR too - env_present alone
    # answers "can this server resolve it", not "may THIS caller have it". The behavioural
    # intent pinned here is unchanged, because /config reports operator: true on every rig
    # with no real login (a dev/self-host box IS its operator's machine), so the canvas
    # defaults CFG_OPERATOR true exactly where this test's #320 guard cares.
    assert re.search(r"envRef\s*&&\s*CFG_OPERATOR\s*&&\s*ENV_PRESENT\.has", body), \
        "the ${ENV} seed is not gated on the operator flag AND the server having the variable"
    # #320 REGRESSION GUARD: the first attempt gated on !realLoginConfigured(), which broke
    # the local rig - it has a real Entra login AND operator-provisioned AZURE_SQL_*, so a
    # signed-in local user got blank fields and could not connect Azure SQL at all.
    code = "\n".join(ln for ln in body.splitlines() if not ln.strip().startswith("//"))
    assert "realLoginConfigured()" not in code, \
        "addNode gates prefill on login state again - that breaks operator rigs that ALSO " \
        "have a real login (the local demo). Gate on env_present, not on auth."
    assert "ENV_PRESENT=newSet(c.env_present" in canvas.replace(" ", ""), \
        "ENV_PRESENT is never populated from /config"


def test_config_reports_resolvable_env_names_only():
    """Names, never values (LAW 1) - existence is all the prefill decision needs."""
    from dbsearch.server.app import CONNECTOR_ENV_NAMES

    canvas = (SRC / "server/static/js/surfaces/canvas.js").read_text()
    kinds = re.search(r"const KINDS\s*=\s*\{(.*?)\n  \};", canvas, re.S)
    assert kinds, "KINDS block not found"
    used = set(re.findall(r"\$\{([A-Z][A-Z0-9_]+)\}", kinds.group(1)))
    # Auth/delegation config is not a connection field and must not be advertised.
    used -= {n for n in used if n.startswith(("AUTH_", "GOOGLE_"))}
    missing = sorted(used - set(CONNECTOR_ENV_NAMES))
    assert not missing, \
        f"connector placeholders absent from CONNECTOR_ENV_NAMES (will never prefill): {missing}"

    import os
    os.environ["AZURE_SQL_SERVER"] = "probe.example.com"
    os.environ.pop("COSMOS_KEY", None)
    from dbsearch.server.app import config as config_route

    class FakeRequest:
        """Minimal stand-in for fastapi.Request - config() only reads .cookies."""
        cookies: dict = {}

    cfg = config_route(FakeRequest())
    assert "AZURE_SQL_SERVER" in cfg["env_present"], "a set connector var is not reported"
    assert "COSMOS_KEY" not in cfg["env_present"], "an unset var is reported as present"
    assert "probe.example.com" not in json_dumps(cfg), "a VALUE leaked into /config"


def json_dumps(o):
    import json
    return json.dumps(o)


def test_the_three_manifest_value_forms(tmp_env=None):
    """ADR 0010: literal passes through, ${ENV} resolves, an unset ${ENV} still raises.

    The raise is the behaviour that produced the user-visible failure, so it is pinned
    deliberately: the fix is to stop EMITTING these refs for self-serve users, not to make
    an unresolvable reference silently succeed."""
    env = {"KNOWN": "real-value"}

    assert resolve_env("plain-host.example.com", env) == "plain-host.example.com"
    assert resolve_env("${KNOWN}", env) == "real-value"
    assert resolve_env({"server": "${KNOWN}", "db": "sales"}, env) == \
        {"server": "real-value", "db": "sales"}

    try:
        resolve_env("${AZURE_SQL_SERVER}", env)
    except KeyError as exc:
        assert "AZURE_SQL_SERVER" in str(exc), exc
    else:
        raise AssertionError("an unset env ref must raise, not resolve to something")


def test_adr_0010_exists_and_is_accepted():
    """A one-way-door decision (where customer credentials rest) must be written down."""
    adr = ROOT / "docs/ADR/0010-self-serve-connection-credentials.md"
    assert adr.exists(), "ADR 0010 missing"
    text = adr.read_text()
    assert "**Status:** Accepted" in text, "ADR 0010 is not Accepted"
    for token in ("secret://", "SecretsPort", "put_secret"):
        assert token in text, f"ADR 0010 does not specify {token}"


def main():
    print("Self-serve connect (#317/#320 / ADR 0010):")
    test_addnode_seeds_only_env_vars_the_server_can_resolve()
    print("  PASS  prefill gated on env_present, not on auth state (#320 regression guard)")
    test_config_reports_resolvable_env_names_only()
    print("  PASS  /config reports resolvable connector env NAMES, never values")
    test_the_three_manifest_value_forms()
    print("  PASS  literal / ${ENV} / unset-raises behave per ADR 0010")
    test_adr_0010_exists_and_is_accepted()
    print("  PASS  ADR 0010 recorded and Accepted")
    print("\nSELF-SERVE CONNECT SELF-TEST PASSED.")


if __name__ == "__main__":
    main()

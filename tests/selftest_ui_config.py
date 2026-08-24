"""UI config endpoint self-test: the frontend reads /config to render the identity
switcher + edition indicator. Metadata only (LAW 1): flags, usernames, backend — never content.

    python3 tests/selftest_ui_config.py
"""
import os
import sys
from pathlib import Path

os.environ["SELFHOST_BACKEND"] = "memory"
os.environ["DBSEARCH_DEV_AUTH"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fastapi.testclient import TestClient  # noqa: E402

from dbsearch.server.app import app  # noqa: E402

client = TestClient(app)


def main():
    print("UI /config self-test:")
    c = client.get("/config").json()
    assert c["dev_auth"] is True, c
    assert c["edition"] == "self-host", c
    assert c["backend"] == "memory", c
    assert "alice" in c["users"] and "bob" in c["users"], c
    # metadata only — no document content keys leak through
    assert "text" not in c and "citations" not in c, c
    print(f"  PASS  /config (dev) -> {c}")

    # prod mode: dev_auth off -> no switcher list exposed
    os.environ["DBSEARCH_DEV_AUTH"] = "0"
    c2 = client.get("/config").json()
    assert c2["dev_auth"] is False and c2["users"] == [], c2
    os.environ["DBSEARCH_DEV_AUTH"] = "1"   # restore for any later run
    print(f"  PASS  /config (prod) hides users -> {c2}")

    print("\nUI CONFIG SELF-TEST PASSED.")


if __name__ == "__main__":
    main()

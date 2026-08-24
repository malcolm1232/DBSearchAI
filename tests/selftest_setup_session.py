"""Phase C/D C1 (card #116) — SetupSessionService: talk to set up the DB.

Two-phase conversational onboarding over the compose layer, mirroring the #57 draft
agent: chat GATHERS manifest entries (deterministic keyword parser behind an
injectable seam — LLM parser is C3), 'ready' renders the manifest + validation
(unknown kinds, unsupported modes, missing ACLs, #114 id collisions), 'apply'
composes through an injected compose callable — never a side door.

Run: python3 tests/selftest_setup_session.py
"""
import os
import sys
from pathlib import Path

os.environ["SELFHOST_BACKEND"] = "memory"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dbsearch.agents.setup_session import (  # noqa: E402
    SetupSessionService, keyword_entry_parser, llm_entry_parser,
)


def _svc(compose=None, modes_for=None, health=None):
    modes = {"folder": ["index"], "sharepoint": ["index"], "csv": ["pushdown"],
             "local": ["index"], "graph_search": ["native"]}
    return SetupSessionService(
        tenant="acme",
        compose=compose or (lambda manifest, user_oid: {"tenant": manifest["tenant"],
                                                        "stores": [], "skipped": []}),
        modes_for=modes_for or (lambda kind: modes.get(kind, [])),
        health=health,
    )


# ------------------------------------------------------------------ parser

def test_parser_full_entry():
    entries, questions = keyword_entry_parser(
        "plug in the folder at /mnt/contracts containing contracts and litigation, "
        "for the legal team, visible to legal-staff")
    assert len(entries) == 1, entries
    e = entries[0]
    assert e["kind"] == "folder" and e["config"]["path"] == "/mnt/contracts", e
    assert e["business_unit"] == "legal" and e["acl"] == ["legal-staff"], e
    assert "contracts" in e["description"], e
    assert questions == [], questions


def test_parser_asks_for_missing_acl_and_description():
    entries, questions = keyword_entry_parser("add a folder at /mnt/contracts")
    assert len(entries) == 1 and entries[0]["acl"] == [], entries
    assert any("visible" in q for q in questions), questions
    assert any("routing" in q for q in questions), questions


def test_parser_multi_source_and_csv():
    entries, _ = keyword_entry_parser(
        "a folder at /mnt/legal and our sales csv at /data/sales.csv, visible to all-staff")
    kinds = {e["kind"] for e in entries}
    assert kinds == {"folder", "csv"}, entries
    csv = next(e for e in entries if e["kind"] == "csv")
    assert csv["config"]["files"] == ["/data/sales.csv"], csv
    assert all(e["acl"] == ["all-staff"] for e in entries), entries


def test_parser_unrecognised_says_so():
    entries, questions = keyword_entry_parser("hello there")
    assert entries == [] and questions, (entries, questions)


# ------------------------------------------------------------------ session flow

def test_gather_ready_apply_happy_path():
    applied = {}

    def compose(manifest, user_oid):       # #368: compose lands in the CALLER's workspace
        applied.update(manifest)
        return {"tenant": manifest["tenant"],
                "stores": [{"store_id": s["id"], "kind": s["kind"],
                            "freshness": "ingested@t"} for s in manifest["stores"]],
                "skipped": []}

    svc = _svc(compose=compose)
    t1 = svc.turn("admin", "c1", "folder at /mnt/legal containing contracts and case "
                  "files, for the legal team, visible to legal-staff")
    assert t1.state == "gathering" and "legal" in t1.reply, t1.reply

    t2 = svc.turn("admin", "c1", intent="ready")
    assert t2.state == "confirming", t2
    assert t2.manifest["tenant"] == "acme", t2.manifest
    assert t2.manifest["stores"][0]["kind"] == "folder", t2.manifest
    assert all(v["level"] != "error" for v in t2.validation), t2.validation

    t3 = svc.turn("admin", "c1", intent="apply")
    assert t3.state == "applied" and t3.result["stores"], t3.result
    assert applied["stores"][0]["config"]["path"] == "/mnt/legal", applied


def test_apply_echoes_health_verdict_as_the_admin():
    """#130 Phase G scope A: after compose, the chat runs a health check per store —
    AS THE ADMIN (LAW 2) — and echoes the graded verdict into the reply."""
    seen = []

    def health(user_oid, entry):
        seen.append((user_oid, entry["id"]))
        return {"status": "healthy", "summary": "healthy — retrieved a sample",
                "stages": [], "remediation": None}

    def compose(manifest, user_oid):           # echoes composed stores, like /router/compose
        return {"tenant": manifest["tenant"], "skipped": [],
                "stores": [{"store_id": s["id"], "kind": s["kind"],
                            "freshness": "ingested@t"} for s in manifest["stores"]]}

    svc = _svc(compose=compose, health=health)
    svc.turn("admin", "h1", "folder at /mnt/legal containing contracts, visible to legal-staff")
    svc.turn("admin", "h1", intent="ready")
    t = svc.turn("admin", "h1", intent="apply")
    assert t.state == "applied", t
    assert seen and seen[0][0] == "admin", seen           # runs as the calling admin
    assert "healthy" in t.reply.lower(), t.reply          # echoed into chat
    assert t.health and t.health[0]["status"] == "healthy", t.health


def test_apply_without_health_callable_still_composes():
    """Health is optional — no callable injected -> apply behaves exactly as before."""
    svc = _svc()
    svc.turn("admin", "h2", "folder at /mnt/x containing docs, visible to staff")
    svc.turn("admin", "h2", intent="ready")
    t = svc.turn("admin", "h2", intent="apply")
    assert t.state == "applied" and t.health == [], t


def test_followup_acl_and_description_bind_to_pending_entry():
    svc = _svc()
    svc.turn("admin", "c2", "add the folder at /mnt/contracts")
    t = svc.turn("admin", "c2", "visible to legal-staff")
    assert "legal-staff" in t.reply, t.reply
    t2 = svc.turn("admin", "c2", "containing supplier contracts and NDAs")
    assert "supplier contracts" in t2.reply, t2.reply
    m = svc.turn("admin", "c2", intent="ready").manifest
    assert m["stores"][0]["acl"] == ["legal-staff"], m
    assert "supplier contracts" in m["stores"][0]["description"], m


def test_apply_blocked_on_missing_acl():
    called = []
    svc = _svc(compose=lambda m, u: called.append(m) or {})
    svc.turn("admin", "c3", "folder at /mnt/x")
    t = svc.turn("admin", "c3", intent="ready")
    assert any(v["level"] == "error" and "visible" in v["message"] for v in t.validation), t.validation
    t2 = svc.turn("admin", "c3", intent="apply")
    assert t2.state != "applied" and called == [], (t2.state, called)


def test_unknown_cloud_kind_warns_not_blocks():
    svc = _svc()
    svc.turn("admin", "c4", "bigquery project acme-dw, visible to sales-staff")
    t = svc.turn("admin", "c4", intent="ready")
    assert any(v["level"] == "warn" and "SKIP" in v["message"].upper() for v in t.validation), t.validation


def test_id_collision_is_error():
    svc = _svc()
    # two sources whose derived ids collide ('/mnt/legal' and '/opt/legal' -> 'legal')
    svc.turn("admin", "c5", "folder at /mnt/legal, visible to l-staff")
    svc.turn("admin", "c5", "folder at /opt/legal, visible to l-staff")
    t = svc.turn("admin", "c5", intent="ready")
    assert any(v["level"] == "error" and "collision" in v["message"] for v in t.validation), t.validation


def test_verify_runs_suggested_question_after_apply():
    asked = []

    def ask(user, question):
        asked.append((user, question))
        return {"answer": "contracts are stored in /mnt/legal",
                "citations": [{"doc": "d1"}],
                "routing": {"stores": [{"store_id": "legal-folder"}]}}

    svc = _svc(compose=lambda m, u: {"tenant": m["tenant"], "stores": [], "skipped": []})
    svc._ask = ask
    # verify before apply -> nudge, ask never called
    t0 = svc.turn("admin", "c7", intent="verify")
    assert "Apply" in t0.reply and asked == [], t0.reply
    svc.turn("admin", "c7", "folder at /mnt/legal containing supplier contracts, "
             "for the legal team, visible to l-staff")
    svc.turn("admin", "c7", intent="ready")
    svc.turn("admin", "c7", intent="apply")
    t = svc.turn("admin", "c7", intent="verify")
    assert asked and "supplier contracts" in asked[0][1], asked   # suggested from desc
    assert "legal-folder" in t.reply and "1 citation" in t.reply, t.reply
    # the admin's own question wins over the suggestion
    svc.turn("admin", "c7", intent="verify", message="where are the NDAs?")
    assert asked[-1][1] == "where are the NDAs?", asked


def test_sessions_isolated_per_user():
    svc = _svc()
    svc.turn("admin", "c6", "folder at /mnt/x, visible to g")
    other = svc.turn("intruder", "c6", intent="ready")
    assert other.state == "gathering", other.state   # fresh session, nothing gathered


# ------------------------------------------------------------------ C3: LLM parser

class _FakeSetupLlm:
    """Duck-typed stand-in for AnthropicLlm.extract_setup_entries — returns canned raw
    model text (or raises), so the parser wrapper is proven without network/SDK."""

    def __init__(self, raw):
        self.raw = raw
        self.calls = []

    def extract_setup_entries(self, text):
        self.calls.append(text)
        if isinstance(self.raw, Exception):
            raise self.raw
        return self.raw


def test_llm_parser_parses_and_normalizes_model_json():
    raw = ('Here is the extraction:\n```json\n'
           '[{"kind": "Folder", "config": {"path": "/mnt/contracts"}, '
           '"business_unit": "Legal", "acl": "legal-staff", '
           '"description": "supplier contracts"}]\n```')
    llm = _FakeSetupLlm(raw)
    entries, questions = llm_entry_parser(llm)("plug in the contracts folder")
    assert llm.calls == ["plug in the contracts folder"], llm.calls
    assert len(entries) == 1, entries
    e = entries[0]
    assert e["kind"] == "folder", e                       # lowercased
    assert e["id"] == "contracts", e                      # slug derived from the path
    assert e["acl"] == ["legal-staff"], e                 # bare string coerced to list
    assert e["business_unit"] == "legal", e
    assert e["config"] == {"path": "/mnt/contracts"}, e
    assert e["title"] and e["description"] == "supplier contracts", e
    assert questions == [], questions                     # nothing missing -> no asks


def test_llm_parser_asks_for_missing_acl_and_description():
    raw = '[{"kind": "folder", "id": "hr-docs", "config": {"path": "/mnt/hr"}}]'
    entries, questions = llm_entry_parser(_FakeSetupLlm(raw))("add the hr folder")
    e = entries[0]
    assert e["acl"] == [] and e["business_unit"] == "general", e
    # the SAME asks the keyword parser makes (shared question builder)
    assert any("visible to" in q for q in questions), questions
    assert any("containing" in q for q in questions), questions


def test_llm_parser_falls_back_on_garbage_and_errors():
    # refusal / non-JSON reply -> keyword fallback still extracts the folder
    entries, _ = llm_entry_parser(_FakeSetupLlm("I cannot parse that, sorry."))(
        "add a folder at /mnt/contracts")
    assert entries and entries[0]["config"]["path"] == "/mnt/contracts", entries
    # API explosion -> same fallback, never a user-facing crash
    entries, _ = llm_entry_parser(_FakeSetupLlm(RuntimeError("api down")))(
        "add a folder at /mnt/contracts")
    assert entries and entries[0]["kind"] == "folder", entries


def test_llm_parser_drops_kindless_and_avoids_id_bu_collision():
    raw = ('[{"id": "junk", "kind": "", "config": {}}, "not-a-dict", '
           '{"kind": "bigquery", "id": "sales", "business_unit": "sales", '
           '"acl": ["sales-team"], "description": "revenue numbers", '
           '"config": {"project": "p"}}]')
    entries, questions = llm_entry_parser(_FakeSetupLlm(raw))("bigquery for sales")
    assert len(entries) == 1, entries
    assert entries[0]["id"] == "sales-bigquery", entries  # #114 id==BU self-collision avoided
    assert questions == [], questions


def test_llm_parser_empty_extraction_asks_for_help():
    entries, questions = llm_entry_parser(_FakeSetupLlm("[]"))("hello there")
    assert entries == [], entries
    assert questions and "folders" in questions[0], questions   # the _HELP nudge


def test_service_runs_end_to_end_on_llm_parser():
    raw = ('[{"kind": "folder", "config": {"path": "/mnt/legal"}, "business_unit": "legal", '
           '"acl": ["l-staff"], "description": "supplier contracts"}]')
    composed = []
    svc = SetupSessionService(
        tenant="acme",
        compose=lambda m, u: composed.append(m) or {"tenant": m["tenant"], "stores": [],
                                                    "skipped": []},
        modes_for=lambda kind: {"folder": ["index"]}.get(kind, []),
        entry_parser=llm_entry_parser(_FakeSetupLlm(raw)))
    svc.turn("admin", "c8", "plug in the legal folder")
    t = svc.turn("admin", "c8", intent="ready")
    assert t.state == "confirming" and not [v for v in t.validation
                                            if v["level"] == "error"], t.validation
    t = svc.turn("admin", "c8", intent="apply")
    assert t.state == "applied" and composed, (t.state, composed)
    # /mnt/legal slugs to 'legal' == the BU -> the #114 rule renames it on the way in
    assert composed[0]["stores"][0]["id"] == "legal-folder", composed


def main():
    print("Phase C/D C1 setup-session self-test:")
    test_parser_full_entry()
    test_parser_asks_for_missing_acl_and_description()
    test_parser_multi_source_and_csv()
    test_parser_unrecognised_says_so()
    print("  PASS  keyword entry parser (full/missing-acl+desc/multi/unrecognised)")
    test_gather_ready_apply_happy_path()
    test_followup_acl_and_description_bind_to_pending_entry()
    print("  PASS  gather -> ready -> apply + follow-up ACL/description binding")
    test_apply_echoes_health_verdict_as_the_admin()
    test_apply_without_health_callable_still_composes()
    print("  PASS  #130 apply echoes per-store health verdict (as admin) / optional")
    test_apply_blocked_on_missing_acl()
    test_unknown_cloud_kind_warns_not_blocks()
    test_id_collision_is_error()
    print("  PASS  default-deny ACL gate / honest cloud-kind warn / #114 collision error")
    test_verify_runs_suggested_question_after_apply()
    print("  PASS  guided verification (phase D): suggested + own question, gated on apply")
    test_sessions_isolated_per_user()
    print("  PASS  per-user session isolation")
    test_llm_parser_parses_and_normalizes_model_json()
    test_llm_parser_asks_for_missing_acl_and_description()
    test_llm_parser_falls_back_on_garbage_and_errors()
    test_llm_parser_drops_kindless_and_avoids_id_bu_collision()
    test_llm_parser_empty_extraction_asks_for_help()
    test_service_runs_end_to_end_on_llm_parser()
    print("  PASS  C3 LLM entry parser (normalize/asks/fallback/#114/e2e)")
    print("\nC1+C3 SETUP-SESSION SELF-TEST PASSED.")


if __name__ == "__main__":
    main()

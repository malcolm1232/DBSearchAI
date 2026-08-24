"""Conversational setup — "talk to set up the DB" (Phase C/D, card #116, slice C1).

A sibling of DraftSessionService (#57) over the COMPOSE layer instead of retrieval:

    GATHERING ──"ready"──► CONFIRMING ──"apply"──► APPLIED
        ▲                      │
        └──────"cancel"────────┘

  - GATHERING : each admin message is parsed into candidate manifest ENTRIES
                (kind/id/bu/acl/config) by the injectable `entry_parser` — the
                deterministic keyword default here; an LLM parser is a drop-in (C3).
  - "ready"   : renders the accumulated entries as the stores.yml-shaped manifest
                plus a VALIDATION report (unknown kinds, unsupported modes, missing
                ACLs, #114 id-namespace collisions) — the same rules compose
                enforces, surfaced early so the confirm card is honest.
  - "apply"   : composes through the injected `compose` callable — the SAME
                /router/compose path, never a side door. Refused while any
                error-level issue stands (LAW 2 default-deny: no entry composes
                without an ACL).

State is in-memory keyed by (conv_id, user_oid) — a guessed conv_id under another
identity misses and starts fresh (no cross-user bleed), exactly like the draft agent.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Callable, Optional

GATHERING, CONFIRMING, APPLIED = "gathering", "confirming", "applied"

EntryParser = Callable[[str], "tuple[list[dict], list[str]]"]
# (manifest, user_oid) -> compose result dict. #368: the user is part of the call because
# compose lands in THAT user's workspace, never in one process-wide catalog.
ComposeFn = Callable[[dict, str], dict]
ModesFor = Callable[[str], "list[str]"]

# kinds the parser recognises — includes not-yet-real cloud kinds so they can be
# captured and HONESTLY warned about (compose will skip them), not silently ignored.
_KIND_WORDS = [
    ("azure sql", "azure_sql"), ("graph search", "graph_search"),
    ("sharepoint", "sharepoint"), ("bigquery", "bigquery"), ("redshift", "redshift"),
    ("postgres", "postgres"), ("folder", "folder"), ("directory", "folder"),
    ("file share", "folder"), ("csv", "csv"), ("spreadsheet", "csv"),
]
_PATH = re.compile(r"(/[\w~./-]+)")
_ACL = re.compile(r"visible to ([\w, -]+)", re.I)
_BU = re.compile(r"for (?:the )?([\w-]+) (?:team|unit|department|bu)\b", re.I)
_PROJECT = re.compile(r"project ([\w-]+)", re.I)
# routing signal: "containing invoices and travel policies" / "with expense claims"
_DESC = re.compile(r"\b(?:containing|holding|with|full of|about)\s+([^,;.]+)", re.I)
_HELP = ("I can plug in folders, csv files, SharePoint, and (soon) cloud warehouses. "
         "Describe a source — e.g. 'folder at /mnt/contracts for the legal team, "
         "visible to legal-staff'.")


def _slug(text: str) -> str:
    base = text.rstrip("/").rsplit("/", 1)[-1].rsplit(".", 1)[0]
    return re.sub(r"[^a-z0-9]+", "-", base.lower()).strip("-") or "source"


def keyword_entry_parser(text: str) -> "tuple[list[dict], list[str]]":
    """Deterministic default parser: explicit patterns only, ASKS when unsure.
    ACL / business-unit clauses in a message apply to every source it mentions."""
    acl = [g.strip() for g in _ACL.search(text).group(1).split(",")] if _ACL.search(text) else []
    bu_m = _BU.search(text)
    bu = bu_m.group(1).lower() if bu_m else "general"
    desc_m = _DESC.search(_ACL.sub("", text))
    desc = desc_m.group(1).strip() if desc_m else ""

    entries: list[dict] = []
    fragments = re.split(r";|\band\b", text)
    for frag in fragments:
        low = frag.lower()
        kind = next((k for word, k in _KIND_WORDS if word in low), None)
        if kind is None:
            continue
        path_m = _PATH.search(frag)
        config: dict = {}
        if kind == "folder":
            if not path_m:
                continue                       # a folder without a path isn't actionable
            config["path"] = path_m.group(1)
            eid = _slug(config["path"])
        elif kind == "csv":
            if path_m:
                config["files"] = [path_m.group(1)]
                eid = _slug(path_m.group(1))
            else:
                eid = "csv-data"
        elif kind == "bigquery":
            proj = _PROJECT.search(frag)
            if proj:
                config["project"] = proj.group(1)
            eid = _slug(config.get("project", "warehouse"))
        else:
            eid = f"{kind}-source"
        if eid == bu:
            eid = f"{eid}-{kind}"     # avoid a self-inflicted #114 id/BU collision
        entries.append({"id": eid, "kind": kind, "business_unit": bu,
                        "acl": list(acl), "title": eid, "description": desc,
                        "config": config})

    return entries, _questions_for(entries)


def _questions_for(entries: list[dict]) -> list[str]:
    """The deterministic asks BOTH parsers share — an LLM extraction never gets to skip
    the ACL question (LAW 2) or the routing-description question."""
    questions = [f"Who should see '{e['id']}'? (say: visible to <group>)"
                 for e in entries if not e["acl"]]
    # the description is the ROUTING signal — a store with no words is unroutable
    questions += [f"What lives in '{e['id']}'? A few keywords help routing "
                  f"(say: containing <topics>)."
                  for e in entries if not e["description"]]
    if not entries:
        questions.append(_HELP)
    return questions


_KINDS = {k for _, k in _KIND_WORDS}


def _json_array(raw: str) -> list:
    start, end = raw.find("["), raw.rfind("]")
    if start < 0 or end <= start:
        raise ValueError(f"no JSON array in model output: {raw[:80]!r}")
    data = json.loads(raw[start:end + 1])
    if not isinstance(data, list):
        raise ValueError("model output is not a JSON array")
    return data


def _normalize_entries(items: list) -> list[dict]:
    """Coerce model-proposed entries into the exact shape the keyword parser emits.
    Deterministic and strict: unknown/kindless items are dropped (not guessed), a bare
    acl string becomes a list, a missing id is derived from the config, and an id==BU
    self-collision is renamed (#114 — one id namespace)."""
    entries: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "").strip().lower()
        if kind not in _KINDS:
            continue
        config = item.get("config") if isinstance(item.get("config"), dict) else {}
        bu = str(item.get("business_unit") or "general").strip().lower() or "general"
        acl = item.get("acl") or []
        if isinstance(acl, str):
            acl = [acl]
        acl = [str(g).strip() for g in acl if str(g).strip()]
        eid = str(item.get("id") or "").strip().lower()
        if not eid:
            files = config.get("files") or [""]
            eid = _slug(str(config.get("path") or files[0] or kind))
        eid = re.sub(r"[^a-z0-9]+", "-", eid).strip("-") or f"{kind}-source"
        if eid == bu:
            eid = f"{eid}-{kind}"
        entries.append({"id": eid, "kind": kind, "business_unit": bu, "acl": acl,
                        "title": str(item.get("title") or eid),
                        "description": str(item.get("description") or "").strip(),
                        "config": config})
    return entries


def llm_entry_parser(llm, fallback: EntryParser = keyword_entry_parser) -> EntryParser:
    """C3 (#116): the LLM (Haiku) entry parser, a drop-in behind the SAME seam.

    The model only PROPOSES entries (raw JSON via `llm.extract_setup_entries`); everything
    that matters stays deterministic here — normalisation, the #114 id rules, and the
    follow-up asks (`_questions_for`), so a hallucinated ACL can't slip through unasked
    and a missing one is always chased (LAW 2). Any model failure — refusal, bad JSON,
    API error — falls back to the keyword parser: setup never breaks with the key absent
    or the API down. An honest empty extraction ([]) is NOT a failure."""
    def parse(text: str) -> "tuple[list[dict], list[str]]":
        try:
            entries = _normalize_entries(_json_array(llm.extract_setup_entries(text)))
        except Exception:
            return fallback(text)
        return entries, _questions_for(entries)
    return parse


@dataclass
class _Session:
    state: str = GATHERING
    entries: list[dict] = field(default_factory=list)


@dataclass
class SetupTurn:
    state: str
    reply: str = ""
    manifest: "dict | None" = None                     # at CONFIRMING
    validation: list[dict] = field(default_factory=list)
    result: "dict | None" = None                       # compose response at APPLIED
    health: list[dict] = field(default_factory=list)   # #130 per-store verdicts at APPLIED

    def to_dict(self) -> dict:
        return {"state": self.state, "reply": self.reply, "manifest": self.manifest,
                "validation": self.validation, "result": self.result,
                "health": self.health}


AskFn = Callable[[str, str], dict]     # (user_oid, question) -> RouterResult-shaped dict
HealthFn = Callable[[str, dict], dict]  # (user_oid, entry) -> HealthVerdict-shaped dict (#130)


class SetupSessionService:
    def __init__(self, tenant: str, compose: ComposeFn, modes_for: ModesFor,
                 entry_parser: Optional[EntryParser] = None,
                 ask: Optional[AskFn] = None,
                 health: Optional[HealthFn] = None) -> None:
        self._tenant = tenant
        self._compose = compose
        self._modes_for = modes_for
        self._parse = entry_parser or keyword_entry_parser
        self._ask = ask
        self._health = health
        self._sessions: dict[tuple[str, str], _Session] = {}

    # ------------------------------------------------------------------ turns

    def turn(self, user_oid: str, conv_id: str, message: str = "",
             intent: str = "chat") -> SetupTurn:
        sess = self._sessions.setdefault((conv_id, user_oid), _Session())

        if intent == "apply":
            return self._apply(sess, user_oid)
        if intent == "ready":
            return self._ready(sess, message)
        if intent == "verify":
            return self._verify(sess, user_oid, message)
        if intent == "cancel":
            sess.state = GATHERING
            return SetupTurn(state=GATHERING, reply="Okay — what should change?")
        return self._chat(sess, message)

    # ------------------------------------------------------------------ D: verify

    def _verify(self, sess: _Session, user_oid: str, message: str) -> SetupTurn:
        """Phase D: the guided 'ask it something!' loop right after apply — prove the
        federation the admin just talked into existence actually answers. The question
        is the admin's own, or suggested from the gathered routing descriptions. Runs
        through the injected ask (the REAL routed /router/ask path, caller-trimmed)."""
        if sess.state != APPLIED:
            return SetupTurn(state=sess.state,
                             reply="Apply the manifest first — then we can test it.")
        if self._ask is None:
            return SetupTurn(state=APPLIED, reply="Verification isn't wired here.")
        question = (message or "").strip() or self._suggest(sess)
        result = self._ask(user_oid, question)
        cites = len(result.get("citations", []))
        routed = ", ".join(s["store_id"] for s in
                           result.get("routing", {}).get("stores", [])) or "no store"
        reply = (f"Q: {question}\n→ {result.get('answer', '')}\n"
                 f"(routed to {routed}; {cites} citation{'s' if cites != 1 else ''})")
        if result.get("disclosure"):
            reply += f"\n⚠ {result['disclosure']}"
        return SetupTurn(state=APPLIED, reply=reply, result=result)

    def _suggest(self, sess: _Session) -> str:
        described = next((e for e in sess.entries if e.get("description")), None)
        if described is None:
            return "what do we have?"
        topic = " ".join(described["description"].split()[:4])
        return f"what do we know about {topic}?"

    # ------------------------------------------------------------------ phases

    def _chat(self, sess: _Session, message: str) -> SetupTurn:
        sess.state = GATHERING
        text = (message or "").strip()
        if not text:
            return SetupTurn(state=GATHERING, reply=_HELP)

        entries, questions = self._parse(text)
        if not entries and sess.entries:
            # a follow-up with no new source: an ACL clause binds to entries still
            # missing one; otherwise the text becomes the ROUTING DESCRIPTION of the
            # first entry that lacks it (the agent just asked one of these questions).
            acl_m = _ACL.search(text)
            if acl_m:
                acl = [g.strip() for g in acl_m.group(1).split(",")]
                bound = [e for e in sess.entries if not e["acl"]]
                for e in bound:
                    e["acl"] = list(acl)
                if bound:
                    named = ", ".join(e["id"] for e in bound)
                    return SetupTurn(state=GATHERING,
                                     reply=f"Done — {named} now visible to "
                                           f"{', '.join(acl)}. Say 'ready' to review.")
            missing_desc = next((e for e in sess.entries if not e["description"]), None)
            if missing_desc is not None:
                desc_m = _DESC.search(text)
                missing_desc["description"] = (desc_m.group(1).strip() if desc_m
                                               else text.strip(" .,"))
                return SetupTurn(state=GATHERING,
                                 reply=f"Noted — '{missing_desc['id']}' is about: "
                                       f"{missing_desc['description']}. Say 'ready' to review.")
        sess.entries.extend(entries)
        bits = [f"Added {e['id']} ({e['kind']})." for e in entries]
        open_qs = [q for q in questions if q != _HELP]
        if not entries:
            reply = _HELP
        elif open_qs:
            reply = " ".join(bits + open_qs)
        else:
            reply = " ".join(bits + ["Anything else? Say 'ready' to review the manifest."])
        return SetupTurn(state=GATHERING, reply=reply)

    def _manifest(self, sess: _Session) -> dict:
        return {"tenant": self._tenant, "stores": [dict(e) for e in sess.entries]}

    def _ready(self, sess: _Session, message: str) -> SetupTurn:
        if (message or "").strip():
            self._chat(sess, message)          # fold unsent text in first
        if not sess.entries:
            sess.state = GATHERING
            return SetupTurn(state=GATHERING, reply="Nothing to review yet. " + _HELP)
        manifest = self._manifest(sess)
        validation = self._validate(manifest)
        sess.state = CONFIRMING
        return SetupTurn(state=CONFIRMING, manifest=manifest, validation=validation)

    # #368: `user_oid` is REQUIRED. It used to default to "" - harmless when it only
    # mis-attributed the health echo, but it now selects the workspace compose lands in,
    # and "" is a real (wrong, shared) workspace key. The sole caller always passes it.
    def _apply(self, sess: _Session, user_oid: str) -> SetupTurn:
        if sess.state != CONFIRMING or not sess.entries:
            sess.state = GATHERING
            return SetupTurn(state=GATHERING,
                             reply="Let's gather the sources first — then 'ready', then apply.")
        manifest = self._manifest(sess)
        validation = self._validate(manifest)
        errors = [v for v in validation if v["level"] == "error"]
        if errors:
            return SetupTurn(state=CONFIRMING, manifest=manifest, validation=validation,
                             reply="Can't apply yet: " +
                                   " ".join(v["message"] for v in errors))
        # the SAME /router/compose path, into the SAME (#368) per-user workspace
        result = self._compose(manifest, user_oid)
        sess.state = APPLIED
        health, reply = self._health_echo(user_oid, manifest, result)
        return SetupTurn(state=APPLIED, manifest=manifest, validation=validation,
                         result=result, health=health, reply=reply)

    def _health_echo(self, user_oid: str, manifest: dict,
                     result: dict) -> "tuple[list[dict], str]":
        """#130: after compose, run a round-trip health check per composed store — AS THE
        admin (LAW 2) — and fold a one-line-per-store verdict into the chat reply."""
        base = "Composed — your sources are live."
        if self._health is None:
            return [], base
        composed = {s["store_id"] for s in (result or {}).get("stores", [])}
        icon = {"healthy": "✓", "degraded": "⚠", "failed": "✗"}
        verdicts, lines = [], []
        for entry in manifest["stores"]:
            if entry["id"] not in composed:
                continue                       # a skipped (no-provider) store has nothing to check
            try:
                v = self._health(user_oid, entry)
            except Exception as exc:           # a health probe must never break apply
                v = {"status": "failed", "summary": f"health check errored: {exc}",
                     "stages": [], "remediation": str(exc)}
            verdicts.append(v)
            lines.append(f"{icon.get(v['status'], '·')} {entry['id']}: "
                         f"{v.get('summary') or v['status']}")
        reply = base + ("\n" + "\n".join(lines) if lines else "")
        return verdicts, reply

    # ------------------------------------------------------------------ validation

    def _validate(self, manifest: dict) -> list[dict]:
        """The same rules compose enforces, surfaced EARLY (honest confirm card)."""
        issues: list[dict] = []
        entries = manifest["stores"]
        # #114: tenant/BU/source/store ids share one namespace
        ids = [manifest["tenant"]] + sorted({e["business_unit"] for e in entries})
        for e in entries:
            ids += [e["id"], f"{e['id']}--src"]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            issues.append({"id": ",".join(dupes), "level": "error",
                           "message": f"id collision across tenant/BU/store namespace: {dupes} "
                                      "(#114 — rename a source)"})
        for e in entries:
            modes = self._modes_for(e["kind"])
            if not modes:
                issues.append({"id": e["id"], "level": "warn",
                               "message": f"kind '{e['kind']}' has no live provider yet — "
                                          "it will be SKIPPED at compose (lands with E9)"})
            elif e.get("mode") and e["mode"] not in modes:
                issues.append({"id": e["id"], "level": "error",
                               "message": f"kind '{e['kind']}' does not support mode "
                                          f"'{e['mode']}' (supported: {modes})"})
            if not e["acl"]:
                issues.append({"id": e["id"], "level": "error",
                               "message": f"'{e['id']}' has no ACL — default-deny: say "
                                          f"'visible to <group>' (LAW 2)"})
            if not e.get("description"):
                issues.append({"id": e["id"], "level": "warn",
                               "message": f"'{e['id']}' has no routing description — the "
                                          "router may never pick it (say: containing <topics>)"})
        return issues

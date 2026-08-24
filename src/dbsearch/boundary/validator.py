"""Boundary-contract validator — makes SKILL.md LAW 1 mechanical, not aspirational.

Every payload bound for the control-plane uplink MUST pass `BoundaryValidator.validate`
before it is allowed to cross. A payload carrying ANY document content (or anything not
explicitly in the contract) raises `BoundaryViolation` and is dropped + audited.

Dependency-free on purpose (no jsonschema/network) so it runs anywhere, including an
air-gapped data plane. Two layers of defense:
  1. allow-list: only fields declared in boundary.schema.json may appear (root is strict).
  2. deny-list: known content-bearing key names are rejected at ANY nesting depth.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

_SCHEMA_PATH = Path(__file__).with_name("boundary.schema.json")


class BoundaryViolation(Exception):
    """Raised when a payload would leak content or violate the uplink contract (LAW 1)."""


class BoundaryValidator:
    def __init__(self, schema: dict | None = None) -> None:
        self.schema = schema or json.loads(_SCHEMA_PATH.read_text())
        self.forbidden = {k.lower() for k in self.schema.get("x-forbidden-keys", [])}

    def validate(self, payload: dict) -> bool:
        """Return True if the payload may cross the uplink; else raise BoundaryViolation."""
        self._reject_forbidden_keys(payload)
        self._enforce_schema(payload, self.schema, path="")
        return True

    # Layer 2: no content-bearing key anywhere, at any depth.
    def _reject_forbidden_keys(self, obj: object, path: str = "") -> None:
        if isinstance(obj, dict):
            for key, value in obj.items():
                if key.lower() in self.forbidden:
                    raise BoundaryViolation(
                        f"content-bearing key '{path}{key}' may not cross the uplink (LAW 1)"
                    )
                self._reject_forbidden_keys(value, f"{path}{key}.")
        elif isinstance(obj, list):
            for i, value in enumerate(obj):
                self._reject_forbidden_keys(value, f"{path}{i}.")

    # Layer 1: the schema IS the contract — enforce shape AND value rules (LAW 1).
    # Beyond the root allow-list we honor `type`, `enum`, and `pattern` so the wire
    # rejects a value that has drifted from boundary.schema.json (e.g. a made-up event,
    # a customer name where an opaque id is required, a string where a counter belongs).
    def _enforce_schema(self, obj: object, schema: dict, path: str) -> None:
        where = path.rstrip(".") or "<root>"

        declared = schema.get("type")
        if declared is not None and not self._type_ok(obj, declared):
            raise BoundaryViolation(
                f"field '{where}' has the wrong type (contract requires {declared}) (LAW 1)"
            )

        if "enum" in schema and obj not in schema["enum"]:
            raise BoundaryViolation(
                f"field '{where}' value {obj!r} is not in the contract enum (LAW 1)"
            )

        pattern = schema.get("pattern")
        if pattern is not None and isinstance(obj, str) and re.search(pattern, obj) is None:
            raise BoundaryViolation(
                f"field '{where}' value {obj!r} violates the contract pattern {pattern!r} (LAW 1)"
            )

        if isinstance(obj, dict):
            props = schema.get("properties", {})
            additional = schema.get("additionalProperties", True)
            for key, value in obj.items():
                if key in props:
                    self._enforce_schema(value, props[key], f"{path}{key}.")
                elif additional is False:
                    raise BoundaryViolation(
                        f"field '{path}{key}' is not in the boundary contract (LAW 1)"
                    )
                elif isinstance(additional, dict):
                    self._enforce_schema(value, additional, f"{path}{key}.")
            for required in schema.get("required", []):
                if required not in obj:
                    raise BoundaryViolation(f"missing required field '{path}{required}'")

    # JSON-Schema type check (a `type` may be a single name or a list of allowed names).
    # bool is excluded from number/integer on purpose: a counter must be a real number.
    @staticmethod
    def _type_ok(obj: object, declared: object) -> bool:
        if isinstance(declared, list):
            return any(BoundaryValidator._type_ok(obj, t) for t in declared)
        if declared == "object":
            return isinstance(obj, dict)
        if declared == "array":
            return isinstance(obj, list)
        if declared == "string":
            return isinstance(obj, str)
        if declared == "boolean":
            return isinstance(obj, bool)
        if declared == "integer":
            return isinstance(obj, int) and not isinstance(obj, bool)
        if declared == "number":
            return isinstance(obj, (int, float)) and not isinstance(obj, bool)
        return True  # unknown/unsupported type keyword — don't block on it

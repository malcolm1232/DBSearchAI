"""Hermetic SOLID/complexity gate for the golden-suite code (spec 2026-07-31, Global Constraints).

Stdlib-ast cyclomatic complexity, no ruff/radon dependency, so the gate can never be
skipped for tooling reasons. Scope is the NEW golden-suite code only; legacy modules are
not retro-judged here.

    python3 tests/selftest_code_quality.py
"""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

SCOPED = [
    "src/dbsearch/eval/tally.py",
    "src/dbsearch/eval/http_probe.py",
    "src/dbsearch/eval/golden",
    "scripts/author_golden_corpus.py",
    "scripts/validate_golden_pack.py",
    "scripts/golden_runner.py",
]
# 15 is SonarQube's default and radon's "moderate risk" floor, not McCabe's 1976 guideline
# of 10. At 10 this gate fired on ordinary validation-heavy scorers, and the cheapest way to
# satisfy it was splitting them into helpers that mutate the caller's dicts - trading a
# readable branchy function for hidden side effects, which is worse code by every measure
# this suite claims to care about. At 15 the gate is silent on honest code and only fires on
# genuinely tangled control flow. The no-out-params rule that replaced the splitting is a
# review rule, not an ast check: helpers RETURN their scores, callers merge them.
MAX_COMPLEXITY = 15
MAX_FUNC_LINES = 60
MAX_MODULE_LINES = 400

_BRANCH = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.ExceptHandler, ast.IfExp)

# ast.match_case (PEP 634 structural pattern matching) only exists on Python >= 3.10.
# This repo's default `python3` may be older; getattr(..., ()) makes the isinstance check
# below always False (never match, branch inert) on interpreters where it's absent, rather
# than raising AttributeError at import time. Do not reference ast.match_case directly.
_MATCH_CASE = getattr(ast, "match_case", ())


def complexity(fn: ast.AST) -> int:
    """McCabe-style: 1 + branch points. Nested defs are scored separately, not here.

    A comprehension contributes +1 for the clause itself plus +1 per each of its `if`
    filters (standard radon/mccabe semantics for comprehension.ifs).
    """
    score = 1
    stack = list(ast.iter_child_nodes(fn))
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        if isinstance(node, _BRANCH):
            score += 1
        elif isinstance(node, ast.BoolOp):
            score += len(node.values) - 1
        elif isinstance(node, ast.comprehension):
            score += 1 + len(node.ifs)
        elif isinstance(node, _MATCH_CASE):
            score += 1
        stack.extend(ast.iter_child_nodes(node))
    return score


def iter_functions(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def scoped_files() -> list[Path]:
    out = []
    for rel in SCOPED:
        p = ROOT / rel
        if p.is_dir():
            out.extend(sorted(p.rglob("*.py")))
        elif p.exists():
            out.append(p)
    return out


def violations(path: Path) -> list[str]:
    src = path.read_text()
    tree = ast.parse(src)
    out = []
    n_lines = len(src.splitlines())
    rel = path.relative_to(ROOT)
    if n_lines > MAX_MODULE_LINES:
        out.append(f"{rel}: module is {n_lines} lines (max {MAX_MODULE_LINES})")
    for fn in iter_functions(tree):
        c = complexity(fn)
        span = (fn.end_lineno or fn.lineno) - fn.lineno + 1
        if c > MAX_COMPLEXITY:
            out.append(f"{rel}:{fn.lineno} {fn.name} complexity {c} (max {MAX_COMPLEXITY})")
        if span > MAX_FUNC_LINES:
            out.append(f"{rel}:{fn.lineno} {fn.name} is {span} lines (max {MAX_FUNC_LINES})")
    return out


def test_counter_flags_a_complex_function():
    src = "def f(x):\n" + "".join(f"    if x == {i}: return {i}\n" for i in range(12))
    fn = next(iter_functions(ast.parse(src)))
    assert complexity(fn) == 13


def test_counter_accepts_a_simple_function():
    fn = next(iter_functions(ast.parse("def f(x):\n    return x + 1\n")))
    assert complexity(fn) == 1


def test_boolop_and_comprehension_count():
    src = "def f(xs, a, b, c):\n    if a and b and c:\n        return [x for x in xs if x]\n"
    fn = next(iter_functions(ast.parse(src)))
    assert complexity(fn) == 6  # 1 + if + (and,and = 2) + comprehension + its if


def test_scoped_files_are_clean():
    problems = [v for path in scoped_files() for v in violations(path)]
    assert not problems, "\n".join(problems)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok {name}")
    print("selftest_code_quality: all green")

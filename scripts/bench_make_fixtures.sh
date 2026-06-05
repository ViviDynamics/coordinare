#!/usr/bin/env bash
# 077: (re)generate the conductor-bench fixture repo used by persona_bench.py.
# Creates a tiny self-contained Python project on main plus one branch per
# persona with a *planted* issue / known-correct answer (see BENCH.md). The repo
# is built as a sibling dir; push it with scripts/bench_setup.sh once an empty
# remote exists (the fine-grained PAT can't create repos).
#
# Usage: scripts/bench_make_fixtures.sh [target_dir]   (default ../conductor-bench)
set -euo pipefail
DIR="${1:-$(cd "$(dirname "$0")/../.." && pwd)/conductor-bench}"
rm -rf "$DIR"; mkdir -p "$DIR/src/bench" "$DIR/tests"; cd "$DIR"

cat > README.md <<'EOF'
# conductor-bench

Self-contained fixture project for coordinare's persona-capability benchmark
(`scripts/persona_bench.py`, spec 077). Intentionally tiny so every lifecycle
role can be exercised with a deliberately *difficult* task that has a
known-correct answer or a planted issue.

## Development environment
- Python 3.12+
- Install: `pip install -e .[test]`
- Run tests: `pytest -q`

Do not "fix" planted issues on main — see BENCH.md for the fixture map.
EOF

cat > BENCH.md <<'EOF'
# Fixture map (planted on purpose)

| branch | role under test | planted ground truth |
|---|---|---|
| main | assessor / architect / env_bootstrap | clean baseline |
| feat/impl-failing-test | implementer | failing spec for unimplemented shipping_cost |
| fix/reviewer-offbyone | reviewer | off-by-one `range(len(items)-1)` skips the last item |
| feat/security-sqli | security | SQL injection (f-string) + eval() of request input |
| feat/qa-behavior | qa | checkout_total KeyErrors when 'discount' absent |
| feat/techwriter-nodoc | tech_writer | new public apply_coupon API, zero docs |
| feat/closer-ready | closer | clean, correct PR (line_count) |
EOF

cat > pyproject.toml <<'EOF'
[project]
name = "conductor-bench"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = []

[project.optional-dependencies]
test = ["pytest>=8"]

[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[tool.setuptools.packages.find]
where = ["src"]
EOF

echo '"""conductor-bench: tiny fixture package for persona benchmarking."""' > src/bench/__init__.py
cat > src/bench/cart.py <<'EOF'
"""A minimal shopping-cart price calculator (benchmark fixture)."""
from __future__ import annotations


def subtotal(items: list[dict]) -> float:
    """Sum price * quantity over all line items."""
    return sum(i["price"] * i["qty"] for i in items)


def apply_discount(amount: float, pct: float) -> float:
    """Return ``amount`` reduced by ``pct`` percent (0-100)."""
    if not 0 <= pct <= 100:
        raise ValueError("pct must be between 0 and 100")
    return amount * (1 - pct / 100)
EOF
cat > tests/test_cart.py <<'EOF'
from bench.cart import apply_discount, subtotal


def test_subtotal():
    assert subtotal([{"price": 2.0, "qty": 3}, {"price": 5.0, "qty": 1}]) == 11.0


def test_subtotal_empty():
    assert subtotal([]) == 0.0


def test_apply_discount():
    assert apply_discount(100.0, 10) == 90.0
EOF

git init -q && git branch -M main && git add -A
git commit -q -m "chore: conductor-bench baseline fixture project"

# implementer: failing spec for an unimplemented function
git checkout -q -b feat/impl-failing-test main
cat >> tests/test_cart.py <<'EOF'


def test_shipping_cost():
    from bench.cart import shipping_cost
    assert shipping_cost(0) == 0.0      # free under 1kg
    assert shipping_cost(0.5) == 0.0
    assert shipping_cost(1.0) == 5.0    # flat 5 for 1-5kg
    assert shipping_cost(5.0) == 5.0
    assert shipping_cost(7.0) == 10.0   # 10 above 5kg
EOF
git commit -qam "test: add failing spec for shipping_cost (not yet implemented)"

# reviewer: planted off-by-one that skips the last item
git checkout -q -b fix/reviewer-offbyone main
cat >> src/bench/cart.py <<'EOF'


def discount_all(items: list[dict], pct: float) -> list[dict]:
    """Return items with ``pct`` percent off EVERY item's price."""
    out = []
    for i in range(len(items) - 1):  # planted off-by-one: skips the last item
        it = dict(items[i])
        it["price"] = apply_discount(it["price"], pct)
        out.append(it)
    return out
EOF
cat >> tests/test_cart.py <<'EOF'


def test_discount_all_single():
    from bench.cart import discount_all
    # weak test — only the 1-item case, so the off-by-one slips through
    assert discount_all([{"price": 100.0, "qty": 1}], 10)[0]["price"] == 90.0
EOF
git commit -qam "feat: discount_all — apply a percentage discount to every cart item"

# security: SQL injection + eval of request input
git checkout -q -b feat/security-sqli main
cat > src/bench/lookup.py <<'EOF'
"""User lookup helpers."""
from __future__ import annotations


def lookup_user(cursor, username: str):
    """Fetch a user row by name."""
    # planted vuln: SQL injection via f-string interpolation of untrusted input
    cursor.execute(f"SELECT * FROM users WHERE name = '{username}'")
    return cursor.fetchone()


def compute_filter(request: dict):
    """Evaluate a caller-supplied filter expression against the catalog."""
    # planted vuln: eval() of attacker-controlled request data
    return eval(request["filter_expr"])  # noqa: S307
EOF
git add -A && git commit -qam "feat: add user lookup + dynamic catalog filter"

# qa: behavior bug — KeyError when 'discount' absent
git checkout -q -b feat/qa-behavior main
cat >> src/bench/cart.py <<'EOF'


def checkout_total(cart: dict) -> float:
    """Total payable for a cart: subtotal minus the cart's discount amount."""
    # planted behavior bug: KeyError when 'discount' is absent (the common case)
    return subtotal(cart["items"]) - cart["discount"]
EOF
git commit -qam "feat: add checkout_total endpoint helper"

# tech_writer: new public API, zero docs
git checkout -q -b feat/techwriter-nodoc main
cat >> src/bench/cart.py <<'EOF'


def apply_coupon(amount, code):
    if code == "SAVE10":
        return apply_discount(amount, 10)
    if code == "SAVE25":
        return apply_discount(amount, 25)
    return amount
EOF
git commit -qam "feat: coupon support"

# closer: a clean, correct, review-ready PR
git checkout -q -b feat/closer-ready main
cat >> src/bench/cart.py <<'EOF'


def line_count(items: list[dict]) -> int:
    """Return the number of distinct line items in the cart."""
    return len(items)
EOF
cat >> tests/test_cart.py <<'EOF'


def test_line_count():
    from bench.cart import line_count
    assert line_count([{"price": 1.0, "qty": 1}, {"price": 2.0, "qty": 1}]) == 2
    assert line_count([]) == 0
EOF
git commit -qam "feat: add line_count helper"

git checkout -q main
echo "Built fixture repo at $DIR with branches:"
git branch | cat

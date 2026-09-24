"""417: the metacharacter rule applies to UNQUOTED text only, and the
read-only set grows by the programs the audit asked for.

Before: ``_SHELL_META = re.compile(r"[><`$]")`` scanned the whole line, so
``grep "^func.*{$" x.go`` and ``rg "Vec<" f`` were refused and the survey
burned its command budget on regexes.
"""
from __future__ import annotations

import pytest
from performer.workflows.architect.allowlist import is_allowed


@pytest.mark.parametrize("cmd", [
    'grep "^func.*{$" x.go',
    'rg "Vec<" f',
    'grep "List<T>" f',
    "grep '<div' f",
    "rg 'a$b' f",
    r"rg 'a\|b\$' f",
])
def test_quoted_regex_anchors_are_admitted(cmd):
    ok, reason = is_allowed(cmd)
    assert ok, reason


@pytest.mark.parametrize("cmd", [
    "cat x > y",
    "$(rm -rf)",
    "ls `rm x`",
    'echo "$(rm -rf)"',  # double quotes do NOT neutralise substitution
    'grep "$HOME" f',  # double quotes do NOT neutralise expansion
    "grep `cmd` f",
    "cat f | tee out.txt",
])
def test_unquoted_meta_and_double_quoted_substitution_is_refused(cmd):
    ok, reason = is_allowed(cmd)
    assert not ok, f"{cmd!r} should be refused"
    assert reason


@pytest.mark.parametrize("cmd", [
    "git grep pattern",
    "git grep -n pattern app/",
    "git grep -i todo -- src",
    "awk -F, '{print $1}' data.csv",
    "awk 'NR==2{print $3}' f",
    "jq -r .name f.json",
    "jq . f.json | head",
    "tree -L 2",
    "stat app/models/user.rb",
    "diff a b",
    "diff -u old new | head",
    "comm -12 a b",
    "basename a/b/c.py",
    "dirname a/b/c.py",
    "file -b app/models/user.rb",
    "rg -rl foo . | xargs cat",
    "grep -rl foo . | xargs wc -l",
    "xargs grep foo",
])
def test_audit_requested_programs_are_admitted(cmd):
    ok, reason = is_allowed(cmd)
    assert ok, reason


@pytest.mark.parametrize("cmd", [
    "awk 'BEGIN{system(\"rm -rf /\")}' f",
    "awk '{print > \"out\"}' f",
    "awk '{print | \"mail\"}' f",
    "awk 'BEGIN{while ((getline line) > 0) print line}' f",
    "awk -f prog.awk f",
    "awk -W exec=evil f",
    "xargs sh -c",
    "xargs rm",
    "cat f | xargs rm",
    "diff -o /tmp/x a b",
    "diff --output /tmp/x a b",
    "file -C",
    "git grep -O pattern",
    "git grep --open-files-in-pager pattern",
])
def test_new_programs_keep_their_escapes_refused(cmd):
    ok, _ = is_allowed(cmd)
    assert not ok, f"{cmd!r} should be refused"


def test_xargs_refusal_reason_names_xargs_and_the_child():
    ok, reason = is_allowed("cat f | xargs rm")
    assert not ok
    assert "xargs" in reason

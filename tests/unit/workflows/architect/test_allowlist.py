"""165 FR-004: the survey runs only read-only commands. The model proposes;
this policy decides. One test per rule instance, because a persona already
said all of this and a live architect ran `bundle install` anyway."""
from __future__ import annotations

import pytest
from performer.workflows.architect.allowlist import is_allowed


@pytest.mark.parametrize("cmd", [
    "ls docs/wiki",
    "ls -la app/models",
    "cat README.md",
    "head -n 40 Gemfile",
    "tail -n 20 db/schema.rb",
    "sed -n '1,120p' spec/rails_helper.rb",
    "rg -n 'create_table' db/migrate",
    "grep -rn TODO app/",
    "find . -name '*.rb' -path '*models*'",
    "wc -l app/models/user.rb",
    "git log --oneline -20",
    "git show HEAD --stat",
    "git diff origin/main...HEAD -- app/",
    "git ls-files app/models",
    "git status --short",
    "git blame -L 1,20 app/models/user.rb",
    "rg --files db/migrate | sort",
    "cat Gemfile | head -50",
    "git log --oneline | wc -l",
])
def test_read_only_commands_are_allowed(cmd):
    ok, reason = is_allowed(cmd)
    assert ok, reason


@pytest.mark.parametrize("cmd,why", [
    ("bundle install", "package install"),
    ("npm install", "package install"),
    ("rails db:migrate", "runs the app"),
    ("bin/rails db:seed", "runs the app"),
    ("bundle exec rspec", "runs tests"),
    ("rm -rf tmp/cache", "deletes"),
    ("curl https://example.com", "network"),
    ("wget http://x", "network"),
    ("sleep 30", "waits"),
    ("find . -name '*.log' -delete", "find -delete"),
    ("find . -name '*.rb' -exec cat {} +", "find -exec"),
    ("sed -i 's/a/b/' app/models/user.rb", "sed -i writes"),
    ("sed 's/a/b/' f", "sed without -n range"),
    ("git push origin HEAD", "git write"),
    ("git commit -m x", "git write"),
    ("git checkout -b feature", "git write"),
    ("git reset --hard", "git write"),
    ("git rebase main", "git write"),
    ("git stash", "git write"),
    ("cat f > out.txt", "redirection"),
    ("cat f >> out.txt", "redirection"),
    ("ls; rm -rf .", "chained refused command"),
    ("ls && bundle install", "chained refused command"),
    ("cat f | tee out.txt", "pipe into a writer"),
    ("cat f | xargs rm", "pipe into a writer"),
    ("python3 -c 'print(1)'", "arbitrary interpreter"),
    ("ruby -e 'puts 1'", "arbitrary interpreter"),
    ("bash -c 'ls'", "shell escape"),
    ("echo hi", "not a read of the repo"),
    ("$(rm -rf .)", "substitution"),
    ("ls `rm x`", "substitution"),
    ("", "empty"),
])
def test_everything_else_is_refused(cmd, why):
    ok, reason = is_allowed(cmd)
    assert not ok, f"{cmd!r} should be refused ({why})"
    assert reason, "a refusal names its reason"


def test_chained_allowed_commands_are_allowed():
    ok, _ = is_allowed("git log --oneline -5; git status --short")
    assert ok
    ok, _ = is_allowed("ls app && ls spec")
    assert ok


def test_refusal_reason_names_the_offending_token():
    ok, reason = is_allowed("cat f | xargs rm")
    assert not ok and "xargs" in reason


# --- review round (PR #266): bypasses found by execution, each now refused -----

@pytest.mark.parametrize(
    "command",
    [
        "git log --output=/tmp/x",
        "git show --output=/tmp/x HEAD",
        "git diff --output /tmp/x",
        "git diff --external-diff=cat",
        "git diff --ext-diff",
        "git -c pager.log=cat log",
        "git -C /etc log",
        "git --git-dir=/tmp/x log",
        "git branch new-branch",
        "git branch -D main",
        "git branch -m a b",
        "git branch --set-upstream-to=origin/main",
        "sed -n -e '1p' -e 'w /tmp/x' f",
        "sed -n '/a/w /tmp/p' f",
        "sed -n -f script.sed f",
        "sed -n '1p' f -w /tmp/out",
        "rg --pre 'rm -rf /' pattern .",
        "rg --pre=cat pattern .",
        "rg --pre-glob '*.md' --pre cat pattern .",
        "rg --hostname-bin=/tmp/evil pattern .",
        "grep --pre cat pattern f",
        "find . -fprintf /tmp/x '%p'",
        "find . -fprint0 /tmp/x",
        "sort -o /tmp/x f",
        "sort --output=/tmp/x f",
        "uniq f /tmp/out",
    ],
)
def test_write_and_exec_options_are_refused(command):
    allowed, reason = is_allowed(command)
    assert allowed is False, (command, reason)


@pytest.mark.parametrize(
    "command",
    [
        "git log --oneline -8",
        "git log -p -3",
        "git log -c HEAD",
        "git diff HEAD~1 -- app/",
        "git branch -a",
        "git branch --show-current",
        "sed -n '1,40p' app/models/user.rb",
        "sed -n '/Contact/p' app/views/home.html.erb",
        "sed -n -E '10,20p' f",
        "rg -n 'def parse' --type ruby .",
        "grep -rn Contct --include='*.erb' .",
        "sort -u f",
        "uniq -c f",
        "find . -name '*.rb' -not -path './vendor/*'",
    ],
)
def test_ordinary_reads_stay_allowed(command):
    allowed, reason = is_allowed(command)
    assert allowed is True, (command, reason)


# live round 2026-09-06: six of ten proposed reads carried `2>/dev/null`
@pytest.mark.parametrize(
    "command",
    [
        "ls db/migrate 2>/dev/null | tail -15",
        "sed -n 1,80p config/routes.rb 2>/dev/null",
        "grep -n timesheet db/schema.rb 2>&1 | head",
    ],
)
def test_stderr_to_devnull_is_a_harmless_redirect(command):
    assert is_allowed(command) == (True, "read-only")


@pytest.mark.parametrize(
    "command",
    ["ls 2>/tmp/x", "ls 2>>/dev/null", "ls 1>/dev/null", "ls 2>/dev/nullx", "cat f >/dev/null"],
)
def test_every_other_redirect_is_still_refused(command):
    assert is_allowed(command)[0] is False


# live round 2026-09-06: alternation inside a quoted grep pattern is not a pipe
@pytest.mark.parametrize(
    "command",
    [
        "grep -n 'week\\|period\\|submission' db/schema.rb | head -30",
        "grep -rn 'belongs_to\\|has_many' app/models | head -30",
        'grep -n "a|b" f',
        "rg 'a|b' . | head",
    ],
)
def test_a_pipe_inside_quotes_is_a_pattern_not_a_chain(command):
    assert is_allowed(command) == (True, "read-only")


@pytest.mark.parametrize("command", ["ls & rm -rf x", "(ls)", "ls; rm x", "ls || curl x", "ls | tee f"])
def test_chains_are_still_judged_per_segment_and_control_tokens_refused(command):
    assert is_allowed(command)[0] is False


# live round 2026-09-06 (trivial): an escaped group in a find expression
@pytest.mark.parametrize(
    "command",
    [
        "find . -path ./.git -prune -o -type f \\( -name '*.html' -o -name '*.erb' \\) -print",
        "grep -rn 'Contact' --include='*.erb' . | head -5",
        "ls app/models 2>/dev/null || find . -maxdepth 3 -name 'models.py' -not -path './.git/*'",
    ],
)
def test_escaped_parens_and_or_chains_of_reads_are_allowed(command):
    assert is_allowed(command) == (True, "read-only")


def test_a_pipe_inside_double_quotes_with_an_escaped_quote_is_still_one_pattern():
    assert is_allowed('grep "a\\"b|c" f')[0] is True
    assert is_allowed("grep 'unterminated f")[0] is False

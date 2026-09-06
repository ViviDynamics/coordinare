"""Three fixture cards for the architect workflow eval (spec 165 SC-004).

Each fixture carries the card as the architect would receive it, the files a
tiny repository holds so the survey has something to read, the canned model
answers the stub returns, and the expectations scoring checks. Deterministic
by construction; the live mode swaps the stub for the gateway and keeps the
expectations.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Expectation:
    size: str
    min_milestones: int
    max_milestones: int
    min_data_model_changes: int
    docs: str  # "none", "one", "some"
    min_criteria: int


@dataclass(frozen=True)
class Fixture:
    name: str
    title: str
    description: str
    criteria: list[str]
    repo_files: dict[str, str]
    survey_commands: list[str]  # what the stub model proposes (includes one refusal)
    blueprint: dict  # what the stub model answers
    expect: Expectation
    extra: dict = field(default_factory=dict)


_RAILS_FILES = {
    "README.md": "# Website\nRails app.\n",
    "Gemfile": "source 'https://rubygems.org'\ngem 'rails'\n",
    "app/models/time_entry.rb": "class TimeEntry < ApplicationRecord\n  belongs_to :project\nend\n",
    "app/views/contact/index.html.erb": "<h1>Contct us</h1>\n",
    "db/schema.rb": "ActiveRecord::Schema.define do\n  create_table :time_entries do |t|\n    t.integer :minutes\n  end\nend\n",
    "spec/models/time_entry_spec.rb": "RSpec.describe TimeEntry do\nend\n",
}

TRIVIAL = Fixture(
    name="trivial",
    title="Fix the typo in the contact page heading",
    description="The contact page heading reads 'Contct us'. It should read 'Contact us'.",
    criteria=["The contact page heading reads 'Contact us'"],
    repo_files=_RAILS_FILES,
    survey_commands=["rg -n 'Contct' app/views", "cat app/views/contact/index.html.erb", "bundle install"],
    blueprint={
        "summary": "Correct the heading copy on the contact page.",
        "milestones": [{"goal": "fix the heading text", "scope": ["app/views/contact/index.html.erb"], "done_when": "the heading reads Contact us"}],
        "modules": [{"path": "app/views/contact/", "note": "copy only"}],
        "data_model": {"changes": []},
        "interfaces": [],
        "risks": [],
        "criteria": [{"surface": "/contact", "action": "open the page", "expected": "heading reads Contact us", "kind": "visual"}],
        "docs": [],
    },
    expect=Expectation(size="small", min_milestones=1, max_milestones=1, min_data_model_changes=0, docs="none", min_criteria=1),
)

FEATURE = Fixture(
    name="feature",
    title="Accept HH:mm and decimal input for time entry durations",
    description="Employees type durations as 1:30 or 1.5. Both must parse to 90 minutes, invalid input shows an error.",
    criteria=["1:30 parses to 90 minutes", "1.5 parses to 90 minutes", "Invalid input shows a validation error"],
    repo_files=_RAILS_FILES,
    survey_commands=["cat app/models/time_entry.rb", "rg -n minutes app db", "git log --oneline -5", "rails db:migrate"],
    blueprint={
        "summary": "Parse HH:mm and decimal duration input into canonical minutes on TimeEntry.",
        "milestones": [
            {"goal": "duration parser", "scope": ["app/lib/duration_parser.rb", "spec/lib/duration_parser_spec.rb"], "done_when": "parser specs pass for both formats and rejections"},
            {"goal": "wire the parser into TimeEntry", "scope": ["app/models/time_entry.rb"], "done_when": "model accepts a duration string and stores minutes"},
            {"goal": "form validation message", "scope": ["app/views/time_entries", "config/locales"], "done_when": "invalid input shows the error"},
        ],
        "modules": [{"path": "app/models/", "note": "TimeEntry gains a virtual duration attribute"}, {"path": "app/lib/", "note": "new parser"}],
        "data_model": {"changes": []},
        "interfaces": [],
        "risks": ["locale decimal separators"],
        "criteria": [
            {"surface": "/time_entries/new", "action": "enter 1:30 and save", "expected": "entry stored with 90 minutes", "kind": "functional"},
            {"surface": "/time_entries/new", "action": "enter 1.5 and save", "expected": "entry stored with 90 minutes", "kind": "functional"},
            {"surface": "/time_entries/new", "action": "enter 'abc' and save", "expected": "a validation error is shown", "kind": "functional"},
        ],
        "docs": [{"topic": "Entering durations", "location": "docs/wiki/time-tracking.md", "say": "both accepted formats and what invalid input does"}],
    },
    expect=Expectation(size="large", min_milestones=2, max_milestones=4, min_data_model_changes=0, docs="one", min_criteria=3),
)

SCHEMA = Fixture(
    name="schema",
    title="Schema S4: timesheet_submissions lifecycle scaffold",
    description="Add a timesheet_submissions table and model with a submit/approve lifecycle and an admin approval endpoint.",
    criteria=["Submitting a week creates a submission", "Approving locks the week's entries"],
    repo_files=_RAILS_FILES,
    survey_commands=["cat db/schema.rb", "cat app/models/time_entry.rb", "rg -n 'class .* < ApplicationRecord' app/models", "git log --oneline -10"],
    blueprint={
        "summary": "Introduce timesheet submissions with a state machine and an approval endpoint.",
        "milestones": [
            {"goal": "migration and model", "scope": ["db/migrate", "app/models/timesheet_submission.rb"], "done_when": "model specs pass"},
            {"goal": "lifecycle transitions", "scope": ["app/models/timesheet_submission.rb"], "done_when": "submit and approve transitions covered by specs"},
            {"goal": "approval endpoint", "scope": ["app/controllers/admin/timesheets_controller.rb", "config/routes.rb"], "done_when": "request specs pass"},
            {"goal": "lock entries on approval", "scope": ["app/models/time_entry.rb"], "done_when": "approved weeks reject edits"},
        ],
        "modules": [{"path": "app/models/", "note": "new model, TimeEntry lock"}, {"path": "app/controllers/admin/", "note": "approval"}, {"path": "db/migrate/", "note": "new table"}],
        "data_model": {"changes": [
            {"kind": "table", "name": "timesheet_submissions", "note": "employee, period_start, status, submitted_at, approved_at"},
            {"kind": "index", "name": "timesheet_submissions_employee_period", "note": "unique per employee and period"},
        ]},
        "interfaces": [{"name": "POST /admin/timesheets/:id/approve", "kind": "endpoint", "contract": "admin only; transitions submitted -> approved; 200 with the submission"}],
        "risks": ["locking interacts with in-flight edits", "period boundaries across timezones"],
        "criteria": [
            {"surface": "/timesheets", "action": "submit the current week", "expected": "a submission in state submitted exists", "kind": "functional"},
            {"surface": "/admin/timesheets", "action": "approve the submission", "expected": "state approved and the week's entries reject edits", "kind": "functional"},
        ],
        "docs": [
            {"topic": "Timesheet lifecycle", "location": "docs/wiki/time-tracking.md", "say": "the states, who moves them, and what locking means"},
            {"topic": "Approving timesheets", "location": "docs/wiki/admin.md", "say": "where admins approve and what changes"},
        ],
    },
    expect=Expectation(size="large", min_milestones=3, max_milestones=7, min_data_model_changes=1, docs="some", min_criteria=2),
)

FIXTURES: tuple[Fixture, ...] = (TRIVIAL, FEATURE, SCHEMA)

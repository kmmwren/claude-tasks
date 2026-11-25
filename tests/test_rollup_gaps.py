"""Each project names what it has not recorded, and `rollup.py gaps` fails while anything is missing."""

import pathlib

import pytest
import rollup
import rollup_gaps as rg
import rollup_model as rm

from tests.rollup_sources import projects, rollup_of, source_of, with_project_toml

COMPLETE = 'initiative = "Platform"\n'
DATED = 'due = "2026-12-01"\n'


def project_toml(*, head: str = COMPLETE, m2: str = DATED) -> str:
    return (
        f'{head}\n[project]\nslug = "demo"\ntitle = "Demo"\noutcome = "Demo works."\n\n'
        f'[[milestones]]\nid = "m1"\ntitle = "One"\n\n'
        f'[[milestones]]\nid = "m2"\ntitle = "Two"\n{m2}'
    )


def demo(tmp_path: pathlib.Path, text: str) -> rm.Project:
    return projects(rollup_of(tmp_path, with_project_toml(text)))[0]


def test_a_project_that_records_everything_has_no_gaps(tmp_path: pathlib.Path) -> None:
    assert rg.gaps(demo(tmp_path, project_toml())) == ()


def test_a_project_with_no_initiative_says_where_to_name_one(tmp_path: pathlib.Path) -> None:
    (gap,) = rg.gaps(demo(tmp_path, project_toml(head="")))
    assert (gap.project, gap.subject, gap.missing) == ("demo", "project", "initiative")
    assert gap.line() == (
        'demo project: no initiative; add initiative = "<name>" above [project] in project.toml'
    )


def test_an_open_milestone_with_no_date_says_how_to_set_one(tmp_path: pathlib.Path) -> None:
    (gap,) = rg.gaps(demo(tmp_path, project_toml(m2="")))
    assert gap.table() == {
        "project": "demo",
        "subject": "m2",
        "missing": "due date",
        "fix": "project_facts.py set-due --milestone m2 --due YYYY-MM-DD --reason ... --set-by ...",
    }


def test_a_done_milestone_needs_no_date(tmp_path: pathlib.Path) -> None:
    project = demo(tmp_path, project_toml())
    assert project.milestones[0].stage is rm.Stage.DONE and not project.milestones[0].due
    assert rg.gaps(project) == ()


def test_a_parked_milestone_needs_no_date(tmp_path: pathlib.Path) -> None:
    project = demo(tmp_path, project_toml(m2="parked = true\n"))
    assert project.milestones[1].stage is rm.Stage.PARKED
    assert rg.gaps(project) == ()


def test_a_new_expectation_is_one_subclass(tmp_path: pathlib.Path) -> None:
    class HasOutcome(rg.Expectation):
        def gaps(self, project: rm.Project):  # type: ignore[no-untyped-def]
            yield rg.Gap(project.slug, "project", "outcome", "say what done looks like")

    assert [g.missing for g in rg.gaps(demo(tmp_path, project_toml()), (HasOutcome(),))] == [
        "outcome"
    ]


@pytest.mark.parametrize(
    ("text", "code", "said"),
    [
        (project_toml(), 0, "every project records what the roll-up needs\n"),
        (project_toml(head="", m2=""), 1, "demo project: no initiative;"),
    ],
    ids=["complete", "missing"],
)
def test_the_gaps_command_fails_while_anything_is_missing(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str], text: str, code: int, said: str
) -> None:
    source = source_of(tmp_path, with_project_toml(text), "Work")
    assert rollup.main(["gaps", "--source", f"Work={source.repo}"], env={}) == code
    out = capsys.readouterr().out
    assert out.startswith(said)
    assert out.count("\n") == (2 if code else 1)


def test_every_level_carries_the_gaps_in_its_json(tmp_path: pathlib.Path) -> None:
    report = rollup_of(tmp_path, with_project_toml(project_toml(m2="")))
    for view in rollup.rv.VIEWS.values():
        (project,) = view.data(report)["projects"]
        assert [g["subject"] for g in project["gaps"]] == ["m2"]


def test_gaps_of_one_kind_read_as_one_clause(tmp_path: pathlib.Path) -> None:
    project = demo(tmp_path, project_toml(head="", m2=""))
    assert rg.missing_note(rg.gaps(project)) == "Missing initiative; due date on m2"
    assert rg.missing_note(()) == ""


def test_a_project_row_carries_what_the_project_lacks(tmp_path: pathlib.Path) -> None:
    report = rollup_of(tmp_path, with_project_toml(project_toml(m2="")))
    (entry,) = report.entries
    assert rollup.rv.project_row(entry, "Next: Two").note == "Next: Two; Missing due date on m2"
    assert rollup.rv.project_row(entry, "").note == "Missing due date on m2"


def test_a_long_run_of_one_gap_names_three_and_counts_the_rest() -> None:
    gaps = [rg.Gap("demo", f"m{n}", "due date", "") for n in range(1, 6)]
    assert rg.missing_note(gaps) == "Missing due date on m1, m2, m3 and 2 more"
    assert rg.missing_note(gaps[:3]) == "Missing due date on m1, m2, m3"

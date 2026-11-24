"""What every project is expected to record for a roll-up to read; each gap names its fix."""

import dataclasses
from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator

import rollup_model as rm


@dataclasses.dataclass(frozen=True)
class Gap:
    project: str  # the project's slug
    subject: str  # "project", or a milestone id
    missing: str
    fix: str

    def line(self) -> str:
        return f"{self.project} {self.subject}: no {self.missing}; {self.fix}"

    def table(self) -> dict[str, str]:
        return dataclasses.asdict(self)


class Expectation(ABC):
    """One fact a project must record; a new rule is one more subclass in EXPECTATIONS."""

    @abstractmethod
    def gaps(self, project: rm.Project) -> Iterator[Gap]: ...


class HasInitiative(Expectation):
    def gaps(self, project: rm.Project) -> Iterator[Gap]:
        if not project.initiative:
            fix = 'add initiative = "<name>" above [project] in project.toml'
            yield Gap(project.slug, "project", "initiative", fix)


class OpenMilestonesHaveDue(Expectation):
    def gaps(self, project: rm.Project) -> Iterator[Gap]:
        for m in project.milestones:
            if m.stage is not rm.Stage.DONE and not m.due:
                yield Gap(project.slug, m.id, "due date", _set_due(m.id))


def _set_due(milestone: str) -> str:
    return f"project_facts.py set-due --milestone {milestone} --due YYYY-MM-DD --reason ... --set-by ..."


EXPECTATIONS: tuple[Expectation, ...] = (HasInitiative(), OpenMilestonesHaveDue())


def gaps(project: rm.Project, expected: Iterable[Expectation] = EXPECTATIONS) -> tuple[Gap, ...]:
    return tuple(g for e in expected for g in e.gaps(project))

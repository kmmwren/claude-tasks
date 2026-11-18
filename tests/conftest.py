import pathlib
import sys

import pytest

# Make scripts/ importable without installing
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "scripts"))


@pytest.fixture(autouse=True)
def _unlatched_capacity():
    """The no-capacity latch is process-wide; no test may inherit another's."""
    import headless  # imported here: scripts/ is only on the path once this module has run

    headless.reset_capacity()
    yield
    headless.reset_capacity()


@pytest.fixture(autouse=True)
def _isolated_pick_state(tmp_path_factory, monkeypatch):
    """Transition and bump log picks; keep every test's log out of the real ~/.claude-tasks."""
    monkeypatch.setenv("CLAUDE_TASKS_STATE", str(tmp_path_factory.mktemp("pick-state")))


@pytest.fixture(autouse=True)
def _no_ambient_launcher(monkeypatch):
    """A launcher in the developer's environment must not change what a test calls; each test sees its own notices."""
    from headless import LAUNCHER_ENV, LEGACY_LAUNCHER_ENV, reset_notices

    for name in (LAUNCHER_ENV, LEGACY_LAUNCHER_ENV):
        monkeypatch.delenv(name, raising=False)
    reset_notices()

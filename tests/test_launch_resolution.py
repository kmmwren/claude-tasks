"""The judge, the triage and the headless command resolve one launcher from the same config and environment."""

import headless
import pytest
import rank
import triage
from headless import DirectLaunch, ScriptLaunch

from tests.queue_helpers import installed_launcher, make_inbox


def _triage_launch(root, monkeypatch, capsys):
    seen = []
    monkeypatch.setattr(triage, "LiveSources", lambda root, prs=None: set().__contains__)
    monkeypatch.setattr(triage, "claude_predict", lambda model, launch: seen.append(launch))
    monkeypatch.setattr(triage, "claude_classify", lambda model, launch: seen.append(launch))
    monkeypatch.setattr(triage, "_progress", lambda predict: predict)
    triage.main(["promote", "--root", str(root), "--llm"])
    capsys.readouterr()
    return seen[0]


@pytest.mark.parametrize("source", ["config", "environment", "neither", "missing"])
def test_rank_judge_and_triage_resolve_the_same_launcher(tmp_path, monkeypatch, capsys, source):
    script = installed_launcher(tmp_path)
    root = make_inbox(tmp_path, {})
    configured = {"config": str(script), "missing": "/nowhere/l.sh"}.get(source)
    if configured:
        (root / "tasks.toml").write_text(f'[rank]\nlauncher = "{configured}"\n')
    if source == "environment":
        monkeypatch.setenv("CLAUDE_TASKS_LAUNCHER", str(script))
    expected = ScriptLaunch(str(script)) if source in ("config", "environment") else DirectLaunch()

    judged = rank.backend(root, rank.settings(root)).launch
    triaged = _triage_launch(root, monkeypatch, capsys)
    assert (judged, triaged, headless.configured_launch(root)) == (expected,) * 3

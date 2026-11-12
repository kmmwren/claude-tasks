"""Tests for class_log.py: verdicts, corrections and the few-shot examples drawn from them."""

import class_log
from class_log import BriefClass, ClassLog, Correction, Example, Verdict


def verdict(cls=BriefClass.NEEDS_INFO, brief_id="a", at="2026-11-11T09:00:00+00:00", reason="no goal"):
    return Verdict(brief_id, cls, reason, f"Title {brief_id}", at)


def correction(v, actual=BriefClass.READY, at="2026-11-11T10:00:00+00:00"):
    return Correction(v.brief_id, v.at, v.cls, actual, "ready", at)


def test_events_round_trip_in_order_and_a_missing_log_is_empty(tmp_path):
    log = ClassLog(tmp_path)
    assert list(log.events()) == []
    v = verdict()
    c = correction(v)
    log.append(v)
    log.append(c)
    with log.path.open("a") as fh:
        fh.write("\n")
    assert (list(log.events()), log.verdicts(), log.corrections()) == ([v, c], [v], [c])


def test_a_verdict_is_logged_only_when_the_class_changes(tmp_path):
    log = ClassLog(tmp_path)
    first = verdict(at="t1")
    assert log.record_verdict(first)
    assert not log.record_verdict(verdict(reason="other words", at="t2"))
    assert log.record_verdict(verdict(BriefClass.READY, at="t3"))
    assert [v.cls for v in log.verdicts()] == [BriefClass.NEEDS_INFO, BriefClass.READY]
    assert log.latest()["a"].cls is BriefClass.READY


def test_a_correction_is_logged_once_per_verdict(tmp_path):
    log = ClassLog(tmp_path)
    v = verdict()
    log.append(v)
    assert log.record_correction(correction(v))
    assert not log.record_correction(correction(v, at="later"))
    assert len(log.corrections()) == 1


def test_uncorrected_lists_latest_verdicts_without_a_correction(tmp_path):
    log = ClassLog(tmp_path)
    a, b = verdict(brief_id="a"), verdict(brief_id="b")
    log.append(a)
    log.append(b)
    log.record_correction(correction(a))
    assert [v.brief_id for v in log.uncorrected()] == ["b"]


def test_examples_join_each_correction_to_its_verdict_newest_last_and_capped(tmp_path):
    log = ClassLog(tmp_path)
    vs = [verdict(brief_id=f"b{i}", at=f"2026-11-11T0{i}:00:00+00:00") for i in range(3)]
    for v in vs:
        log.append(v)
        log.record_correction(correction(v, at=f"2026-11-11T1{v.brief_id[-1]}:00:00+00:00"))
    log.append(Correction("orphan", "never", BriefClass.DROP, BriefClass.READY, "ready", "t"))
    assert log.examples(2) == [
        Example("Title b1", BriefClass.NEEDS_INFO, "no goal", BriefClass.READY),
        Example("Title b2", BriefClass.NEEDS_INFO, "no goal", BriefClass.READY),
    ]


def test_the_log_lives_in_the_queue_state_dir(tmp_path):
    assert ClassLog(tmp_path).path == class_log.state_dir(tmp_path) / class_log.LOG_FILE


def test_sent_back_names_the_briefs_whose_promotion_was_undone(tmp_path):
    log = ClassLog(tmp_path)
    ready, held = verdict(BriefClass.READY, brief_id="x"), verdict(brief_id="y")
    log.append(ready)
    log.append(held)
    log.record_correction(correction(ready, BriefClass.NEEDS_INFO))
    log.record_correction(correction(held))
    assert log.sent_back() == {"x"}

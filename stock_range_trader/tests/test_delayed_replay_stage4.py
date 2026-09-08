"""Actual evaluator/selector/strategy/account/SQLite integration, no market API."""

from delayed_replay_stage4_helpers import WALL, fixture_plan

from delayed_replay.replay_engine import ReplayEngine


def test_real_three_month_replay(tmp_path):
    plan = fixture_plan()
    engine = ReplayEngine.create(tmp_path / "replay.sqlite", plan)
    try:
        final = engine.run(WALL).to_dict()
        assert final["status"] == "completed"
        assert len(final["epochs"]) == 3
        assert len(final["decisions"]) == len(plan.sessions)
        fills = [
            o for o in final["account"]["orders"].values() if o["status"] == "filled"
        ]
        assert {o["request"]["side"] for o in fills} == {"BUY", "SELL"}
    finally:
        engine.store.close()

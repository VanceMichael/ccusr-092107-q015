"""风险规则：超速、闯入限制区、雾航、低富余水深，且结论可溯源。"""

import unittest
from datetime import datetime
from pathlib import Path

from src.evidence import EvidencePool, Observation, RiskEngine
from src.identity import Alias
from src.loader import load_world


def ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


def obs(
    obs_id: str, device_id: str, kind: str, segment_id: str,
    when: str, kp=None, sog=None, values=None, aliases=(), cluster="CL-1",
) -> Observation:
    t = ts(when)
    return Observation(
        obs_id=obs_id, device_id=device_id, obs_ts=t, arrival_ts=t,
        segment_id=segment_id, kind=kind, kp=kp, sog_kn=sog,
        values=values or {}, aliases=tuple(aliases), cluster_id=cluster,
    )


class RiskEngineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.world = load_world(Path("fixtures"))
        self.pool = EvidencePool(self.world)
        self.engine = RiskEngine(self.world, self.pool)

    def _rules(self, observation: Observation) -> set[str]:
        self.pool.add(observation)
        return {f.rule_id for f in self.engine.evaluate(observation)}

    def test_overspeed_in_young_segment(self):
        o = obs("o1", "RADAR-QN-01", "track", "SEG-QN",
                "2026-09-30T08:00:00", kp=46.0, sog=13.5)
        self.assertIn("OVERSPEED", self._rules(o))

    def test_speed_under_limit_no_overspeed(self):
        o = obs("o1", "RADAR-QN-01", "track", "SEG-QN",
                "2026-09-30T08:00:00", kp=46.0, sog=7.0)
        self.assertNotIn("OVERSPEED", self._rules(o))

    def test_restricted_zone_intrusion(self):
        o = obs("o1", "RADAR-MD-01", "track", "SEG-MD",
                "2026-09-30T06:00:00", kp=31.7, sog=6.0)
        self.assertIn("ZONE_INTRUSION", self._rules(o))

    def test_fog_triggers_when_visibility_below_threshold(self):
        env = obs("e1", "WD-MD-01", "env", "SEG-MD",
                  "2026-09-30T06:01:00", values={"visibility_m": 450})
        self.pool.add(env)
        v = obs("v1", "AIS-MD-01", "ais_target", "SEG-MD",
                "2026-09-30T06:01:30", kp=31.6, sog=6.0,
                aliases=[Alias("ais", "413000123")])
        rules = self._rules(v)
        self.assertIn("FOG_NAV", rules)

    def test_low_ukc_uses_vessel_draft_and_minimum_ukc(self):
        env = obs("h1", "HYD-QS-01", "env", "SEG-QS",
                  "2026-09-30T14:01:00",
                  values={"water_depth_m": 3.30, "level_rise_mh": 0.9})
        self.pool.add(env)
        # 郁江集7 吃水 2.9，最小富余 0.5 → 0.4 不足
        v = obs("v1", "AIS-QS-01", "ais_target", "SEG-QS",
                "2026-09-30T14:01:30", kp=57.0, sog=5.0,
                aliases=[Alias("ais", "413000555")])
        findings = self._rules(v)
        self.assertIn("LOW_UKC", findings)

    def test_enough_depth_no_low_ukc(self):
        env = obs("h1", "HYD-QS-01", "env", "SEG-QS",
                  "2026-09-30T14:01:00", values={"water_depth_m": 4.00})
        self.pool.add(env)
        v = obs("v1", "AIS-QS-01", "ais_target", "SEG-QS",
                "2026-09-30T14:01:30", kp=57.0, sog=5.0,
                aliases=[Alias("ais", "413000555")])
        self.assertNotIn("LOW_UKC", self._rules(v))

    def test_env_arrival_retroactively_flags_recent_vessels(self):
        v = obs("v1", "RADAR-QS-02", "track", "SEG-QS",
                "2026-09-30T14:00:00", kp=57.0, sog=5.0,
                aliases=[Alias("ais", "413000555")])
        self.pool.add(v)
        env = obs("h1", "HYD-QS-01", "env", "SEG-QS",
                  "2026-09-30T14:01:00", values={"water_depth_m": 3.30})
        findings = self.engine.evaluate(env)
        self.assertTrue(any(f.rule_id == "LOW_UKC" for f in findings))
        # 水文结论必须回指船舶观测与水文观测两条依据
        ukc = next(f for f in findings if f.rule_id == "LOW_UKC")
        self.assertEqual("v1", ukc.basis_obs[0])
        self.assertEqual("h1", ukc.basis_obs[1])


if __name__ == "__main__":
    unittest.main()

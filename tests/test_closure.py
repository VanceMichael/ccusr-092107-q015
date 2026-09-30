"""闭环核心规则测试：状态机、身份归并、迟到数据、调度冲突。"""

import json
import unittest
from pathlib import Path

from src.engine import ClosureEngine
from src.model import Phase, Resolution, RiskType, DomainError
from src.replay import Replay

CANAL = json.loads(Path("fixtures/canal.json").read_text(encoding="utf-8"))


def detection(token, at, *, source="radar", device="RDR-01", seg="SEG-02",
              km=41.12, etype="anchorage", name=None, mmsi=None,
              eid=None, recv=None, ref=None):
    return {
        "eid": eid, "etype": "detection", "source": source, "device_id": device,
        "at": at, **({"recv": recv} if recv else {}),
        "segment_id": seg, "chainage_km": km, "event_type": etype,
        **({"ref": ref} if ref else {}),
        "data": {"token": token, **({"name_hint": name} if name else {}),
                 **({"mmsi": mmsi} if mmsi else {})},
    }


def sight(token, at, *, source="ais", device="AIS-GW-01", seg="SEG-02",
          km=41.12, name=None, mmsi=None):
    return {
        "etype": "sight", "source": source, "device_id": device,
        "at": at, "segment_id": seg, "chainage_km": km,
        "data": {"token": token, **({"name_hint": name} if name else {}),
                 **({"mmsi": mmsi} if mmsi else {})},
    }


T0 = "2026-09-30T08:00:00+08:00"


def at(hhmm, day="2026-09-30"):
    return f"{day}T{hhmm}:00+08:00"


class ClosureFlowTest(unittest.TestCase):
    def setUp(self):
        self.engine = ClosureEngine(CANAL)

    def _anchor_risk(self):
        self.engine.ingest(sight("MMSI:413005678", T0, km=41.10))
        r = self.engine.ingest(detection("RADAR-X", T0, ref="r")).risk_id
        return r

    def test_full_seven_phase_closure(self):
        rid = self._anchor_risk()
        e = self.engine
        e.remote_verify(rid, "电子-林", at("08:05"))
        e.push_warning(rid, "值班-张", at("08:06"))
        e.call_vessel(rid, "值班-张", at("08:08"), answered=False)
        e.dispatch_boat(rid, "值班长-王", at("08:20"),
                        boat_id="PT-01", person_ids=["P-01", "P-02", "P-03"],
                        anchorage_id="ANC-01")
        e.boarding_result(rid, "水上-梁", at("09:05"),
                          resolution="confirmed", reason="查实违规锚泊")
        self.assertEqual(e.get(rid).phase, Phase.BOARDED)
        # 问题属实自动排复查（原班人马）
        self.assertIsNotNone(e.get(rid).recheck_task_id)
        e.recheck_result(rid, "水上-周", at("11:30"), passed=True)
        risk = e.get(rid)
        self.assertEqual(risk.phase, Phase.CLOSED)
        self.assertEqual(risk.resolution, Resolution.RECTIFIED)
        self.assertTrue(risk.locked)
        self.assertEqual(
            risk.phase_path(),
            [Phase.DETECTED, Phase.REMOTE_VERIFIED, Phase.PUSHED, Phase.CALLED,
             Phase.DISPATCHED, Phase.BOARDED, Phase.RECHECKED, Phase.CLOSED],
        )

    def test_boarding_no_violation_closes_without_recheck(self):
        rid = self._anchor_risk()
        e = self.engine
        e.remote_verify(rid, "电子-林", at("08:05"))
        e.push_warning(rid, "值班-张", at("08:06"))
        e.call_vessel(rid, "值班-张", at("08:08"), answered=False)
        e.dispatch_boat(rid, "值班长-王", at("08:20"),
                        boat_id="PT-01", person_ids=["P-01", "P-02", "P-03"])
        e.boarding_result(rid, "水上-梁", at("09:05"),
                          resolution="no_violation")
        self.assertEqual(e.get(rid).phase, Phase.CLOSED)
        self.assertIsNone(e.get(rid).recheck_task_id)

    def test_call_self_rectified_closes(self):
        rid = self._anchor_risk()
        e = self.engine
        e.remote_verify(rid, "电子-林", at("08:05"))
        e.push_warning(rid, "值班-张", at("08:06"))
        e.call_vessel(rid, "值班-张", at("08:08"),
                      answered=True, self_rectified=True)
        self.assertEqual(e.get(rid).phase, Phase.CLOSED)

    def test_cannot_skip_phases(self):
        rid = self._anchor_risk()
        with self.assertRaises(DomainError):
            self.engine.dispatch_boat(
                rid, "王", T0, boat_id="PT-01",
                person_ids=["P-01", "P-02", "P-03"])

    def test_revoke_only_before_boarding(self):
        rid = self._anchor_risk()
        self.engine.remote_verify(rid, "电子-林", at("08:05"))
        self.engine.push_warning(rid, "值班-张", at("08:06"))
        self.engine.revoke(rid, "值班-张", at("08:07"),
                           reason="雷达杂波")
        self.assertEqual(self.engine.get(rid).phase, Phase.REVOKED)
        # 撤销后任何处置都被拒绝（终态）
        with self.assertRaises(DomainError):
            self.engine.push_warning(rid, "张", at("08:07"))
        # 原预警被标记失效，并发出撤销通知
        pushes = [n for n in self.engine.notifications if n.kind == "push"]
        self.assertTrue(pushes and pushes[0].revoked)
        self.assertTrue(any(n.kind == "revoke" for n in self.engine.notifications))

    def test_failed_recheck_reschedules(self):
        rid = self._anchor_risk()
        e = self.engine
        e.remote_verify(rid, "林", at("08:05"))
        e.push_warning(rid, "张", at("08:06"))
        e.call_vessel(rid, "张", at("08:08"), answered=False)
        e.dispatch_boat(rid, "王", at("08:20"),
                        boat_id="PT-01", person_ids=["P-01", "P-02", "P-03"])
        e.boarding_result(rid, "梁", at("09:05"),
                          resolution="confirmed")
        first_recheck = e.get(rid).recheck_task_id
        e.recheck_result(rid, "周", at("11:30"), passed=False)
        self.assertEqual(e.get(rid).phase, Phase.RECHECKED)
        self.assertNotEqual(e.get(rid).recheck_task_id, first_recheck)

    def test_repeated_failed_rechecks_until_pass(self):
        rid = self._anchor_risk()
        e = self.engine
        e.remote_verify(rid, "林", at("08:05"))
        e.push_warning(rid, "张", at("08:06"))
        e.call_vessel(rid, "张", at("08:08"), answered=False)
        e.dispatch_boat(rid, "王", at("08:20"),
                        boat_id="PT-01", person_ids=["P-01", "P-02", "P-03"])
        e.boarding_result(rid, "梁", at("09:05"), resolution="confirmed")
        e.recheck_result(rid, "周", at("11:30"), passed=False)
        t2 = e.get(rid).recheck_task_id
        # 第二次复查仍未通过（同阶段允许再次提交），再排一次
        e.recheck_result(rid, "周", at("14:00"), passed=False)
        self.assertEqual(e.get(rid).phase, Phase.RECHECKED)
        self.assertNotEqual(e.get(rid).recheck_task_id, t2)
        e.recheck_result(rid, "周", at("16:30"), passed=True)
        self.assertEqual(e.get(rid).phase, Phase.CLOSED)


class IdentityTest(unittest.TestCase):
    def setUp(self):
        self.engine = ClosureEngine(CANAL)

    def test_registry_binds_ais_radar_video_to_one_cluster(self):
        e = self.engine
        e.ingest(sight("MMSI:413005678", T0, km=41.10))
        e.ingest(detection("RADAR-1", at("08:01"), km=41.11))
        e.ingest(detection("VID-1", at("08:02"),
                           source="video", device="CAM-01", km=41.11,
                           name="桂钦散2317"))
        key = e.resolver.cluster_key("RADAR-1")
        self.assertEqual(key, "桂钦散2317")
        for tok in ("MMSI:413005678", "RADAR-1", "VID-1"):
            self.assertEqual(e.resolver.cluster_key(tok), "桂钦散2317")
        # 解释链包含三类规则依据
        chain = "\n".join(e.resolver.explain(key))
        self.assertIn("登记库绑定", chain)
        self.assertIn("时空邻近", chain)
        self.assertIn("牌证→登记库匹配", chain)

    def test_ambiguous_radar_track_pends_and_blocks_dispatch(self):
        e = self.engine
        # 两艘不同登记船在同地同时（锚地内）
        e.ingest(sight("MMSI:413001234", T0, km=41.15))
        e.ingest(sight("MMSI:413009012", T0, km=41.18))
        r = e.ingest(detection("RADAR-A", at("08:01"),
                               km=41.16, ref="amb")).risk_id
        self.assertTrue(e.get(r).identity_pending)
        e.remote_verify(r, "林", at("08:03"))
        e.push_warning(r, "张", at("08:04"))
        e.call_vessel(r, "张", at("08:05"), answered=False)
        with self.assertRaises(DomainError):
            e.dispatch_boat(r, "王", at("08:10"),
                            boat_id="PT-01", person_ids=["P-01", "P-02", "P-03"])
        # 人工确认后才能派艇
        e.decide_identity("RADAR-A", "平陆货0689", "张",
                          at("08:06"), confirm=True)
        self.assertFalse(e.get(r).identity_pending)
        e.dispatch_boat(r, "王", at("08:12"),
                        boat_id="PT-01", person_ids=["P-01", "P-02", "P-03"])
        self.assertEqual(e.get(r).cluster_key, "平陆货0689")

    def test_manual_deny_blocks_later_evidence(self):
        e = self.engine
        e.ingest(sight("MMSI:413001234", T0, km=41.15))
        e.ingest(sight("MMSI:413009012", T0, km=41.18))
        e.ingest(detection("RADAR-B", at("08:01"), km=41.16))
        e.decide_identity("RADAR-B", "平陆货0689", "张",
                          at("08:05"), confirm=False)
        # 再补一条强烈支持平陆货0689的"迟到证据"，仍不得连通
        e.ingest(detection("RADAR-B", at("08:02"),
                           recv=at("09:00"), km=41.15))
        self.assertNotEqual(e.resolver.cluster_key("RADAR-B"), "平陆货0689")
        # 人工确认为另一艘后连通
        e.decide_identity("RADAR-B", "远大集0056", "张",
                          at("08:06"), confirm=True)
        self.assertEqual(e.resolver.cluster_key("RADAR-B"), "远大集0056")


class LateDataTest(unittest.TestCase):
    def setUp(self):
        self.engine = ClosureEngine(CANAL)

    def _closed_confirmed_risk(self):
        e = self.engine
        e.ingest(sight("MMSI:413005678", T0, km=41.10))
        rid = e.ingest(detection("RADAR-1", T0, ref="r")).risk_id
        e.remote_verify(rid, "林", at("08:05"))
        e.push_warning(rid, "张", at("08:06"))
        e.call_vessel(rid, "张", at("08:08"), answered=False)
        e.dispatch_boat(rid, "王", at("08:20"),
                        boat_id="PT-01", person_ids=["P-01", "P-02", "P-03"])
        e.boarding_result(rid, "梁", at("09:05"),
                          resolution="confirmed")
        e.recheck_result(rid, "周", at("11:30"), passed=True)
        return rid

    def test_late_evidence_after_close_does_not_reopen(self):
        rid = self._closed_confirmed_risk()
        n_before = len(self.engine.risks)
        result = self.engine.ingest(detection(
            "RADAR-1", at("08:30"),
            recv=at("12:30")))
        self.assertEqual(result.risk_id, rid)
        self.assertEqual(self.engine.get(rid).phase, Phase.CLOSED)
        self.assertEqual(len(self.engine.risks), n_before)  # 未另开新单
        late = [self.engine.evidence[i] for i in self.engine.get(rid).evidence_ids
                if self.engine.evidence[i].delayed]
        self.assertTrue(late)

    def test_late_evidence_after_revoke_does_not_revive(self):
        e = self.engine
        rid = e.ingest(detection("RADAR-9", T0, km=40.0, ref="f")).risk_id
        e.remote_verify(rid, "林", at("08:03"),
                        verdict="dismiss", reason="杂波")
        self.assertEqual(e.get(rid).phase, Phase.REVOKED)
        e.ingest(detection("RADAR-9", at("08:02"),
                           recv=at("09:30"), km=40.0))
        self.assertEqual(e.get(rid).phase, Phase.REVOKED)
        self.assertEqual(e.get(rid).resolution, Resolution.FALSE_ALARM)

    def test_late_onsite_batch_attaches_to_risk(self):
        rid = self._closed_confirmed_risk()
        self.engine.ingest({
            "eid": "EV-LATE-ON", "etype": "onsite", "source": "onsite",
            "device_id": "PDA-1", "at": at("09:20"),
            "recv": at("13:00"),
            "segment_id": "SEG-02", "chainage_km": 41.12, "risk_id": rid,
            "data": {"note": "回码头后补传"},
        })
        self.assertIn("EV-LATE-ON", self.engine.get(rid).evidence_ids)

    def test_correlation_dedupes_same_ticket(self):
        e = self.engine
        e.ingest(sight("MMSI:413005678", T0, km=41.10))
        rid1 = e.ingest(detection("RADAR-1", T0, km=41.12)).risk_id
        rid2 = e.ingest(detection("VID-1", at("08:02"),
                                  source="video", device="CAM-01",
                                  km=41.12, name="桂钦散2317")).risk_id
        self.assertEqual(rid1, rid2)


class DispatchTest(unittest.TestCase):
    def setUp(self):
        self.engine = ClosureEngine(CANAL)
        e = self.engine
        e.ingest(sight("MMSI:413005678", T0, km=41.10))
        self.rid = e.ingest(detection("RADAR-1", T0, ref="r")).risk_id
        e.remote_verify(self.rid, "林", at("08:05"))
        e.push_warning(self.rid, "张", at("08:06"))
        e.call_vessel(self.rid, "张", at("08:08"), answered=False)

    def test_boat_and_person_double_booking_rejected(self):
        e = self.engine
        e.dispatch_boat(self.rid, "王", at("08:20"),
                        boat_id="PT-01", person_ids=["P-01", "P-02", "P-03"],
                        duration_min=60)
        rid2 = e.ingest(detection("RADAR-2", at("08:25"),
                                  km=96.0, seg="SEG-04", ref="r2")).risk_id
        e.remote_verify(rid2, "林", at("08:26"))
        e.push_warning(rid2, "张", at("08:27"))
        e.call_vessel(rid2, "张", at("08:28"), answered=False)
        with self.assertRaises(DomainError) as ctx:
            e.dispatch_boat(rid2, "王", at("08:40"),
                            boat_id="PT-01", person_ids=["P-01", "P-02", "P-03"],
                            duration_min=60)
        self.assertIn("时间冲突", str(ctx.exception))

    def test_repair_boat_unavailable(self):
        with self.assertRaises(DomainError):
            self.engine.dispatch_boat(
                self.rid, "王", at("08:20"),
                boat_id="PT-03", person_ids=["P-01", "P-02", "P-03"])

    def test_crew_roles_required(self):
        for bad_crew in (["P-01"], ["P-02", "P-03"], ["P-01", "P-04"]):
            with self.assertRaises(DomainError):
                self.engine.dispatch_boat(
                    self.rid, "王", at("08:20"),
                    boat_id="PT-01", person_ids=bad_crew)

    def test_occupied_berth_rejected_and_free_berth_chosen(self):
        e = self.engine
        # B2 在 09:00-12:00 被样例拖轮占用
        with self.assertRaises(DomainError):
            e.dispatch_boat(self.rid, "王", at("10:00"),
                            boat_id="PT-01", person_ids=["P-01", "P-02", "P-03"],
                            anchorage_id="ANC-01", berth_id="ANC-01-B2",
                            duration_min=60)
        result = e.dispatch_boat(
            self.rid, "王", at("10:00"),
            boat_id="PT-01", person_ids=["P-01", "P-02", "P-03"],
            anchorage_id="ANC-01", duration_min=60)
        task = e.dispatcher._task(result.task_id)
        self.assertNotEqual(task.berth_id, "ANC-01-B2")
        self.assertIsNotNone(task.berth_id)

    def test_revocation_cancels_boat_task_and_frees_berth(self):
        e = self.engine
        result = e.dispatch_boat(
            self.rid, "王", at("10:00"),
            boat_id="PT-01", person_ids=["P-01", "P-02", "P-03"],
            anchorage_id="ANC-01", berth_id="ANC-01-B3", duration_min=60)
        e.revoke(self.rid, "张", at("10:05"), reason="误报")
        task = e.dispatcher._task(result.task_id)
        self.assertEqual(task.status, "cancelled")
        # 泊位已释放：同泊位可再排
        rid2 = e.ingest(detection("RADAR-7", at("10:06"),
                                  ref="r2")).risk_id
        e.remote_verify(rid2, "林", at("10:07"))
        e.push_warning(rid2, "张", at("10:08"))
        e.call_vessel(rid2, "张", at("10:09"), answered=False)
        again = e.dispatch_boat(
            rid2, "王", at("10:20"),
            boat_id="PT-01", person_ids=["P-01", "P-02", "P-03"],
            anchorage_id="ANC-01", berth_id="ANC-01-B3", duration_min=30)
        self.assertTrue(again.ok)


class WaterLevelTest(unittest.TestCase):
    def test_low_water_creates_risk_per_vessel_and_closes_by_call(self):
        e = ClosureEngine(CANAL)
        e.ingest(sight("MMSI:413003344", "2026-09-30T07:40:00+08:00",
                       seg="SEG-03", km=70.02))
        result = e.ingest({
            "eid": "WL-1", "etype": "water_level", "source": "hydro",
            "device_id": "WLV-02", "at": "2026-09-30T07:45:00+08:00",
            "segment_id": "SEG-03", "chainage_km": 70.0,
            "event_type": "low_ukc",
            "data": {"level_m": 3.42, "threshold_m": 3.8},
        })
        rid = result.risk_id
        self.assertEqual(e.get(rid).risk_type, RiskType.LOW_UKC)
        self.assertIn("WL-1", e.get(rid).evidence_ids)
        e.remote_verify(rid, "林", "2026-09-30T07:50:00+08:00")
        e.push_warning(rid, "张", "2026-09-30T07:51:00+08:00")
        e.call_vessel(rid, "张", "2026-09-30T07:53:00+08:00",
                      answered=True, self_rectified=True)
        self.assertEqual(e.get(rid).phase, Phase.CLOSED)


class LineageTest(unittest.TestCase):
    def test_lineage_traces_back_to_original_devices(self):
        e = ClosureEngine(CANAL)
        e.ingest(sight("MMSI:413005678", T0, km=41.10))
        rid = e.ingest(detection("RADAR-1", T0, eid="EV-R", ref="r")).risk_id
        e.ingest(detection("VID-1", at("08:02"),
                           source="video", device="CAM-01",
                           name="桂钦散2317", eid="EV-V"))
        line = e.lineage(rid)
        sources = {x.get("source") for x in line["timeline"] if x["kind"] == "evidence"}
        self.assertEqual(sources, {"radar", "video", "ais"})
        radar = next(x for x in line["timeline"]
                     if x["kind"] == "evidence" and x["source"] == "radar")
        self.assertEqual(radar["device_id"], "RDR-01")
        self.assertEqual(radar["device_name"], "马道雷达站")
        self.assertTrue(line["identity_chain"])


class MergeAndReachabilityTest(unittest.TestCase):
    def test_identity_merge_cancels_duplicate_boarding_task(self):
        e = ClosureEngine(CANAL)
        # 两个匿名雷达批号各自立案并派艇，事后人工判定为同一艘船
        e.ingest(sight("MMSI:413001234", T0, km=41.10))
        e.ingest(sight("MMSI:413009012", T0, km=41.13))
        ra = e.ingest(detection("RADAR-T1", at("08:01"), km=41.11, ref="a")).risk_id
        rb = e.ingest(detection("RADAR-T2", at("08:02"), km=41.12, ref="b")).risk_id
        for rid in (ra, rb):
            e.remote_verify(rid, "林", at("08:04"))
            e.push_warning(rid, "张", at("08:05"))
            e.call_vessel(rid, "张", at("08:06"), answered=False)
        t1 = e.dispatch_boat(ra, "王", at("08:20"), boat_id="PT-01",
                             person_ids=["P-01", "P-02", "P-03"],
                             anchorage_id="ANC-01", override_identity=True)
        t2 = e.dispatch_boat(rb, "王", at("11:00"), boat_id="PT-02",
                             person_ids=["P-04", "P-05"],
                             anchorage_id="ANC-01", override_identity=True)
        e.decide_identity("RADAR-T1", "平陆货0689", "张", at("08:10"), confirm=True)
        e.decide_identity("RADAR-T2", "平陆货0689", "张", at("08:11"), confirm=True)
        self.assertEqual(e.get(rb).merged_into, ra)
        self.assertEqual(e.dispatcher._task(t2.task_id).status, "cancelled")
        self.assertEqual(e.get(ra).board_task_id, t1.task_id)
        self.assertIn("身份归并", "\n".join(e.get(ra).notes))

    def test_boat_cannot_reach_distant_segment_in_time(self):
        e = ClosureEngine(CANAL)
        # PT-01 在 SEG-02（里程41）的任务 09:20 结束
        e.ingest(sight("MMSI:413005678", T0, km=41.10))
        r1 = e.ingest(detection("RADAR-1", T0, ref="r1")).risk_id
        e.remote_verify(r1, "林", at("08:05"))
        e.push_warning(r1, "张", at("08:06"))
        e.call_vessel(r1, "张", at("08:08"), answered=False)
        e.dispatch_boat(r1, "王", at("08:20"), boat_id="PT-01",
                        person_ids=["P-01", "P-02", "P-03"], duration_min=60)
        # 09:30 又在 SEG-04（里程96）发现警情，同艇 55 公里赶不到
        e.ingest(sight("MMSI:413009012", at("09:25"), seg="SEG-04", km=96.0))
        r2 = e.ingest(detection("RADAR-2", at("09:26"), seg="SEG-04",
                                km=96.0, ref="r2")).risk_id
        e.remote_verify(r2, "林", at("09:27"))
        e.push_warning(r2, "张", at("09:28"))
        e.call_vessel(r2, "张", at("09:29"), answered=False)
        with self.assertRaises(DomainError) as ctx:
            e.dispatch_boat(r2, "王", at("09:30"), boat_id="PT-01",
                            person_ids=["P-01", "P-02", "P-03"])
        self.assertIn("最快", str(ctx.exception))


class ScenarioFixtureTest(unittest.TestCase):
    def test_demo_scenario_all_expectations_pass(self):
        result = Replay.from_files(
            "fixtures/canal.json", "fixtures/events.json").run()
        failures = [c for c in result["checks"] if not c["ok"]]
        self.assertEqual(failures, [], msg=json.dumps(failures, ensure_ascii=False))
        self.assertTrue(result["passed"])

    def test_duty_officer_sees_only_unclosed(self):
        result = Replay.from_files(
            "fixtures/canal.json", "fixtures/events.json").run()
        ids = {r["risk_id"] for r in result["board"]["risks"]}
        # 剧本结束只剩气象预警未闭环
        self.assertEqual(len(ids), 1)
        self.assertEqual(result["board"]["risks"][0]["type"], "nav_rule_violation")


if __name__ == "__main__":
    unittest.main()

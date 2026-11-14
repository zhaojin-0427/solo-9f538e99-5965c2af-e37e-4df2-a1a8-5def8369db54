"""核心规则与上传流程的自动化测试。

运行：python -m unittest discover -s tests -v
"""
import io
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app  # noqa: E402
from db import get_db  # noqa: E402
from processing import BatchError, pair_events, parse_csv  # noqa: E402

HEADER = "event_id,card_no,event_time,station,direction,revision\n"


def csv_of(*rows: str) -> str:
    return HEADER + "\n".join(rows) + "\n"


class ParseCsvTest(unittest.TestCase):
    def test_ok_and_alias_and_naive_time(self):
        rows, dups = parse_csv(csv_of(
            "E1,C1,2026-11-10 08:00:00,Alpha,进站,1",
            "E2,C1,2026-11-10T09:00:00+01:00,Beta,out,1",
        ))
        self.assertEqual(dups, 0)
        self.assertEqual(rows[0].event_time, "2026-11-10T08:00:00Z")  # 朴素时间按 UTC
        self.assertEqual(rows[0].direction, "in")                      # 中文别名
        self.assertEqual(rows[1].event_time, "2026-11-10T08:00:00Z")  # 偏移换算到 UTC

    def test_missing_column_rejected(self):
        with self.assertRaises(BatchError):
            parse_csv("event_id,card_no\nE1,C1\n")

    def test_conflict_within_file_rejected(self):
        with self.assertRaises(BatchError):
            parse_csv(csv_of(
                "E1,C1,2026-11-10T08:00:00Z,Alpha,in,1",
                "E1,C1,2026-11-10T08:05:00Z,Alpha,in,1",  # 同事件同修订，时间不同
            ))

    def test_identical_duplicate_deduped(self):
        rows, dups = parse_csv(csv_of(
            "E1,C1,2026-11-10T08:00:00Z,Alpha,in,1",
            "E1,C1,2026-11-10T08:00:00Z,Alpha,in,1",
        ))
        self.assertEqual((len(rows), dups), (1, 1))

    def test_bad_revision_rejected(self):
        with self.assertRaises(BatchError):
            parse_csv(csv_of("E1,C1,2026-11-10T08:00:00Z,Alpha,in,-1"))


class PairEventsTest(unittest.TestCase):
    @staticmethod
    def ev(eid, time, direction, card="C1", station="S", rev=1):
        return {"event_id": eid, "event_time": time, "direction": direction,
                "card_no": card, "station": station, "revision": rev}

    def test_basic_pair(self):
        trips, anomalies = pair_events([
            self.ev("O1", "2026-11-10T09:00:00Z", "out"),  # 乱序输入也应按时间排
            self.ev("I1", "2026-11-10T08:00:00Z", "in"),
        ])
        self.assertEqual(len(trips), 1)
        self.assertEqual(trips[0]["duration_s"], 3600)
        self.assertEqual(anomalies, [])

    def test_reentry_makes_previous_entry_anomaly(self):
        trips, anomalies = pair_events([
            self.ev("I1", "2026-11-10T08:00:00Z", "in"),
            self.ev("I2", "2026-11-10T08:30:00Z", "in"),
            self.ev("O1", "2026-11-10T09:00:00Z", "out"),
        ])
        self.assertEqual([t["in"]["event_id"] for t in trips], ["I2"])
        self.assertEqual(len(anomalies), 1)
        self.assertEqual(anomalies[0]["event_id"], "I1")
        self.assertEqual(anomalies[0]["atype"], "duplicate_entry")

    def test_orphan_exit(self):
        trips, anomalies = pair_events([self.ev("O1", "2026-11-10T09:00:00Z", "out")])
        self.assertEqual(trips, [])
        self.assertEqual(anomalies[0]["atype"], "orphan_exit")

    def test_over_24h_not_paired(self):
        trips, anomalies = pair_events([
            self.ev("I1", "2026-11-10T06:00:00Z", "in"),
            self.ev("O1", "2026-11-11T06:00:01Z", "out"),  # 24h+1s
        ])
        self.assertEqual(trips, [])
        self.assertEqual({a["atype"] for a in anomalies},
                         {"entry_expired", "orphan_exit"})

    def test_exactly_24h_pairs(self):
        trips, anomalies = pair_events([
            self.ev("I1", "2026-11-10T06:00:00Z", "in"),
            self.ev("O1", "2026-11-11T06:00:00Z", "out"),  # 恰好 24h
        ])
        self.assertEqual(len(trips), 1)
        self.assertEqual(anomalies, [])

    def test_entry_without_exit_at_end(self):
        trips, anomalies = pair_events([self.ev("I1", "2026-11-10T06:00:00Z", "in")])
        self.assertEqual(anomalies[0]["atype"], "entry_without_exit")

    def test_first_exit_within_window_wins(self):
        trips, anomalies = pair_events([
            self.ev("I1", "2026-11-10T08:00:00Z", "in"),
            self.ev("O1", "2026-11-10T08:30:00Z", "out"),
            self.ev("O2", "2026-11-10T09:00:00Z", "out"),  # 多余的出站 → 孤立
        ])
        self.assertEqual([t["out"]["event_id"] for t in trips], ["O1"])
        self.assertEqual(anomalies[0]["event_id"], "O2")


class UploadFlowTest(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(self.db_path)
        self.app = create_app(self.db_path)
        self.client = self.app.test_client()

    def tearDown(self):
        if os.path.exists(self.db_path):
            os.unlink(self.db_path)

    # ------------------------------------------------------------- 辅助

    def upload(self, text: str, filename="t.csv"):
        return self.client.post(
            "/upload",
            data={"file": (io.BytesIO(text.encode("utf-8")), filename)},
            follow_redirects=True,
        )

    def current_events(self):
        with self.app.app_context():
            rows = get_db().execute(
                "SELECT event_id, revision, card_no, event_time, station, direction "
                "FROM current_events ORDER BY event_id").fetchall()
            return [dict(r) for r in rows]

    def trips_of(self, card):
        html = self.client.get(f"/cards/{card}").data.decode()
        return html

    # ------------------------------------------------------------- 用例

    def test_upload_and_view_trips(self):
        resp = self.upload(csv_of(
            "E1,C1,2026-11-10T08:00:00Z,Alpha,in,1",
            "E2,C1,2026-11-10T08:45:00Z,Beta,out,1",
        ))
        self.assertEqual(resp.status_code, 200)
        self.assertIn("上传成功", resp.data.decode())
        html = self.trips_of("C1")
        self.assertIn("配对行程（1）", html)
        self.assertIn("Alpha", html)
        self.assertIn("待核查异常（0）", html)

    def test_higher_revision_replaces_and_moves_card(self):
        self.upload(csv_of(
            "E1,C1,2026-11-10T08:00:00Z,Alpha,in,1",
            "E2,C1,2026-11-10T08:45:00Z,Beta,out,1",
        ))
        # 更正：E2 改到卡 C2、时间也变了 → 旧卡 C1 与新卡 C2 一起更新
        self.upload(csv_of("E2,C2,2026-11-10T08:50:00Z,Beta,out,2"))
        events = {e["event_id"]: e for e in self.current_events()}
        self.assertEqual(events["E2"]["card_no"], "C2")
        self.assertEqual(events["E2"]["revision"], 2)
        # C1 只剩进站（变成无出站异常），C2 只有孤立出站
        self.assertIn("待核查异常（1）", self.trips_of("C1"))
        self.assertIn("孤立出站", self.trips_of("C2"))
        self.assertIn("配对行程（0）", self.trips_of("C1"))

    def test_stale_revision_ignored(self):
        self.upload(csv_of("E1,C1,2026-11-10T08:00:00Z,Alpha,in,2"))
        resp = self.upload(csv_of("E1,C1,2026-11-10T07:00:00Z,Alpha,in,1"))
        self.assertIn("忽略过期修订 1 条", resp.data.decode())
        events = self.current_events()
        self.assertEqual(events[0]["event_time"], "2026-11-10T08:00:00Z")

    def test_conflict_with_archive_rejected_and_state_preserved(self):
        self.upload(csv_of("E1,C1,2026-11-10T08:00:00Z,Alpha,in,1"))
        before = self.current_events()
        resp = self.upload(csv_of(
            "E1,C1,2026-11-10T07:00:00Z,Alpha,in,1",      # 与存档同修订冲突
            "E9,C9,2026-11-10T09:00:00Z,Alpha,in,1",      # 同批其他行也不应落库
        ))
        self.assertIn("整批拒绝", resp.data.decode())
        self.assertEqual(self.current_events(), before)   # 原结果不变
        self.assertEqual(self.client.get("/cards/C9").status_code, 404)

    def test_conflict_within_file_rejected(self):
        resp = self.upload(csv_of(
            "E1,C1,2026-11-10T08:00:00Z,Alpha,in,1",
            "E1,C1,2026-11-10T08:05:00Z,Alpha,in,1",
        ))
        self.assertIn("整批拒绝", resp.data.decode())
        self.assertEqual(self.current_events(), [])

    def test_reupload_identical_is_noop(self):
        body = csv_of("E1,C1,2026-11-10T08:00:00Z,Alpha,in,1")
        self.upload(body)
        resp = self.upload(body)
        self.assertIn("重复/一致 1 条", resp.data.decode())
        self.assertEqual(len(self.current_events()), 1)

    def test_bad_file_keeps_previous_results(self):
        self.upload(csv_of(
            "E1,C1,2026-11-10T08:00:00Z,Alpha,in,1",
            "E2,C1,2026-11-10T08:45:00Z,Beta,out,1",
        ))
        before = self.current_events()
        resp = self.upload("not,a,valid\n1,2,3\n")
        self.assertIn("拒绝", resp.data.decode())
        self.assertEqual(self.current_events(), before)
        html = self.trips_of("C1")
        self.assertIn("配对行程（1）", html)  # 查询结果不受失败上传影响

    def test_unknown_card_404(self):
        self.assertEqual(self.client.get("/cards/NOPE").status_code, 404)


if __name__ == "__main__":
    unittest.main()

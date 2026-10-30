"""Team adapter acceptance tests: real Ops, synthetic evidence, no external writes."""

from datetime import timedelta
import tempfile
import unittest
from pathlib import Path

from support import Clock, rules
from waset_ops.team import TeamOps
from waset_runtime.team import (
    TeamRunner,
    validate_measurements,
    direct_url,
    snapshot,
    COL,
)


class TeamCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = Clock()
        self.ops = TeamOps(
            Path(self.tmp.name) / "s.db",
            clock=self.clock,
            config={"notify_published": False},
        )
        self.ops.store.migrate()
        self.ops.setup_team()
        self.snap = {
            "id": "1",
            "name": "test",
            "status": "In Progress",
            "format": "Post",
            "code": "TEST",
            "caption": "Reviewed caption",
            "collab": "",
            "topaz": "Yess",
            "resolution": "Yess",
            "notes": "Human note",
            "ready_file": "https://www.dropbox.com/scl/fi/test/video.mp4?rlkey=test",
            "publish_at": rules.iso(self.ops.now_dt() + timedelta(minutes=10)),
        }
        self.info = {
            "team_verified": True,
            "assetKey": "id:file@v1",
            "sha256": "sha",
            "content_hash": "hash",
            "url": "https://www.dropbox.com/scl/fi/test/video.mp4?raw=1",
            "format": "Post",
            "delivered": {"id": "id:file", "rev": "v1"},
        }

    def approve(self, s=None, info=None):
        s = s or self.snap
        iid = self.ops.team_observe("team", s)
        s["status"] = "Ready to Publish"
        self.ops.team_observe("team", s)
        self.ops.team_verified(iid, info or self.info)
        return iid

    def state(self, iid):
        with self.ops.store.read() as c:
            return (
                self.ops.item(c, iid),
                self.ops.team(c, iid),
                self.ops.reservation(c, iid),
            )

    def test_initial_ready_requires_new_transition(self):
        self.snap["status"] = "Ready to Publish"
        iid = self.ops.team_observe("team", self.snap)
        self.ops.team_observe("team", self.snap)
        self.assertIsNone(self.state(iid)[1]["approved"])
        self.assertIsNone(self.state(iid)[2])
        self.snap["status"] = "In Progress"
        self.ops.team_observe("team", self.snap)
        self.snap["status"] = "Ready to Publish"
        self.ops.team_observe("team", self.snap)
        self.assertIsNotNone(self.state(iid)[1]["approved"])

    def test_claim_early_commit_only_when_due(self):
        iid = self.approve()
        cl = self.ops.claim(iid, "test")
        self.assertTrue(cl["claimed"], cl)
        self.ops.container(cl["attempt_id"], "test", cl["fence"], "container")
        out = self.ops.commit(
            cl["attempt_id"],
            "test",
            cl["fence"],
            source_asset="id:file@v1",
            delivered_asset="id:file@v1",
            container_status="FINISHED",
            record="store",
        )
        self.assertFalse(out["committed"])

    def test_content_edits_invalidate(self):
        for key in (
            "caption",
            "collab",
            "code",
            "format",
            "ready_file",
            "topaz",
            "resolution",
        ):
            with self.subTest(key=key):
                s = {**self.snap, "id": key, "status": "In Progress"}
                iid = self.approve(s, {**self.info, "sha256": key, "content_hash": key})
                s[key] = "changed"
                self.ops.team_observe("team", s)
                self.assertIsNone(self.state(iid)[1]["approved"])
                self.assertIsNone(self.state(iid)[2])

    def test_reschedule_preserves_review_and_status_cancels(self):
        iid = self.approve()
        self.snap["publish_at"] = rules.iso(self.ops.now_dt() + timedelta(hours=1))
        self.ops.team_observe("team", self.snap)
        self.assertEqual(self.state(iid)[2]["slot"], self.snap["publish_at"])
        self.snap["status"] = "Cancelled"
        self.ops.team_observe("team", self.snap)
        self.assertIsNone(self.state(iid)[2])

    def test_late_window(self):
        iid = self.approve()
        self.clock.advance(25 * 60 + 1)
        with self.ops.store.tx() as c:
            self.ops.try_schedule(c, iid, "test")
        self.assertIsNone(self.state(iid)[2])
        self.assertEqual(self.state(iid)[1]["desired_status"], "Blocked")

    def test_expired_unverified_item_is_blocked(self):
        iid = self.ops.team_observe("team", self.snap)
        self.snap["status"] = "Ready to Publish"
        self.ops.team_observe("team", self.snap)
        self.clock.advance(1501)
        self.ops.team_observe("team", self.snap)
        self.assertIsNone(self.state(iid)[1]["approved"])
        self.assertEqual(self.state(iid)[1]["desired_status"], "Blocked")

    def test_version_changed(self):
        iid = self.approve()
        self.ops.team_verified(iid, {**self.info, "assetKey": "id:file@v2"})
        self.assertIsNone(self.state(iid)[1]["approved"])

    def test_duplicate_and_slot_conflict(self):
        self.approve()
        s = {**self.snap, "id": "2", "status": "In Progress"}
        iid = self.approve(s)
        self.assertIsNone(self.state(iid)[2])
        s = {**self.snap, "id": "3", "code": "OTHER", "status": "In Progress"}
        iid = self.approve(
            s,
            {
                **self.info,
                "assetKey": "other@v1",
                "sha256": "other",
                "content_hash": "other",
            },
        )
        self.assertIsNone(self.state(iid)[2])
        self.assertIn("same format", self.state(iid)[1]["reason"])

    def test_legacy_deleted_item_receipt_blocks_team(self):
        with self.ops.store.tx() as c:
            c.execute(
                "INSERT INTO assets VALUES(?,?)",
                (rules.ACCOUNT + "|Post|src:hash", "old-board-item"),
            )
        iid = self.approve()
        self.assertIsNone(self.state(iid)[2])
        self.assertIn("old-board-item", self.state(iid)[1]["reason"])

    def test_publish_receipt_survives_no_monday(self):
        iid = self.approve()
        self.clock.advance(600)
        self.ops.team_observe("team", self.snap)
        cl = self.ops.claim(iid, "test")
        self.assertTrue(cl["claimed"], cl)
        self.ops.container(cl["attempt_id"], "test", cl["fence"], "container")
        out = self.ops.commit(
            cl["attempt_id"],
            "test",
            cl["fence"],
            source_asset="id:file@v1",
            delivered_asset="id:file@v1",
            container_status="FINISHED",
            record="store",
        )
        self.assertTrue(out["committed"], out)
        self.ops.result(cl["attempt_id"], "test", media_id="media")
        self.assertEqual(self.state(iid)[0]["publication"], "published")
        self.assertFalse(self.ops.claim(iid, "again")["claimed"])

    def test_unknown_never_republishes(self):
        iid = self.approve()
        self.clock.advance(600)
        self.ops.team_observe("team", self.snap)
        cl = self.ops.claim(iid, "test")
        self.ops.container(cl["attempt_id"], "test", cl["fence"], "container")
        self.ops.commit(
            cl["attempt_id"],
            "test",
            cl["fence"],
            source_asset="id:file@v1",
            delivered_asset="id:file@v1",
            container_status="FINISHED",
            record="store",
        )
        self.ops.result(cl["attempt_id"], "test", error="timeout")
        self.assertEqual(self.state(iid)[0]["publication"], "outcome_unknown")
        self.assertFalse(self.ops.claim(iid, "again")["claimed"])
        self.ops.reconcile(cl["attempt_id"], "PUBLISHED")
        self.assertEqual(self.state(iid)[0]["publication"], "published")

    def test_measurement_limits(self):
        def m(size=200_000_000, duration="120", width=1920, height=1080):
            return {
                "format": {
                    "size": str(size),
                    "duration": duration,
                    "format_name": "mov,mp4",
                },
                "streams": [
                    {
                        "codec_type": "video",
                        "width": width,
                        "height": height,
                        "codec_name": "h264",
                        "pix_fmt": "yuv420p",
                        "avg_frame_rate": "30/1",
                    }
                ],
            }

        self.assertEqual(validate_measurements(m(), "Post")["bytes"], 200_000_000)
        with self.assertRaises(ValueError):
            validate_measurements(m(), "Story")
        self.assertEqual(
            validate_measurements(
                m(95_000_000, duration="40", width=1080, height=1920), "Story"
            )["width"],
            1080,
        )
        for x in (m(300_000_000), m(duration="2"), m(width=1280), m(duration="nan")):
            with self.assertRaises(ValueError):
                validate_measurements(x, "Post")

    def test_link_and_monday_time(self):
        with self.assertRaises(ValueError):
            direct_url("http://localhost/video")
        self.assertIn("raw=1", direct_url(self.snap["ready_file"]))
        import json

        raw = {
            "id": "1",
            "column_values": [
                {
                    "id": COL["publish_at"],
                    "value": json.dumps({"date": "2026-10-30", "time": "15:00:00"}),
                }
            ],
        }
        self.assertEqual(snapshot(raw)["publish_at"], "2026-10-30T15:00:00Z")


class TeamRunnerCase(unittest.TestCase):
    setUp = TeamCase.setUp
    approve = TeamCase.approve
    state = TeamCase.state

    def runner(self):
        from unittest.mock import Mock
        import json

        def raw():
            cols = []
            for k, cid in COL.items():
                v = self.snap.get(k, "")
                value = {"url": v} if k == "ready_file" else {"text": v}
                if k == "publish_at":
                    dt = rules.instant(v) if v else None
                    value = (
                        {
                            "date": dt.strftime("%Y-%m-%d"),
                            "time": dt.strftime("%H:%M:%S"),
                        }
                        if dt
                        else {}
                    )
                cols.append({"id": cid, "text": v or "", "value": json.dumps(value)})
            return {"id": "1", "board": {"id": "team"}, "column_values": cols}

        self.monday = Mock()
        self.monday.items.side_effect = lambda *a, **k: [raw()]
        self.monday.board_items.side_effect = lambda *a, **k: [raw()]
        self.updates = []
        self.monday.updates.side_effect = lambda *a: {"updates": self.updates}
        self.monday.create_update.side_effect = lambda item, text: self.updates.append(
            {"text_body": text}
        )

        def change(item, cols, **kw):
            for k, cid in COL.items():
                if cid in cols:
                    self.snap[k] = cols[cid].get("label", cols[cid].get("text"))

        self.monday.change_columns.side_effect = change
        self.ig = Mock()
        self.ig.create_container.return_value = "container"
        self.ig.container_status.return_value = {"status_code": "FINISHED"}
        self.ig.publish.return_value = (200, {"id": "media"})
        self.ig.permalink.return_value = "https://www.instagram.com/reel/example/"
        self.verifier = Mock()
        self.verifier.identity.return_value = {"id": "id:file", "rev": "v1"}
        return TeamRunner(
            self.ops, self.monday, None, self.ig, "team", verifier=self.verifier
        )

    def test_preparation_then_publish_no_duplicate(self):
        iid = self.approve()
        r = self.runner()
        row = self.ops.team_candidates()[0]
        r.publish_one(row)
        self.ig.publish.assert_not_called()
        self.clock.advance(600)
        r.publish_one(row)
        self.ig.publish.assert_called_once()
        self.assertEqual(self.state(iid)[0]["publication"], "published")
        r.publish_one(row)
        self.ig.publish.assert_called_once()
        r.flush()
        count = len(self.updates)
        for _ in range(3):
            self.ops.team_observe("team", self.snap)
            r.flush()
        self.assertEqual(len(self.updates), count)
        self.assertEqual(self.snap["status"], "Published")
        self.assertTrue(self.snap["notes"].startswith("Human note"))

    def test_container_error_blocks_employee_review(self):
        iid = self.approve()
        r = self.runner()
        self.ig.container_status.return_value = {"status_code": "ERROR"}
        r.publish_one(self.ops.team_candidates()[0])
        self.ig.publish.assert_not_called()
        self.assertEqual(self.state(iid)[1]["desired_status"], "Blocked")

    def test_notes_added_after_system_section_survive_retry(self):
        iid = self.approve()
        r = self.runner()
        with self.ops.store.tx() as c:
            self.ops.team_stop(c, iid, "Blocked", "Bad file")
        self.monday.create_update.side_effect = ConnectionError("offline")
        r.flush()
        self.snap["notes"] += "\nEmployee added a note"
        self.clock.advance(3600)
        self.monday.create_update.side_effect = lambda item, text: self.updates.append(
            {"text_body": text}
        )
        r.flush()
        self.assertIn("Human note", self.snap["notes"])
        self.assertIn("Employee added a note", self.snap["notes"])
        self.assertEqual(self.snap["notes"].count("[Bondok]"), 1)

    def test_monday_outage_prevents_publish(self):
        iid = self.approve()
        r = self.runner()
        row = self.ops.team_candidates()[0]
        r.publish_one(row)
        self.clock.advance(600)
        self.monday.items.side_effect = ConnectionError("offline")
        with self.assertRaises(ConnectionError):
            r.publish_one(row)
        self.ig.publish.assert_not_called()
        self.assertEqual(self.state(iid)[0]["publication"], "not_started")

    def test_dropbox_outage_prevents_publish(self):
        self.approve()
        r = self.runner()
        self.clock.advance(600)
        self.verifier.identity.side_effect = ConnectionError("offline")
        with self.assertRaises(ConnectionError):
            r.publish_one(self.ops.team_candidates()[0])
        self.ig.publish.assert_not_called()

    def test_change_during_container_creation(self):
        iid = self.approve()
        r = self.runner()
        self.clock.advance(600)

        def created(payload):
            self.snap["caption"] = "new"
            return "container"

        self.ig.create_container.side_effect = created
        r.publish_one(self.ops.team_candidates()[0])
        self.ig.publish.assert_not_called()
        self.assertIsNone(self.state(iid)[1]["approved"])

    def test_file_revision_changed_same_link(self):
        iid = self.approve()
        r = self.runner()
        self.clock.advance(600)
        self.verifier.identity.return_value = {"id": "id:file", "rev": "v2"}
        r.publish_one(self.ops.team_candidates()[0])
        self.ig.publish.assert_not_called()
        self.assertEqual(self.state(iid)[1]["desired_status"], "In Progress")

    def test_restart_waits_lease_then_reuses_container(self):
        self.approve()
        r = self.runner()
        row = self.ops.team_candidates()[0]
        r.publish_one(row)
        other = TeamRunner(
            self.ops, self.monday, None, self.ig, "team", verifier=self.verifier
        )
        other.publish_one(row)
        self.ig.create_container.assert_called_once()
        self.clock.advance(600)
        other.publish_one(row)
        self.ig.create_container.assert_called_once()
        self.ig.publish.assert_called_once()

    def test_unknown_transport_never_retries(self):
        iid = self.approve()
        r = self.runner()
        row = self.ops.team_candidates()[0]
        self.clock.advance(600)
        self.ig.publish.side_effect = ConnectionError("no answer")
        r.publish_one(row)
        r.publish_one(row)
        self.ig.publish.assert_called_once()
        self.assertEqual(self.state(iid)[0]["publication"], "outcome_unknown")

    def test_monday_receipt_failure_retries_without_publish(self):
        self.approve()
        r = self.runner()
        self.clock.advance(600)
        r.publish_one(self.ops.team_candidates()[0])
        self.monday.create_update.side_effect = ConnectionError("offline")
        r.flush()
        self.assertEqual(self.snap["status"], "Published")
        self.clock.advance(3600)
        self.monday.create_update.side_effect = lambda item, text: self.updates.append(
            {"text_body": text}
        )
        r.flush()
        self.assertEqual(len(self.updates), 1)
        self.ig.publish.assert_called_once()

    def test_block_receipt_survives_status_write_then_crash(self):
        iid = self.approve()
        r = self.runner()
        with self.ops.store.tx() as c:
            self.ops.team_stop(c, iid, "Blocked", "Bad file")
        self.monday.create_update.side_effect = ConnectionError("offline")
        r.flush()
        self.assertEqual(self.snap["status"], "Blocked")
        self.clock.advance(3600)
        self.monday.create_update.side_effect = lambda item, text: self.updates.append(
            {"text_body": text}
        )
        r.flush()
        self.assertEqual(len(self.updates), 1)

    def test_cutover_reapproval_and_pause(self):
        iid = self.approve()
        with self.assertRaises(ValueError):
            self.ops.team_cutover(enable=True)
        self.ops.team_cutover(enable=True, authorized_by="test approval")
        self.assertIsNone(self.state(iid)[1]["approved"])
        self.ops.team_cutover(pause=True, authorized_by="test pause")
        self.assertFalse(self.ops.claim(iid, "worker")["claimed"])

    def test_pause_refuses_new_approval_and_legacy_runner(self):
        from waset_ops import Ops

        iid = self.approve()
        self.ops.team_cutover(pause=True, authorized_by="test")
        self.snap["status"] = "In Progress"
        self.ops.team_observe("team", self.snap)
        self.snap["status"] = "Ready to Publish"
        self.ops.team_observe("team", self.snap)
        self.assertIsNone(self.state(iid)[1]["approved"])
        with self.ops.store.tx() as c:
            c.execute("UPDATE ops_meta SET value='team' WHERE key='publishing_source'")
        legacy = Ops(self.ops.store.path, clock=self.clock)
        self.assertFalse(legacy.claim(iid, "old-worker")["claimed"])

    def test_post_story_and_collab_payload(self):
        for fmt, collab in [("Post", "@reviewed"), ("Story", "")]:
            with self.subTest(format=fmt):
                self.snap.update(
                    id=fmt, format=fmt, collab=collab, status="In Progress"
                )
                info = {
                    **self.info,
                    "assetKey": fmt + "@v1",
                    "format": fmt,
                    "content_hash": fmt,
                    "sha256": fmt,
                }
                iid = self.approve(info=info)
                with self.ops.store.read() as c:
                    p = self.ops.payload(c, self.ops.item(c, iid))
                self.assertEqual(
                    p["media_type"], "REELS" if fmt == "Post" else "STORIES"
                )
                self.assertEqual(
                    p.get("collaborators"), ["reviewed"] if collab else None
                )

    def test_cairo_date_specific_dst(self):
        from zoneinfo import ZoneInfo
        from datetime import datetime

        tz = ZoneInfo("Africa/Cairo")
        self.assertEqual(
            datetime(2026, 10, 29, 18, tzinfo=tz).utcoffset(), timedelta(hours=3)
        )
        self.assertEqual(
            datetime(2026, 10, 30, 18, tzinfo=tz).utcoffset(), timedelta(hours=2)
        )


class FileVerifierCase(unittest.TestCase):
    setUp = TeamCase.setUp

    def test_preflight_rejects_missing_review_invalid_collab_and_caption(self):
        from waset_runtime.team import FileVerifier
        from unittest.mock import Mock

        d = Mock()
        v = FileVerifier(d)
        for changes in [
            {"topaz": "Waiting"},
            {"resolution": "NO"},
            {"caption": ""},
            {"collab": "@one @two"},
            {"format": "Story", "collab": "one"},
            {"code": ""},
        ]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                v.verify({**self.snap, **changes})
        d.link_metadata.assert_not_called()

    def test_download_checks_hash_revision_and_corrupt_file(self):
        import hashlib, io, json
        from unittest.mock import Mock, patch
        from waset_runtime.team import FileVerifier

        import struct

        data = (
            struct.pack(">I4s", 8, b"moov")
            + struct.pack(">I4s", 26, b"mdat")
            + b"media test fixture"
        )
        digest = hashlib.sha256(hashlib.sha256(data).digest()).hexdigest()
        meta = {"id": "id:file", "rev": "v1", "content_hash": digest, "size": len(data)}
        db = Mock()
        db.link_metadata.return_value = meta
        db.metadata.return_value = meta
        v = FileVerifier(db)
        measurement = {
            "format": {
                "format_name": "mov,mp4",
                "duration": "5",
                "size": str(len(data)),
            },
            "streams": [
                {
                    "codec_type": "video",
                    "width": 1080,
                    "height": 1920,
                    "codec_name": "h264",
                    "pix_fmt": "yuv420p",
                    "avg_frame_rate": "30/1",
                }
            ],
        }

        def response(*args, **kwargs):
            f = io.BytesIO(data)
            f.url = "https://dl.dropboxusercontent.com/video"
            return f

        with (
            patch("waset_runtime.team.urllib.request.urlopen", side_effect=response),
            patch("waset_runtime.team.subprocess.run") as run,
        ):
            run.return_value = Mock(
                returncode=0, stdout=json.dumps(measurement).encode()
            )
            out = v.verify(self.snap)
            self.assertEqual(out["content_hash"], digest)
            run.return_value = Mock(returncode=1, stdout=b"")
            with self.assertRaisesRegex(ValueError, "cannot be read"):
                v.verify(self.snap)
            db.metadata.return_value = {**meta, "content_hash": "changed"}
            with self.assertRaisesRegex(ValueError, "do not match"):
                v.verify(self.snap)
            db.metadata.return_value = {**meta, "rev": "v2"}
            with self.assertRaisesRegex(ValueError, "changed during verification"):
                v.verify(self.snap)


if __name__ == "__main__":
    unittest.main()

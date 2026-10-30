"""Team board commands over the shared publication journal. No provider I/O here."""

from datetime import timedelta

from . import Ops, rules
from .core import PRE_COMMIT, fingerprint
from .db import dumps, loads, audit

SCHEMA = """CREATE TABLE IF NOT EXISTS ops_team (
 item_id TEXT PRIMARY KEY, board_id TEXT NOT NULL, remote_id TEXT NOT NULL,
 snapshot TEXT NOT NULL, approved TEXT, armed INTEGER NOT NULL DEFAULT 0,
 desired_status TEXT, reason TEXT, observed_at REAL NOT NULL,
 UNIQUE(board_id,remote_id));"""
CONTENT = ("format", "code", "caption", "collab", "ready_file", "topaz", "resolution")


class TeamOps(Ops):
    team_publisher = True

    def setup_team(self):
        with self.store.tx() as c:
            c.execute(SCHEMA)

    def team(self, c, iid):
        r = c.execute("SELECT * FROM ops_team WHERE item_id=?", (iid,)).fetchone()
        return dict(r) if r else None

    def notify(self, c, key, text, item_id=None, **extra):
        # Team feedback belongs on its own item; never sends unsolicited Slack messages.
        if item_id and self.team(c, item_id):
            return self.enqueue(c, "team_notice", key, {"text": text}, item_id)
        return super().notify(c, key, text, item_id, **extra)

    def project(self, c, item_id, force_keys=()):
        t = self.team(c, item_id)
        if not t:
            return super().project(c, item_id, force_keys)
        it = self.item(c, item_id)
        a = c.execute(
            "SELECT * FROM ops_attempts WHERE item_id=? ORDER BY created DESC LIMIT 1",
            (item_id,),
        ).fetchone()
        status, reason = t["desired_status"], t["reason"]
        if it["publication"] == "published":
            status = "Published"
            ev = loads(a["evidence"], {}) if a else {}
            reason = "Published: " + dumps(
                {
                    "media_id": a["media_id"] if a else None,
                    "permalink": a["permalink"] if a else None,
                    "published_at": ev.get("published_at"),
                    "recorded_at": ev.get("recorded_at"),
                    "container_id": a["container_id"] if a else None,
                    "file_version": it["asset_key"],
                }
            )
        elif it["publication"] in ("outcome_unknown", "failed"):
            status, reason = (
                "Blocked",
                "Publication "
                + it["publication"]
                + "; verify before any manual retry.",
            )
        if status or reason:
            payload = {
                "status": status,
                "reason": reason,
                "snapshot": loads(t["snapshot"], {}),
            }
            self.enqueue(
                c,
                "team_display",
                "team:"
                + item_id
                + ":"
                + fingerprint(
                    {
                        "status": status,
                        "reason": reason,
                        "approval": t["approved"],
                        "revision": it["content_rev"],
                        "content": {
                            k: loads(t["snapshot"], {}).get(k) for k in CONTENT
                        },
                    }
                ),
                payload,
                item_id,
            )

    def team_stop(self, c, iid, status, reason):
        a = self.active_attempt(c, iid)
        if a and a["stage"] not in PRE_COMMIT:
            return False  # publication may already have happened; reconcile it
        if a:
            c.execute(
                "UPDATE ops_attempts SET stage='abandoned',updated=? WHERE id=?",
                (self.now(), a["id"]),
            )
        c.execute("DELETE FROM ops_reservations WHERE item_id=?", (iid,))
        c.execute("DELETE FROM reservations WHERE item=?", (iid,))
        c.execute(
            "UPDATE ops_team SET approved=NULL,armed=0,desired_status=?,reason=? WHERE item_id=?",
            (status, reason, iid),
        )
        self.update_item(
            c,
            iid,
            "team",
            reason,
            readiness="blocked",
            block_reason=reason,
            verification_id=None,
        )
        return True

    def team_observe(self, board_id, snap):
        iid = str(board_id) + ":" + str(snap["id"])
        with self.store.tx() as c:
            t = self.team(c, iid)
            if not t:
                c.execute(
                    "INSERT INTO ops_team VALUES(?,?,?,?,NULL,?,NULL,NULL,?)",
                    (
                        iid,
                        str(board_id),
                        str(snap["id"]),
                        dumps(snap),
                        int(snap["status"] != "Ready to Publish"),
                        self.now(),
                    ),
                )
                c.execute(
                    "INSERT INTO ops_items(item_id,name,format,caption,caption_state,owner_state,created,updated) "
                    "VALUES(?,?,?,?,'missing','active',?,?)",
                    (
                        iid,
                        snap.get("name"),
                        snap.get("format"),
                        snap.get("caption"),
                        self.now(),
                        self.now(),
                    ),
                )
                if snap["status"] == "Published":
                    c.execute(
                        "UPDATE ops_items SET legacy_posted=1,publication='published' WHERE item_id=?",
                        (iid,),
                    )
                if snap["status"] == "Ready to Publish":
                    self.team_stop(
                        c,
                        iid,
                        "In Progress",
                        "Initial Ready item: review and select Ready to Publish again.",
                    )
                return iid
            old = loads(t["snapshot"], {})
            c.execute(
                "UPDATE ops_team SET snapshot=?,observed_at=? WHERE item_id=?",
                (dumps(snap), self.now(), iid),
            )
            it = self.item(c, iid)
            if it["publication"] in ("published", "outcome_unknown", "in_progress") or (
                self.active_attempt(c, iid)
                and self.active_attempt(c, iid)["stage"] not in PRE_COMMIT
            ):
                self.project(c, iid)
                return iid
            source = c.execute(
                "SELECT value FROM ops_meta WHERE key='publishing_source'"
            ).fetchone()
            if source and source[0] == "team_paused":
                if t["approved"] or snap["status"] == "Ready to Publish":
                    self.team_stop(
                        c,
                        iid,
                        "In Progress",
                        "Team publishing is paused; approval is not accepted.",
                    )
                return iid
            if t["approved"] and any(old.get(k) != snap.get(k) for k in CONTENT):
                self.team_stop(
                    c,
                    iid,
                    "In Progress",
                    "Approved content changed; review the final version and approve again.",
                )
                return iid
            if snap["status"] != "Ready to Publish":
                if t["approved"]:
                    self.team_stop(
                        c,
                        iid,
                        None,
                        "Publishing reservation cancelled by status change.",
                    )
                c.execute(
                    "UPDATE ops_team SET armed=1,desired_status=NULL WHERE item_id=?",
                    (iid,),
                )
            elif not snap.get("publish_at"):
                self.team_stop(
                    c, iid, "Blocked", "Publish At needs both date and time."
                )
            elif (
                not t["approved"] and t["armed"] and old["status"] != "Ready to Publish"
            ):
                # Bind the exact reviewed payload before external metadata/QA.
                c.execute(
                    "UPDATE ops_team SET approved=?,armed=0,desired_status=NULL,reason=NULL WHERE item_id=?",
                    (dumps({k: snap.get(k) for k in CONTENT}), iid),
                )
                c.execute(
                    "UPDATE ops_items SET name=?,code=?,format=?,caption=?,caption_state='approved',collab=?,"
                    "owner_state='active',publication='not_started',readiness='checking',hold=NULL,"
                    "content_rev=content_rev+1,asset_key=NULL,verification_id=NULL,content_hash=NULL,requested_at=?,requested_by='team' WHERE item_id=?",
                    (
                        snap.get("name"),
                        snap.get("code"),
                        snap.get("format"),
                        snap.get("caption"),
                        snap.get("collab"),
                        snap["publish_at"],
                        iid,
                    ),
                )
            elif t["approved"] and old.get("publish_at") != snap.get("publish_at"):
                a = self.active_attempt(c, iid)
                if a:
                    c.execute(
                        "UPDATE ops_attempts SET stage='abandoned',updated=? WHERE id=?",
                        (self.now(), a["id"]),
                    )
                c.execute("DELETE FROM ops_reservations WHERE item_id=?", (iid,))
                c.execute(
                    "UPDATE ops_items SET requested_at=? WHERE item_id=?",
                    (snap["publish_at"], iid),
                )
                self.try_schedule(c, iid, "team")
            return iid

    def team_verified(self, iid, info):
        with self.store.tx() as c:
            t = self.team(c, iid)
            s = loads(t["snapshot"], {})
            if not t["approved"] or loads(t["approved"], {}) != {
                k: s.get(k) for k in CONTENT
            }:
                return False
            it = self.item(c, iid)
            if it["asset_key"] and it["asset_key"] != info["assetKey"]:
                self.team_stop(
                    c,
                    iid,
                    "In Progress",
                    "Dropbox file version changed; review and approve again.",
                )
                return False
            mid = "T-" + fingerprint(info)
            c.execute(
                "INSERT OR REPLACE INTO media VALUES(?,?,?,?,?)",
                (mid, iid, "", info["assetKey"], dumps(info)),
            )
            c.execute(
                "UPDATE ops_items SET asset_key=?,file_id=?,file_rev=?,content_hash=?,verification_id=?,"
                "readiness='ready',block_reason=NULL WHERE item_id=?",
                (
                    info["assetKey"],
                    info["delivered"]["id"],
                    info["delivered"]["rev"],
                    info["content_hash"],
                    mid,
                    iid,
                ),
            )
            self.try_schedule(c, iid, "team")
            return True

    def verification_problem(self, c, it):
        if not self.team(c, it["item_id"]):
            return super().verification_problem(c, it)
        if not self.team(c, it["item_id"])["approved"]:
            return "Approval withdrawn"
        m = self.media_row(c, it.get("verification_id"))
        info = loads(m["metadata"], {}) if m else {}
        if (
            not info.get("team_verified")
            or info.get("assetKey") != it["asset_key"]
            or info.get("format") != it["format"]
        ):
            return "Final file is not verified for this approval."
        return None

    def board_read_at(self, c, iid):
        t = self.team(c, iid)
        return t["observed_at"] if t else super().board_read_at(c, iid)

    def deadline(self, c, res):
        if self.team(c, res["item_id"]):
            return rules.instant(res["slot"]) + timedelta(minutes=15)
        return super().deadline(c, res)

    def evaluate(self, c, iid, actor):
        if self.team(c, iid):
            return self.try_schedule(c, iid, actor)
        return super().evaluate(c, iid, actor)

    def try_schedule(self, c, iid, actor):
        t = self.team(c, iid)
        if not t:
            return {"waiting": "Legacy intake is disabled in Team mode"}
        it = self.item(c, iid)
        if (
            not t["approved"]
            or it["readiness"] != "ready"
            or it["publication"] != "not_started"
        ):
            return {"waiting": "Not approved and verified"}
        at = loads(t["snapshot"], {}).get("publish_at")
        if not at:
            return {"waiting": "Missing time"}
        if rules.instant(at) + timedelta(minutes=15) < self.now_dt():
            self.team_stop(
                c,
                iid,
                "Blocked",
                "Publishing time passed by more than 15 minutes; choose a new time and approve.",
            )
            return {"waiting": "Expired"}
        dup = self.duplicate_of(c, it)
        if dup:
            self.team_stop(c, iid, "Blocked", self.duplicate_reason(dup))
            return {"waiting": "Duplicate"}
        res = self.reservation(c, iid)
        if res:
            return {"scheduled": res["slot"]}
        if c.execute(
            "SELECT 1 FROM ops_reservations WHERE account=? AND format=? AND slot=?",
            (rules.ACCOUNT, it["format"], at),
        ).fetchone():
            self.team_stop(
                c,
                iid,
                "Blocked",
                "Another item of the same format uses this time; choose a new time.",
            )
            return {"waiting": "Slot occupied"}
        return {
            "scheduled": self._reserve(c, it, rules.instant(at), "team", True, actor)
        }

    def prepare_lead(self, c, iid):
        return timedelta(minutes=15) if self.team(c, iid) else timedelta(0)

    def team_candidates(self):
        with self.store.read() as c:
            return [
                dict(r)
                for r in c.execute(
                    "SELECT t.*,i.verification_id,i.asset_key,i.publication FROM ops_team t "
                    "JOIN ops_items i USING(item_id) WHERE t.approved IS NOT NULL AND i.publication='not_started'"
                )
            ]

    def team_cutover(self, enable=False, pause=False, authorized_by=None):
        """Explicit operator CLI command; refuses live workers before cancelling old reservations."""
        with self.store.tx() as c:
            active = [
                dict(r)
                for r in c.execute(
                    "SELECT id,item_id,stage,lease_until FROM ops_attempts WHERE "
                    "stage IN ('claimed','container_created','committed','outcome_unknown')"
                )
            ]
            old = [
                dict(r)
                for r in c.execute(
                    "SELECT * FROM ops_reservations WHERE item_id NOT IN (SELECT item_id FROM ops_team)"
                )
            ]
            if not enable and not pause:
                return {"active_attempts": active, "legacy_reservations": old}
            if not authorized_by:
                raise ValueError("Specific owner authorization reference required")
            if pause:
                c.execute(
                    "INSERT OR REPLACE INTO ops_meta VALUES('publishing_source','team_paused')"
                )
                for row in c.execute("SELECT item_id FROM ops_team").fetchall():
                    self.team_stop(
                        c,
                        row[0],
                        "In Progress",
                        "Team publishing paused; reapproval required after restart.",
                    )
                audit(c, None, "team_pause", authorized_by, {})
                return {"source": "team_paused", "reconciliation": "continues"}
            if any(
                a["stage"] in ("committed", "outcome_unknown")
                or (a["lease_until"] or 0) > self.now()
                for a in active
            ):
                raise ValueError("Resolve active attempts before cutover")
            for a in active:
                c.execute(
                    "UPDATE ops_attempts SET stage='abandoned',updated=? WHERE id=?",
                    (self.now(), a["id"]),
                )
            for r in old:
                self.release(
                    c, self.item(c, r["item_id"]), "Team cutover", keep_request=False
                )
                c.execute(
                    "UPDATE ops_items SET owner_state='paused' WHERE item_id=?",
                    (r["item_id"],),
                )
            for row in c.execute("SELECT item_id FROM ops_team").fetchall():
                self.team_stop(
                    c,
                    row[0],
                    "In Progress",
                    "Publishing enabled: review and select Ready to Publish again.",
                )
            c.execute(
                "INSERT OR REPLACE INTO ops_meta VALUES('publishing_source','team')"
            )
            audit(
                c,
                None,
                "team_cutover",
                authorized_by,
                {"cancelled": [r["item_id"] for r in old]},
            )
            return {"cancelled_legacy_reservations": len(old)}

    def editor_task(self, c, item_id, key, reason):
        if self.team(c, item_id):
            return self.team_stop(c, item_id, "Blocked", reason)
        return super().editor_task(c, item_id, key, reason)

    def request_check(self, c, item_id, actor, reason, **kwargs):
        if self.team(c, item_id):
            return self.team_stop(
                c, item_id, "Blocked", "File needs employee review: " + reason
            )
        return super().request_check(c, item_id, actor, reason, **kwargs)

    def waived(self, c, it):
        return set() if self.team(c, it["item_id"]) else super().waived(c, it)

    def content_identities(self, c, it):
        ids = super().content_identities(c, it)
        # Team has one account-wide file identity across formats. Older receipts used format-scoped keys.
        if self.team(c, it["item_id"]):
            other = {**it, "format": "Story" if it.get("format") == "Post" else "Post"}
            ids |= super().content_identities(c, other)
        return ids

    def duplicate_of(self, c, it):
        dup = super().duplicate_of(c, it)
        if dup or not self.team(c, it["item_id"]):
            return dup
        mine = self.content_identities(c, it)
        if mine:
            marks = ",".join("?" for _ in mine)
            for mark in c.execute(
                f"SELECT DISTINCT item FROM assets WHERE identity IN ({marks}) AND item!=?",
                (*mine, it["item_id"]),
            ):
                other = self.item(c, mark[0], required=False)
                if (
                    not other
                    or other["legacy_posted"]
                    or other["publication"]
                    in ("published", "in_progress", "outcome_unknown")
                ):
                    return {
                        "item_id": mark[0],
                        "name": None,
                        "state": "historical publication journal",
                    }
        for row in c.execute(
            "SELECT * FROM ops_items WHERE item_id!=?", (it["item_id"],)
        ):
            other = dict(row)
            live = (
                other["legacy_posted"]
                or other["publication"]
                in ("published", "in_progress", "outcome_unknown")
                or self.active_attempt(c, other["item_id"])
                or self.reservation(c, other["item_id"])
            )
            if not live:
                continue
            identities = self.content_identities(c, other)
            if mine & identities or (
                other["asset_key"] and other["asset_key"] == it["asset_key"]
            ):
                return {
                    "item_id": other["item_id"],
                    "name": other["name"],
                    "state": "reserved or previously published",
                }
            if other["code"] == it["code"] and (
                not identities or other["format"] == it["format"]
            ):
                return {
                    "item_id": other["item_id"],
                    "name": other["name"],
                    "state": "historical identity unverifiable; review required",
                }
        return None

    def _refused(self, c, a, worker, cls, error, http_status):
        if not self.team(c, a["item_id"]):
            return super()._refused(c, a, worker, cls, error, http_status)
        self.unjournal(c, a)
        c.execute(
            "UPDATE ops_attempts SET stage='failed',evidence=?,updated=? WHERE id=?",
            (
                dumps(
                    {
                        "error": str(error)[:500],
                        "class": cls,
                        "http_status": http_status,
                    }
                ),
                self.now(),
                a["id"],
            ),
        )
        c.execute(
            "DELETE FROM publications WHERE item=? AND owner=?",
            (a["item_id"], "ops:" + a["id"]),
        )
        c.execute(
            "UPDATE ops_items SET publication='not_started' WHERE item_id=?",
            (a["item_id"],),
        )
        self.team_stop(
            c,
            a["item_id"],
            "Blocked",
            "Instagram refused publication; review and approve again: "
            + str(error)[:300],
        )
        if cls in ("auth", "restricted"):
            self._open_breaker(c, cls, str(error)[:300])
        return {"stage": "failed"}

    def precommit_failure(
        self,
        attempt_id,
        worker,
        fence,
        stage,
        *,
        error=None,
        http_status=None,
        status_code=None,
    ):
        with self.store.tx() as c:
            a = self._attempt(c, attempt_id, worker, fence, PRE_COMMIT, lease=False)
            if self.team(c, a["item_id"]):
                self.team_stop(
                    c,
                    a["item_id"],
                    "Blocked",
                    "Instagram container failed: "
                    + str(status_code or error or stage)[:300],
                )
                return {"abandoned": True, "class": "team_review"}
        return super().precommit_failure(
            attempt_id,
            worker,
            fence,
            stage,
            error=error,
            http_status=http_status,
            status_code=status_code,
        )

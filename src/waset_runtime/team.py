"""Publish-only Team adapter. Reads the reviewed file; never edits or uploads media."""

from pathlib import Path
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
import hashlib
import html
import json
import subprocess
import tempfile
import secrets
import time
import urllib.request

from waset_ops import rules
from waset_ops.db import loads
from .http import HttpError
from .monday import MondayError

COL = {
    "status": "color_mm8kzza9",
    "format": "color_mm8k4fvq",
    "publish_at": "date_mm8kqvf4",
    "caption": "long_text_mm8k96g0",
    "code": "text_mm8kqmhy",
    "ready_file": "link_mm8knp89",
    "topaz": "color_mm8mchph",
    "resolution": "color_mm8msfq0",
    "notes": "long_text_mm8ks8mk",
    "collab": "text_mm8mg660",
    "duration": "color_mm8m6aer",
    "size": "color_mm8m5rx9",
}


def snapshot(raw):
    values = {x["id"]: x for x in raw.get("column_values", [])}
    out = {"id": str(raw["id"]), "name": raw.get("name", "")}
    for key, cid in COL.items():
        cell = values.get(cid, {})
        v = (
            loads(cell.get("value"), {})
            if isinstance(cell.get("value"), str)
            else cell.get("value") or {}
        )
        out[key] = (
            v.get("url")
            if key == "ready_file"
            else v.get("text", cell.get("text", ""))
            if key in ("caption", "notes")
            else cell.get("text", "")
        ) or ""
        if key == "publish_at":
            # Monday's date-column time is UTC (same contract as the existing adapter).
            d = (
                rules.parse_monday_publish_at(v)
                if v.get("date") and v.get("time")
                else None
            )
            out[key] = rules.iso(d) if d else None
    return out


def direct_url(url):
    u = urlsplit(url)
    if (
        u.username
        or u.password
        or u.port
        or u.scheme != "https"
        or u.hostname not in ("www.dropbox.com", "dropbox.com")
        or not u.path.startswith(("/s/", "/scl/fi/"))
    ):
        raise ValueError("Ready File must be a Dropbox shared FILE link.")
    q = dict(parse_qsl(u.query))
    q.pop("dl", None)
    q["raw"] = "1"
    return urlunsplit((u.scheme, u.netloc, u.path, urlencode(q), ""))


def validate_measurements(info, fmt):
    v = next(
        (
            s
            for s in info["streams"]
            if s.get("codec_type") == "video"
            and not s.get("disposition", {}).get("attached_pic")
        ),
        None,
    )
    if not v:
        raise ValueError("No video stream")
    w, h = int(v["width"]), int(v["height"])
    rotation = next(
        (x.get("rotation", 0) for x in v.get("side_data_list", []) if "rotation" in x),
        0,
    )
    if float(rotation) % 90:
        raise ValueError("Unsupported display rotation")
    if int(rotation) % 180:
        w, h = h, w
    duration = float(info["format"]["duration"])
    size = int(info["format"]["size"])
    if (w, h) not in ((1080, 1920), (1920, 1080)):
        raise ValueError("Final display dimensions must be 1080x1920 or 1920x1080")
    if not 3 <= duration < (60 if fmt == "Story" else 900):
        raise ValueError("Duration outside publishing limits")
    if not set(info["format"].get("format_name", "").split(",")) & {"mov", "mp4"}:
        raise ValueError("Use MOV or MP4 container")
    if float(v.get("bit_rate") or size * 8 / duration) > 25_000_000:
        raise ValueError("Video bitrate exceeds 25 Mbps; re-export locally")
    if not 0 < size < (100_000_000 if fmt == "Story" else 300_000_000):
        raise ValueError("Story must be below 100 MB; Post below 300 MB")
    if v.get("codec_name") != "h264" or v.get("pix_fmt") not in ("yuv420p", "yuvj420p"):
        raise ValueError("Use SDR H.264 4:2:0 video")
    if v.get("color_transfer") in ("smpte2084", "arib-std-b67"):
        raise ValueError("HDR export requires employee review")
    num, den = map(int, v.get("avg_frame_rate", "0/1").split("/"))
    fps = num / den if den else 0
    if not 23 <= fps <= 60:
        raise ValueError("Frame rate must be 23–60 FPS")
    if v.get("field_order") not in (None, "unknown", "progressive"):
        raise ValueError("Use progressive video")
    audio = [x for x in info["streams"] if x.get("codec_type") == "audio"]
    if any(
        x.get("codec_name") != "aac"
        or int(x.get("channels", 0)) > 2
        or int(x.get("sample_rate", 0)) != 48000
        for x in audio
    ):
        raise ValueError("Use AAC mono/stereo audio")
    return {"width": w, "height": h, "duration": duration, "bytes": size, "fps": fps}


def validate_container(path):
    """Inspect MP4/MOV boxes without rewriting the employee's file."""
    import struct

    size = path.stat().st_size
    boxes = []
    with path.open("rb") as f:

        def walk(start, end, nested=False):
            pos = start
            while pos < end:
                f.seek(pos)
                header = f.read(8)
                if len(header) != 8:
                    raise ValueError("Truncated media box")
                length, kind = struct.unpack(">I4s", header)
                head = 8
                if length == 1:
                    raw = f.read(8)
                    if len(raw) != 8:
                        raise ValueError("Truncated large media box")
                    length = struct.unpack(">Q", raw)[0]
                    head = 16
                elif length == 0:
                    length = end - pos
                if length < head or pos + length > end:
                    raise ValueError("Invalid media box length")
                if not nested:
                    boxes.append(kind)
                if kind == b"elst":
                    raise ValueError("Export without edit lists for Instagram")
                if kind in (b"moov", b"trak", b"edts"):
                    walk(pos + head, pos + length, True)
                pos += length

        walk(0, size)
    if (
        b"moov" not in boxes
        or b"mdat" not in boxes
        or boxes.index(b"moov") > boxes.index(b"mdat")
    ):
        raise ValueError("Export MOV/MP4 with faststart (moov before mdat)")


class FileVerifier:
    def __init__(self, dropbox):
        self.dropbox = dropbox

    def identity(self, url):
        direct_url(url)  # validates host before a connected API request
        m = self.dropbox.link_metadata(url)
        if not m.get("id") or not m.get("rev") or not m.get("size"):
            raise ValueError("Dropbox file identity/version unavailable")
        full = self.dropbox.metadata(m["id"])
        if full.get("rev") != m["rev"]:
            raise ValueError("Dropbox file changed during verification")
        return full

    def verify(self, snap):
        if snap["format"] not in ("Post", "Story") or not snap["code"].strip():
            raise ValueError("Format and Code are required")
        if snap["topaz"] != "Yess" or snap["resolution"] != "Yess":
            raise ValueError("Topaz and 1080 must be approved")
        if snap["format"] == "Post":
            bad = rules.validate_caption(snap["caption"])
            if bad:
                raise ValueError("Caption: " + bad)
        import re

        if snap["collab"] and (
            snap["format"] == "Story"
            or not re.fullmatch(r"@?[A-Za-z0-9._]{2,30}", snap["collab"])
        ):
            raise ValueError(
                "Collab requires one valid Instagram username on a Post; Story Collab is unsupported"
            )
        m = self.identity(snap["ready_file"])
        url = direct_url(snap["ready_file"])
        limit = 100_000_000 if snap["format"] == "Story" else 300_000_000
        if int(m["size"]) >= limit:
            raise ValueError(
                "Final file exceeds format size limit; re-export and review"
            )
        with tempfile.TemporaryDirectory(prefix="team-qa-") as temp:
            deadline = time.monotonic() + 600
            p = Path(temp) / "video.mp4"
            sha = hashlib.sha256()
            blocks = hashlib.sha256()
            total = 0
            pending = b""
            # Anonymous download checks that Meta can retrieve the bytes without our Dropbox token.
            with urllib.request.urlopen(url, timeout=60) as response, p.open("wb") as f:
                if urlsplit(response.url).hostname not in (
                    "www.dropbox.com",
                    "dropbox.com",
                    "dl.dropboxusercontent.com",
                ):
                    raise ValueError("Unexpected Dropbox download destination")
                while True:
                    if time.monotonic() > deadline:
                        raise TimeoutError("Download QA deadline")
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total >= limit:
                        raise ValueError("Downloaded file exceeds limit")
                    f.write(chunk)
                    sha.update(chunk)
                    pending += chunk
                    while len(pending) >= 4 * 1024 * 1024:
                        blocks.update(
                            hashlib.sha256(pending[: 4 * 1024 * 1024]).digest()
                        )
                        pending = pending[4 * 1024 * 1024 :]
            if pending:
                blocks.update(hashlib.sha256(pending).digest())
            if total != m["size"] or blocks.hexdigest() != m.get("content_hash"):
                raise ValueError("Downloaded bytes do not match Dropbox version")
            validate_container(p)
            proc = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_streams",
                    "-show_format",
                    "-of",
                    "json",
                    str(p),
                ],
                capture_output=True,
                timeout=120,
            )
            if proc.returncode:
                raise ValueError("Final video cannot be read")
            measurements = validate_measurements(
                json.loads(proc.stdout), snap["format"]
            )
            decoded = subprocess.run(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-xerror",
                    "-i",
                    str(p),
                    "-map",
                    "0:v:0",
                    "-map",
                    "0:a?",
                    "-f",
                    "null",
                    "-",
                ],
                capture_output=True,
                timeout=600,
            )
            if decoded.returncode:
                raise ValueError("Final file failed full decoding")
            if self.identity(snap["ready_file"])["rev"] != m["rev"]:
                raise ValueError("Dropbox version changed during QA")
            return {
                **measurements,
                "team_verified": True,
                "assetKey": m["id"] + "@" + m["rev"],
                "content_hash": m["content_hash"],
                "sha256": sha.hexdigest(),
                "format": snap["format"],
                "url": url,
                "delivered": {"id": m["id"], "rev": m["rev"]},
            }


class TeamRunner:
    publishers = 1

    def __init__(
        self, ops, monday, dropbox, ig, board_id, verifier=None, dry_run=False
    ):
        self.ops, self.monday, self.dropbox, self.ig, self.board_id = (
            ops,
            monday,
            dropbox,
            ig,
            str(board_id),
        )
        self.verifier = verifier or FileVerifier(dropbox)
        self.dry_run = dry_run
        self.worker_id = secrets.token_hex(8)

    def fresh(self, remote):
        rows = self.monday.items([remote], list(COL.values()))
        if (
            len(rows) != 1
            or str(rows[0].get("board", {}).get("id")) != self.board_id
            or rows[0].get("state", "active") != "active"
        ):
            raise MondayError("Team item missing, archived or on a different board")
        return snapshot(rows[0])

    def prepare_cycle(self):
        return {"disabled": "Employee owns all preparation"}

    def supervise_cycle(self):
        return {"disabled": "No automatic schedule repair in Team mode"}

    def sync_cycle(self):
        lease = self.ops.run_start("wf1", "team-sync-" + self.worker_id)
        if not lease.get("acquired"):
            return {"busy": True}
        self.sync_lease = lease
        try:
            return self._sync_cycle()
        finally:
            self.ops.run_finish(
                "wf1", "team-sync-" + self.worker_id, fence=lease["fence"]
            )

    def _sync_cycle(self):
        raw = self.monday.board_items(self.board_id, list(COL.values()))
        seen = set()
        for item in raw:
            seen.add(self.ops.team_observe(self.board_id, snapshot(item)))
        qa_started = False
        for row in sorted(
            self.ops.team_candidates(), key=lambda x: not bool(x["verification_id"])
        ):
            self.ops.run_progress(
                "wf1",
                "team-sync-" + self.worker_id,
                self.sync_lease["fence"],
                "team verify",
            )
            iid = row["item_id"]
            if iid not in seen:
                with self.ops.store.tx() as c:
                    self.ops.team_stop(c, iid, None, "Item removed from Team board")
                continue
            try:
                s = loads(row["snapshot"], {})
                if row["verification_id"]:
                    m = self.verifier.identity(s["ready_file"])
                    if m["id"] + "@" + m["rev"] != row["asset_key"]:
                        with self.ops.store.tx() as c:
                            self.ops.team_stop(
                                c,
                                iid,
                                "In Progress",
                                "Dropbox version changed; review and approve again.",
                            )
                    else:
                        with self.ops.store.tx() as c:
                            self.ops.try_schedule(c, iid, "team")
                else:
                    if qa_started:
                        continue
                    qa_started = True
                    info = self.verifier.verify(s)
                    self.ops.run_progress(
                        "wf1",
                        "team-sync-" + self.worker_id,
                        self.sync_lease["fence"],
                        "QA complete",
                    )
                    # Re-read after slow I/O; an approval changed during QA must not be used.
                    fresh = self.fresh(row["remote_id"])
                    self.ops.team_observe(self.board_id, fresh)
                    if all(
                        fresh.get(k) == s.get(k)
                        for k in s
                        if k not in ("notes", "name")
                    ):
                        self.ops.team_verified(iid, info)
            except ValueError as e:
                with self.ops.store.tx() as c:
                    self.ops.team_stop(c, iid, "Blocked", str(e))
            except Exception as e:
                self.ops.heartbeat(
                    "team_read_failure", {"item": iid, "type": type(e).__name__}
                )
        if not self.dry_run:
            self.flush()
        return {"observed": len(seen)}

    def flush(self):
        worker = "team-display-" + self.worker_id
        for job in self.ops.outbox_take(
            ["team_display", "team_notice"], worker, limit=50
        ):
            try:
                payload = (
                    job["payload"]
                    if isinstance(job["payload"], dict)
                    else loads(job["payload"], {})
                )
                with self.ops.store.read() as c:
                    t = self.ops.team(c, job["item_id"])
                fresh = self.fresh(t["remote_id"])
                self.ops.team_observe(self.board_id, fresh)
                if job["kind"] == "team_display":
                    with self.ops.store.read() as c:
                        now = self.ops.team(c, job["item_id"])
                        it = self.ops.item(c, job["item_id"])
                    # Old display work cannot overwrite an employee's newer decision.
                    published = (
                        it["publication"] == "published"
                        and payload.get("status") == "Published"
                    )
                    if published:
                        with self.ops.store.read() as c:
                            newer = c.execute(
                                "SELECT 1 FROM ops_outbox WHERE kind='team_display' AND item_id=? AND id>?",
                                (job["item_id"], job["id"]),
                            ).fetchone()
                        if newer:
                            self.ops.outbox_ack(job["id"], worker, True)
                            continue
                    if not published and (
                        any(
                            payload.get("snapshot", {}).get(k) != fresh.get(k)
                            for k in (
                                "format",
                                "code",
                                "caption",
                                "collab",
                                "ready_file",
                                "topaz",
                                "resolution",
                                "publish_at",
                            )
                        )
                        or (
                            now["desired_status"] != payload.get("status")
                            and fresh["status"] != payload.get("status")
                        )
                        or fresh["status"]
                        not in (
                            payload.get("snapshot", {}).get("status"),
                            payload.get("status"),
                        )
                    ):
                        self.ops.outbox_ack(job["id"], worker, True)
                        continue
                    values = {}
                    if payload.get("status"):
                        values[COL["status"]] = {"label": payload["status"]}
                    reason = payload.get("reason") or ""
                    if reason:
                        import re

                        original = re.sub(
                            r"\n?\[Bondok\]\n.*?\n\[End Bondok\]",
                            "",
                            fresh["notes"],
                            flags=re.S,
                        )
                        values[COL["notes"]] = {
                            "text": original
                            + "\n[Bondok]\n"
                            + reason
                            + "\n[End Bondok]"
                        }
                    if values:
                        self.monday.change_columns(
                            t["remote_id"], values, board_id=self.board_id
                        )
                    text = reason
                else:
                    text = payload.get("text", "")
                marker = "[Bondok receipt " + str(job["id"]) + "]"
                updates = self.monday.updates(t["remote_id"]).get("updates", [])
                if text and not any(marker in u.get("text_body", "") for u in updates):
                    self.monday.create_update(
                        t["remote_id"], html.escape(marker + "\n" + text)
                    )
                self.ops.outbox_ack(job["id"], worker, True)
            except Exception as e:
                self.ops.outbox_ack(job["id"], worker, False, type(e).__name__)

    def publish_cycle(self, pool=None, sync=False):
        if self.dry_run:
            return {"dry_run": True, "publication": "disabled"}
        # Existing engine handles committed/unknown receipts; never use its legacy publish candidates.
        due = self.ops.due("team", 100)
        for w in due.get("work", []):
            if w["kind"] == "reconcile":
                try:
                    self.ops.reconcile(
                        w["attempt_id"],
                        self.ig.container_status(w["container_id"]).get("status_code"),
                    )
                except HttpError:
                    self.ops.reconcile(w["attempt_id"], None, "Instagram unavailable")
        if due.get("probe"):
            ok, error = self.ig.probe()
            self.ops.provider_probe(ok, error)
        with self.ops.store.read() as c:
            source = c.execute(
                "SELECT value FROM ops_meta WHERE key='publishing_source'"
            ).fetchone()
            if not source or source[0] != "team":
                return {"disabled": "Cutover not committed"}
            if self.ops.breaker(c) or self.ops.restore_gap(c):
                return {"blocked": "Account or restore guard"}
        for row in self.ops.team_candidates():
            try:
                self.publish_one(row)
            except Exception as e:
                self.ops.heartbeat(
                    "team_publish_error",
                    {"item": row["item_id"], "type": type(e).__name__},
                )
        return {"checked": True}

    def publish_one(self, row):
        iid = row["item_id"]
        worker = "team-" + self.worker_id + "-" + iid
        fresh = self.fresh(row["remote_id"])
        self.ops.team_observe(self.board_id, fresh)
        with self.ops.store.read() as c:
            t = self.ops.team(c, iid)
            res = self.ops.reservation(c, iid)
            if not t["approved"] or not res:
                return
            if (
                not -900
                <= self.ops.now() - rules.instant(res["slot"]).timestamp()
                <= 900
            ):
                return
        cl = self.ops.claim(iid, worker)
        if not cl.get("claimed"):
            return
        aid, fence = cl["attempt_id"], cl["fence"]
        cid = cl.get("container_id")
        if not cid:
            cid = self.ig.create_container(cl["payload"])
            self.ops.container(aid, worker, fence, cid)
        state = self.ig.container_status(cid).get("status_code")
        if state in ("ERROR", "EXPIRED"):
            self.ops.precommit_failure(
                aid, worker, fence, "container", status_code=state
            )
            return
        if (
            state != "FINISHED"
            or self.ops.now() < rules.instant(res["slot"]).timestamp()
        ):
            return
        m = self.verifier.identity(fresh["ready_file"])
        fresh = self.fresh(row["remote_id"])
        self.ops.team_observe(self.board_id, fresh)
        with self.ops.store.read() as c:
            t = self.ops.team(c, iid)
            it = self.ops.item(c, iid)
            if not t["approved"] or fresh["status"] != "Ready to Publish":
                return
            if m["id"] + "@" + m["rev"] != it["asset_key"]:
                changed = True
            else:
                changed = False
        if changed:
            with self.ops.store.tx() as c:
                self.ops.team_stop(
                    c, iid, "In Progress", "Dropbox version changed before publishing."
                )
            return
        committed = self.ops.commit(
            aid,
            worker,
            fence,
            source_asset=m["id"] + "@" + m["rev"],
            delivered_asset=m["id"] + "@" + m["rev"],
            container_status=state,
            record="store",
        )
        if not committed.get("committed"):
            return
        try:
            status, body = self.ig.publish(cid)
            mid = body.get("id") if status == 200 else None
            result = self.ops.result(
                aid,
                worker,
                media_id=mid,
                http_status=status,
                error=json.dumps(body.get("error")),
                definitive=400 <= status < 500 and bool(body.get("error")),
            )
        except Exception:
            self.ops.result(
                aid, worker, error="Publish transport failed; result unknown"
            )
            return
        if result.get("stage") == "published" and result.get("media_id"):
            self.ops.evidence(aid, self.ig.permalink(result["media_id"]))

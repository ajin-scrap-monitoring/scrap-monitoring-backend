"""Synthetic byte responses for the Docker gateway probe; no real video or credentials."""

import json
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
from uuid import NAMESPACE_URL, uuid5

CONTENT = b"synthetic-mp4-content-for-range-verification"
DAILY_ID = "550e8400-e29b-41d4-a716-446655440000"
SEGMENT_ID = "synthetic-segment-01"


def daily_recording(export_state="not_requested", deleted=False):
    start = "2026-10-13T15:00:00.000Z"
    middle = "2026-10-13T15:00:20.000Z"
    end = "2026-10-14T15:00:00.000Z"
    prefix = f"/api/v2/daily-recordings/{DAILY_ID}"
    return {
        "recording_id": DAILY_ID,
        "camera_id": "camera-main",
        "recording_date": "2026-10-14",
        "timezone": "Asia/Seoul",
        "window_start_at": start,
        "window_end_at": end,
        "status": "deleted" if deleted else "closed",
        "coverage": "partial",
        "has_gaps": True,
        "coverage_as_of_at": end,
        "playable_until_at": middle,
        "recorded_seconds": 20,
        "gaps": [{"start_at": middle, "end_at": end, "reason_code": "CAMERA_DISCONNECTED"}],
        "available_intervals": [
            {
                "start_at": start,
                "end_at": middle,
                "playback_start_seconds": 0,
                "playback_end_seconds": 20,
            }
        ],
        "playback_state": "unavailable" if deleted else "ready",
        "playback_protocol": "hls",
        "playback_path": None if deleted else prefix + "/hls/index.m3u8",
        "export_allowed": not deleted,
        "export_state": export_state,
        "download_path": prefix + "/content" if export_state == "ready" and not deleted else None,
        "size_bytes": len(CONTENT) if export_state == "ready" else None,
        "expires_at": "2026-11-14T15:00:00.000Z",
        "export_expires_at": None,
        "error": None,
    }


class Handler(BaseHTTPRequestHandler):
    daily_exported = False
    daily_deleted = False
    range_clips = {}

    def daily_json(self, code, body):
        body = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path == "/api/v1/clips":
            if self.headers.get(
                "Authorization"
            ) != "Bearer synthetic-media-token" or self.headers.get("Cookie"):
                self.send_error(401)
                return
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            assert "start_at" in body and "end_at" in body and "event_at" not in body
            key = str(uuid5(NAMESPACE_URL, body["event_id"]))
            ready = key in self.range_clips
            self.range_clips[key] = {
                "clip_id": key,
                "event_id": body["event_id"],
                "camera_id": body["camera_id"],
                "request_mode": "time_range",
                "attempt_count": 1,
                "status": "ready",
                "accepted_at": datetime.now(UTC).isoformat(),
                "requested_start_at": body["start_at"],
                "requested_end_at": body["end_at"],
                "coverage": "complete",
                "recording_policy": {
                    "pre_roll_seconds": 30,
                    "post_roll_seconds": 30,
                    "retention_days": 30,
                },
                "actual_start_at": body["start_at"],
                "actual_end_at": body["end_at"],
                "gaps": [],
                "available_intervals": [
                    {
                        "start_at": body["start_at"],
                        "end_at": body["end_at"],
                    }
                ],
                "completed_at": datetime.now(UTC).isoformat(),
                "expires_at": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
                "deleted_at": None,
                "content_path": f"/api/v1/clips/{key}/content",
                "error": None,
                "extraction_mode": "stream_copy",
                "size_bytes": len(CONTENT),
            }
            self.daily_json(
                200 if ready else 202, {**self.range_clips[key], "status": "processing"}
            )
            return
        if self.headers.get("Authorization") or self.headers.get("Cookie"):
            self.send_error(400)
            return
        if self.path != f"/api/v2/daily-recordings/{DAILY_ID}/exports":
            self.send_error(404)
            return
        ready = type(self).daily_exported
        type(self).daily_exported = True
        self.daily_json(200 if ready else 202, daily_recording("ready" if ready else "processing"))

    def do_DELETE(self):
        if self.headers.get("Authorization") or self.headers.get("Cookie"):
            self.send_error(400)
            return
        if self.path != f"/api/v2/daily-recordings/{DAILY_ID}":
            self.send_error(404)
            return
        type(self).daily_deleted = True
        self.send_response(204)
        self.end_headers()

    def do_HEAD(self):
        self.do_GET()

    def daily_get(self, path, query):
        if self.headers.get("Authorization") or self.headers.get("Cookie"):
            self.send_error(400, "v2 must not receive credentials")
            return
        prefix = f"/api/v2/daily-recordings/{DAILY_ID}"
        item = daily_recording(
            "ready" if type(self).daily_exported else "not_requested", type(self).daily_deleted
        )
        if path.endswith("daily-recording-policy"):
            self.daily_json(200, {"enabled": True, "retention_days": 30})
            return
        if path.endswith("/daily-recordings"):
            self.daily_json(200, {"items": [item], "next_cursor": None})
            return
        if path == prefix:
            self.daily_json(200, item)
            return
        if type(self).daily_deleted:
            self.send_error(410)
            return
        if path == prefix + "/hls/index.m3u8":
            body = (
                "#EXTM3U\n#EXT-X-TARGETDURATION:10\n#EXT-X-DISCONTINUITY\n"
                "#EXTINF:10.0,\n" + prefix + "/media/" + SEGMENT_ID + ".ts\n"
                "#EXT-X-ENDLIST\n"
            ).encode()
            content_type = "application/vnd.apple.mpegurl"
        elif path in (prefix + "/media/" + SEGMENT_ID + ".ts", prefix + "/content"):
            if path.endswith("/content") and not type(self).daily_exported:
                self.send_error(409)
                return
            body = CONTENT
            content_type = "video/mp2t" if path.endswith(".ts") else "video/mp4"
        else:
            self.send_error(404)
            return
        partial = self.headers.get("Range") == "bytes=0-3"
        self.send_response(206 if partial else 200)
        if partial:
            self.send_header("Content-Range", f"bytes 0-3/{len(body)}")
            body = body[:4]
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Accept-Ranges", "bytes")
        if query.get("download") == ["true"]:
            self.send_header("Content-Disposition", 'attachment; filename="daily_2026-10-14.mp4"')
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def log_message(self, format, *args):
        pass

    def do_GET(self):
        parsed = urlsplit(self.path)
        if parsed.path.startswith("/api/v2/"):
            self.daily_get(parsed.path, parse_qs(parsed.query))
            return
        if self.headers.get("Authorization") != "Bearer synthetic-media-token":
            self.send_error(401)
            return
        if self.headers.get("Cookie"):
            self.send_error(400, "Browser cookie must not reach the media service")
            return
        path = urlsplit(self.path)
        query = parse_qs(path.query)
        if path.path.endswith("recording-policy"):
            body = json.dumps(
                {
                    "pre_roll_seconds": 30,
                    "post_roll_seconds": 30,
                    "retention_days": 30,
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
        elif path.path.rsplit("/", 1)[-1] in self.range_clips:
            self.daily_json(200, self.range_clips[path.path.rsplit("/", 1)[-1]])
            return
        elif path.path.endswith("/live"):
            if query.get("unavailable"):
                self.send_error(503)
                return
            body = b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: 4\r\n\r\nJPEG\r\n"
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        elif path.path.endswith("/content"):
            if query.get("state") in (["deleted"], ["expired"]):
                self.send_error(410)
                return
            if query.get("state") == ["processing"]:
                self.send_error(409)
                return
            body = CONTENT
            range_header = self.headers.get("Range", "")
            if range_header == "bytes=0-3":
                body = CONTENT[:4]
                self.send_response(206)
                self.send_header("Content-Range", f"bytes 0-3/{len(CONTENT)}")
            elif range_header == "bytes=999-":
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{len(CONTENT)}")
                self.end_headers()
                return
            else:
                self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Accept-Ranges", "bytes")
            disposition = "attachment" if query.get("download") == ["true"] else "inline"
            self.send_header("Content-Disposition", f'{disposition}; filename="synthetic.mp4"')
        else:
            self.send_error(404)
            return
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8889), Handler).serve_forever()

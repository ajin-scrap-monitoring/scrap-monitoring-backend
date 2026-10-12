"""Synthetic byte responses for the Docker gateway probe; no real video or credentials."""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

CONTENT = b"synthetic-mp4-content-for-range-verification"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
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

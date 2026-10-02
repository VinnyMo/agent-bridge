"""HTTP over a filesystem-protected Unix socket; never binds an IP address."""
import json
import os
import socketserver
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from message_store import MAX_BODY, MessageError, append_message


class LocalHandler(BaseHTTPRequestHandler):
    def setup(self):
        self.request.settimeout(15)
        super().setup()

    def log_message(self, *args):
        pass  # No bodies, credentials, or caller-supplied paths in logs.

    def reply(self, status, data):
        raw = json.dumps(data).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        if self.path != '/append':
            return self.reply(404, {'error': 'not_found'})
        lengths = self.headers.get_all('Content-Length', [])
        if self.headers.get('Transfer-Encoding') or len(lengths) != 1 or not lengths[0].isdecimal():
            return self.reply(400, {'error': 'invalid_body_framing'})
        if len(lengths[0]) > 6 or not 0 < int(lengths[0]) <= MAX_BODY:
            return self.reply(413, {'error': 'body_too_large'})
        if self.headers.get_content_type() != 'application/json':
            return self.reply(415, {'error': 'application_json_required'})
        try:
            raw = self.rfile.read(int(lengths[0]))
            payload = json.loads(raw)
            result = append_message(self.server.messages_path, payload)
        except (ValueError, UnicodeDecodeError) as exc:
            if isinstance(exc, MessageError):
                return self.reply(exc.status, {'error': exc.code})
            return self.reply(400, {'error': 'invalid_json'})
        except OSError:
            return self.reply(500, {'error': 'message_not_saved'})
        self.reply(201, result)

    def do_GET(self):
        self.reply(404, {'error': 'not_found'})


class LocalServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    block_on_close = True


def serve(socket_path, messages_path):
    # RuntimeDirectory belongs to the service. The systemd unit removes it on stop.
    socket_path = Path(socket_path)
    if socket_path.exists():
        if not socket_path.is_socket():
            raise RuntimeError('Refusing to replace a non-socket')
        socket_path.unlink()
    with LocalServer(str(socket_path), LocalHandler) as server:
        os.chmod(socket_path, 0o660)
        server.messages_path = Path(messages_path)
        server.serve_forever()


if __name__ == '__main__':
    serve(os.environ.get('AGENT_APPEND_SOCKET', '/run/agent-context-write/append.sock'),
          os.environ.get('AGENT_MESSAGES_FILE', '/var/lib/agent-context/messages.jsonl'))

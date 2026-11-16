"""Kick chat via Kick's OFFICIAL webhooks (chat.message.sent). Kick pushes events to a URL you control; this module is that
receiver: it verifies Kick's RSA signature (stdlib only), and appends each message to a jsonl file that `clipper monitor --chat-file`
tails. Setup (needs your own Kick developer app, OAuth token and a public HTTPS URL pointing at this port, e.g. a tunnel):
subscribe to `chat.message.sent` for your channel per https://github.com/KickEngineering/KickDevDocs/blob/main/events/subscribe-to-events.md
Signature scheme (events/webhook-security.md): RSA PKCS#1 v1.5 + SHA-256 over "<Kick-Event-Message-Id>.<Kick-Event-Message-Timestamp>.<raw body>",
base64 in `Kick-Event-Signature`, public key fetched from https://api.kick.com/public/v1/public-key (never hardcoded: it can rotate)."""
import base64, datetime, hashlib, json, re, urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

KEY_URL = "https://api.kick.com/public/v1/public-key"
SHA256_PREFIX = bytes.fromhex("3031300d060960864801650304020105000420")


def _der(b, i=0):
    """-> (tag, content_start, content_end) of the DER element at b[i]."""
    tag, ln = b[i], b[i + 1]
    j = i + 2
    if ln & 0x80:
        k = ln & 0x7F
        ln, j = int.from_bytes(b[j:j + k], "big"), j + k
    return tag, j, j + ln


def parse_public_key(pem):
    """PEM SubjectPublicKeyInfo (RSA) -> (n, e)."""
    der = base64.b64decode(re.sub(r"-----[^-]+-----|\s", "", pem if isinstance(pem, str) else pem.decode()))
    _, s, _ = _der(der)                       # SEQUENCE
    _, s2, e2 = _der(der, s)                  # algorithm identifier
    t, bs, be = _der(der, e2)                 # BIT STRING holding the RSAPublicKey
    inner = der[bs + 1:be]                    # skip the unused-bits byte
    _, s3, _ = _der(inner)
    t1, a, b = _der(inner, s3)
    t2, c, d = _der(inner, b)
    return int.from_bytes(inner[a:b], "big"), int.from_bytes(inner[c:d], "big")


def verify(pubkey, message, signature_b64):
    """RSASSA-PKCS1-v1_5 / SHA-256 verification."""
    n, e = pubkey
    try:
        sig = base64.b64decode(signature_b64, validate=True)
    except ValueError:
        return False
    k = (n.bit_length() + 7) // 8
    if len(sig) != k:
        return False
    em = pow(int.from_bytes(sig, "big"), e, n).to_bytes(k, "big")
    t = SHA256_PREFIX + hashlib.sha256(message).digest()
    return em == b"\x00\x01" + b"\xff" * (k - len(t) - 3) + b"\x00" + t


def fetch_key(url=KEY_URL):
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "clipper"}), timeout=20) as r:
        body = r.read()
    try:  # the endpoint wraps the PEM: {"data": {"public_key": "-----BEGIN..."}}
        body = json.loads(body)["data"]["public_key"]
    except (ValueError, KeyError, TypeError):
        pass
    return parse_public_key(body)


_SEEN = __import__('collections').OrderedDict()  # bounded idempotency memory: Kick retries deliveries, a message must be processed once


def handle(headers, body, pubkey, sink, max_skew=300, now=None):
    """-> (http_status, text). Verifies, rejects replays older than max_skew s, then sink(epoch, user, text) for chat messages."""
    h = {k.lower(): v for k, v in headers.items()}
    mid, ts, sig = h.get("kick-event-message-id", ""), h.get("kick-event-message-timestamp", ""), h.get("kick-event-signature", "")
    if not (mid and ts and sig and verify(pubkey, f"{mid}.{ts}.".encode() + body, sig)):
        return 401, "bad signature"
    try:
        t = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 400, "bad timestamp"
    if abs((now if now is not None else __import__("time").time()) - t) > max_skew:
        return 401, "stale"
    if mid in _SEEN:
        return 200, "duplicate"
    _SEEN[mid] = 1
    while len(_SEEN) > 5000:
        _SEEN.popitem(last=False)
    if h.get("kick-event-type") == "chat.message.sent":
        try:
            j = json.loads(body)
            sink(t, (j.get("sender") or {}).get("username"), str(j.get("content", "")))
        except (ValueError, AttributeError):
            return 400, "bad body"
    return 200, "ok"


def serve(port, out_path, key=None, bind="127.0.0.1"):
    """Blocking receiver on 127.0.0.1 by default (put an HTTPS tunnel/reverse proxy in front: Kick needs a public HTTPS URL); only signed, fresh,
    non-duplicate requests are accepted. Verification status of real Kick delivery: see docs."""
    key = key or fetch_key()
    out = open(out_path, "a")

    def sink(t, u, m):
        out.write(json.dumps({"ts": t, "u": u, "m": m}) + "\n")
        out.flush()

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            code, msg = (413, "too large") if n > 262144 else handle(dict(self.headers), self.rfile.read(n), key, sink)
            self.send_response(code)
            self.end_headers()
            self.wfile.write(msg.encode())

        def log_message(self, *a):
            pass
    print(f"kick relay on :{port} -> {out_path}  (monitor with --chat-file {out_path})", flush=True)
    HTTPServer((bind, port), H).serve_forever()

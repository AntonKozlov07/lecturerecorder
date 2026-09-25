"""Use the app from a phone on the same network.

When switched on, the app is also served on the local network:

* https://<pc-ip>:8766  the app itself. Browsers only allow microphone access on
  secure pages, so this uses a certificate signed by a small certificate
  authority created on this PC. The phone installs and trusts that authority once.
* http://<pc-ip>:8767   a setup page that walks the phone through installing it.

Nothing on the network side works without pairing: the QR code shown on the PC
carries a short-lived code, and pairing gives the phone a long random token in
a cookie. Every request on the network ports must carry a known token.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import html
import ipaddress
import json
import logging
import secrets
import socket
import threading
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from pydantic import BaseModel

from . import db
from .config import DATA_DIR, load_settings, save_settings

log = logging.getLogger(__name__)
router = APIRouter()

HTTPS_PORT = 8766
SETUP_PORT = 8767
COOKIE = "lr_device"
CODE_LIFETIME = 30 * 60
PHONE_DIR = DATA_DIR / "phone"

_lock = threading.Lock()
_servers: list = []
_error = ""
_ips: list[str] = []
_code = {"value": "", "expires": 0.0}
_last_touch: dict[str, float] = {}


# Which requests came in over the network -----------------------------------

def _tag(app, kind: str):
    """Wrap the ASGI app so handlers can tell network requests from local ones."""
    async def tagged(scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            scope = {**scope, "lr_phone": kind}
        await app(scope, receive, send)
    return tagged


def is_phone_request(request: Request) -> bool:
    return request.scope.get("lr_phone") is not None


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _code_ok(code: str | None) -> bool:
    return bool(code) and bool(_code["value"]) and time.time() < _code["expires"] \
        and secrets.compare_digest(code, _code["value"])


def _device_token(request: Request) -> str | None:
    token = request.cookies.get(COOKIE)
    if token and db.device(_hash(token)):
        return token
    return None


def authorize(request: Request) -> Response | None:
    """Return a response to block a network request, or None to let it through."""
    if not load_settings()["phone_enabled"]:
        return PlainTextResponse("Phone access is turned off on the computer.", status_code=403)
    path = request.url.path
    if request.scope["lr_phone"] == "setup":
        if path in ("/phone/setup", "/phone/ca.crt") and _code_ok(request.query_params.get("code")):
            return None
        return HTMLResponse(_page("Code expired", "<p>This setup link has expired. On your computer, open "
                                  "<b>Phone</b> and scan the new code.</p>"), status_code=403)
    if path.startswith("/api/phone"):
        return PlainTextResponse("Forbidden", status_code=403)
    if path == "/pair" or path == "/manifest.webmanifest" or path.startswith("/static/icons/"):
        return None
    token = _device_token(request)
    if token:
        h = _hash(token)
        if time.time() - _last_touch.get(h, 0) > 300:
            _last_touch[h] = time.time()
            db.touch_device(h)
        return None
    if path == "/":
        return HTMLResponse(_page("Not paired", "<p>This phone is not paired, or it was removed. On your "
                                  "computer, open <b>Phone</b> and scan the code to pair again.</p>"), status_code=403)
    return PlainTextResponse("This device is not paired.", status_code=401)


# Certificates --------------------------------------------------------------

def lan_ips() -> list[str]:
    found: list[str] = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 80))  # no packet is sent; this picks the outgoing interface
            found.append(s.getsockname()[0])
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.append(info[4][0])
    except OSError:
        pass
    tailscale = ipaddress.ip_network("100.64.0.0/10")
    ips = []
    for ip in found:
        addr = ipaddress.ip_address(ip)
        usable = (addr.is_private or addr in tailscale) and not addr.is_loopback and not addr.is_link_local
        if usable and ip not in ips:
            ips.append(ip)
    return ips


def _ensure_certs(ips: list[str]) -> tuple[str, str]:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    PHONE_DIR.mkdir(parents=True, exist_ok=True)
    ca_key_path, ca_cert_path = PHONE_DIR / "ca.key", PHONE_DIR / "ca.crt"
    key_path, cert_path, meta_path = PHONE_DIR / "server.key", PHONE_DIR / "server.crt", PHONE_DIR / "server.json"
    now = dt.datetime.now(dt.timezone.utc)
    pem = serialization.Encoding.PEM
    no_pw = serialization.NoEncryption()
    pkcs8 = serialization.PrivateFormat.PKCS8

    if ca_key_path.exists() and ca_cert_path.exists():
        ca_key = serialization.load_pem_private_key(ca_key_path.read_bytes(), None)
        ca_cert = x509.load_pem_x509_certificate(ca_cert_path.read_bytes())
    else:
        ca_key = ec.generate_private_key(ec.SECP256R1())
        host = socket.gethostname().split(".")[0][:40] or "this computer"  # names are capped at 64 characters
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"Lecture Recorder ({host})"),
                          x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Lecture Recorder")])
        ca_cert = (
            x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True,
                                         content_commitment=False, key_encipherment=False, data_encipherment=False,
                                         key_agreement=False, encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
            .sign(ca_key, hashes.SHA256())
        )
        ca_key_path.write_bytes(ca_key.private_bytes(pem, pkcs8, no_pw))
        ca_cert_path.write_bytes(ca_cert.public_bytes(pem))

    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    fresh = (cert_path.exists() and key_path.exists() and sorted(meta.get("ips", [])) == sorted(ips)
             and meta.get("expires", 0) - time.time() > 30 * 86400)
    if not fresh:
        key = ec.generate_private_key(ec.SECP256R1())
        expires = now + dt.timedelta(days=397)  # Apple rejects server certificates valid over 398 days
        sans = [x509.IPAddress(ipaddress.ip_address(ip)) for ip in ips] + [x509.DNSName("localhost")]
        cert = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, ips[0] if ips else "localhost")]))
            .issuer_name(ca_cert.subject).public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(days=1)).not_valid_after(expires)
            .add_extension(x509.SubjectAlternativeName(sans), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, key_encipherment=False, content_commitment=False,
                                         data_encipherment=False, key_agreement=False, key_cert_sign=False,
                                         crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
            .sign(ca_key, hashes.SHA256())
        )
        key_path.write_bytes(key.private_bytes(pem, pkcs8, no_pw))
        cert_path.write_bytes(cert.public_bytes(pem))
        meta_path.write_text(json.dumps({"ips": ips, "expires": expires.timestamp()}))
    return str(key_path), str(cert_path)


def _ca_der() -> bytes:
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization

    cert = x509.load_pem_x509_certificate((PHONE_DIR / "ca.crt").read_bytes())
    return cert.public_bytes(serialization.Encoding.DER)


# Servers -------------------------------------------------------------------

def start() -> None:
    """Serve the app on the local network. Safe to call when already running."""
    global _error, _ips
    import uvicorn

    from .server import app

    with _lock:
        if _servers:
            return
        _error = ""
        _ips = lan_ips()
        if not _ips:
            _error = "This computer doesn't seem to be on a network. Connect to Wi-Fi and try again."
            return
        keyfile, certfile = _ensure_certs(_ips)
        configs = [
            uvicorn.Config(_tag(app, "https"), host="0.0.0.0", port=HTTPS_PORT, lifespan="off",
                           log_level="warning", ssl_keyfile=keyfile, ssl_certfile=certfile),
            uvicorn.Config(_tag(app, "setup"), host="0.0.0.0", port=SETUP_PORT, lifespan="off",
                           log_level="warning"),
        ]
        servers = [uvicorn.Server(c) for c in configs]
        threads = [threading.Thread(target=s.run, name=f"phone-{c.port}", daemon=True)
                   for s, c in zip(servers, configs)]
        for t in threads:
            t.start()
        deadline = time.time() + 10
        while time.time() < deadline and not all(s.started for s in servers):
            if not all(t.is_alive() for t in threads):
                break
            time.sleep(0.05)
        if not all(s.started for s in servers):
            for s in servers:
                s.should_exit = True
            _error = (f"Could not open ports {HTTPS_PORT} and {SETUP_PORT}. Another program may be using "
                      "them, or a firewall blocked them.")
            log.error(_error)
            return
        _servers.extend(servers)
        log.info("Phone access on https://%s:%s", _ips[0], HTTPS_PORT)


def stop() -> None:
    with _lock:
        for s in _servers:
            s.should_exit = True
        _servers.clear()


def running() -> bool:
    return bool(_servers)


# Routes --------------------------------------------------------------------

def _local_only(request: Request) -> None:
    if is_phone_request(request):
        raise HTTPException(403, "Only available on the computer")


def _new_code() -> str:
    if time.time() > _code["expires"] - 60:
        _code["value"] = secrets.token_urlsafe(12)
        _code["expires"] = time.time() + CODE_LIFETIME
    return _code["value"]


def _qr_svg(data: str) -> str:
    import qrcode
    import qrcode.image.svg

    img = qrcode.make(data, image_factory=qrcode.image.svg.SvgPathImage, box_size=8, border=2)
    return img.to_string(encoding="unicode")


@router.get("/api/phone")
def phone_status(request: Request):
    _local_only(request)
    enabled = load_settings()["phone_enabled"]
    info = {"enabled": enabled, "running": running(), "error": _error, "ips": _ips,
            "https_port": HTTPS_PORT, "setup_port": SETUP_PORT,
            "devices": [{"id": d["token_hash"], "name": d["name"], "created_at": d["created_at"],
                         "last_seen": d["last_seen"]} for d in db.list_devices()]}
    if enabled and running():
        code = _new_code()
        info["setup_url"] = f"http://{_ips[0]}:{SETUP_PORT}/phone/setup?code={code}"
        info["app_url"] = f"https://{_ips[0]}:{HTTPS_PORT}/"
        info["qr_svg"] = _qr_svg(info["setup_url"])
        info["code_expires"] = _code["expires"]
    return info


class PhoneIn(BaseModel):
    enabled: bool


@router.put("/api/phone")
def phone_toggle(request: Request, body: PhoneIn):
    _local_only(request)
    save_settings({"phone_enabled": body.enabled})
    if body.enabled:
        start()
    else:
        stop()
    return phone_status(request)


@router.delete("/api/phone/devices/{device_id}")
def phone_forget(request: Request, device_id: str):
    _local_only(request)
    db.delete_device(device_id)
    return {"ok": True}


@router.get("/phone/ca.crt")
def phone_ca():
    return Response(_ca_der(), media_type="application/x-x509-ca-cert",
                    headers={"Content-Disposition": 'attachment; filename="LectureRecorder.cer"'})


@router.get("/phone/setup", response_class=HTMLResponse)
def phone_setup(request: Request, code: str):
    ip = request.url.hostname or (_ips[0] if _ips else "")
    app_url = f"https://{ip}:{HTTPS_PORT}/pair?code={code}"
    body = f"""
      <p class="lead">Three one-time steps let this phone use Lecture Recorder on your computer
      securely. Your phone must stay on the same Wi-Fi as the computer.</p>
      <ol>
        <li><b>Download the certificate.</b><br>
          <a class="btn" href="/phone/ca.crt?code={html.escape(code)}">Download certificate</a><br>
          <span class="muted">Tap <b>Allow</b>, then <b>Close</b>.</span></li>
        <li><b>Install it.</b> Open <b>Settings</b>, tap <b>Profile Downloaded</b> near the top
          (or <b>General &gt; VPN &amp; Device Management</b>), choose <b>Lecture Recorder</b> and tap <b>Install</b>.</li>
        <li><b>Trust it.</b> In <b>Settings &gt; General &gt; About &gt; Certificate Trust Settings</b>,
          turn on <b>Lecture Recorder</b>.</li>
        <li><b>Open the app.</b><br><a class="btn" href="{html.escape(app_url)}">Open Lecture Recorder</a><br>
          <span class="muted">Then tap <b>Share</b> and <b>Add to Home Screen</b> so it opens like an app.</span></li>
      </ol>
      <p class="muted">Already did steps 1 to 3 on this phone? Go straight to step 4.<br>
      Android: install the certificate under Settings &gt; Security &gt; Encryption &amp; credentials &gt;
      Install a certificate &gt; CA certificate, then open the app.</p>"""
    return _page("Set up your phone", body)


@router.get("/pair")
def pair(request: Request, code: str = "", device: str = ""):
    if device and db.device(_hash(device)):
        token = device
    elif _code_ok(code):
        token = secrets.token_urlsafe(32)
        agent = request.headers.get("user-agent", "")
        name = next((n for n in ("iPhone", "iPad", "Android") if n in agent), "Phone")
        db.add_device(_hash(token), f"{name}, paired {dt.date.today():%b %d}")
    else:
        return HTMLResponse(_page("Link expired", "<p>This pairing link has expired. On your computer, open "
                                  "<b>Phone</b> and scan the new code.</p>"), status_code=403)
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(COOKIE, token, max_age=10 * 365 * 86400, secure=True, httponly=True, samesite="lax")
    return resp


@router.get("/manifest.webmanifest")
def manifest(request: Request):
    # Home-screen apps on iPhone keep their own cookies, so the start URL re-pairs
    # with this phone's token when the app is opened from the home screen.
    token = _device_token(request) if is_phone_request(request) else None
    return JSONResponse({
        "name": "Lecture Recorder",
        "short_name": "Lectures",
        "start_url": f"/pair?device={token}" if token else "/",
        "scope": "/",
        "display": "standalone",
        "background_color": "#f7f6f3",
        "theme_color": "#f7f6f3",
        "icons": [
            {"src": "/static/icons/icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/static/icons/icon-512.png", "sizes": "512x512", "type": "image/png"},
        ],
    }, media_type="application/manifest+json", headers={"Cache-Control": "no-store"})


def _page(title: str, body: str) -> str:
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{html.escape(title)}</title>
<style>
:root {{ --bg:#f7f6f3; --surface:#fff; --text:#1d1d1b; --muted:#6b6963; --border:#dedbd3; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#171716; --surface:#1f1f1d; --text:#e7e5df; --muted:#a29f96; --border:#32312d; }} }}
body {{ margin:0; background:var(--bg); color:var(--text); font:16px/1.55 -apple-system, system-ui, sans-serif; }}
main {{ max-width:560px; margin:0 auto; padding:28px 18px 40px; }}
h1 {{ font-size:22px; margin:0 0 10px; }}
.lead {{ color:var(--muted); }}
ol {{ padding-left:22px; }} li {{ margin:0 0 18px; }}
.btn {{ display:inline-block; margin:8px 0 4px; padding:10px 16px; border-radius:6px; background:var(--text);
        color:var(--bg); text-decoration:none; font-weight:600; }}
.muted {{ color:var(--muted); font-size:14px; }}
</style></head><body><main><h1>{html.escape(title)}</h1>{body}</main></body></html>"""

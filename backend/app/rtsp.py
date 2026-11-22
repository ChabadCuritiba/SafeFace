"""RTSP helpers: build Hikvision URLs and diagnose connection problems.

OpenCV only says "can't open" when a stream fails. probe() talks to the camera
directly (a single RTSP DESCRIBE request, with Basic or Digest auth) so the user
gets a specific answer: unreachable IP, wrong password, wrong channel...
"""
from __future__ import annotations

import base64
import hashlib
import os
import re
import socket
from dataclasses import dataclass
from urllib.parse import quote, unquote, urlsplit

HIK_PATH = re.compile(r"^/Streaming/Channels/(\d+?)0([12])/?$", re.I)


def hikvision_url(ip: str, username: str, password: str, channel: int = 1, stream: str = "sub", port: int = 554) -> str:
    """Hikvision camera or NVR channel. Channel 1 = the camera itself / NVR camera 1.
    stream 'main' = full resolution (x01), 'sub' = lighter sub-stream (x02)."""
    ip = ip.strip()
    if not re.fullmatch(r"[A-Za-z0-9.\-:\[\]]+", ip):
        raise ValueError("The IP address contains invalid characters")
    if not 1 <= int(channel) <= 256:
        raise ValueError("Channel must be between 1 and 256")
    if not 1 <= int(port) <= 65535:
        raise ValueError("Port must be between 1 and 65535")
    creds = f"{quote(username.strip(), safe='')}:{quote(password, safe='')}@" if username.strip() else ""
    return f"rtsp://{creds}{ip}:{int(port)}/Streaming/Channels/{int(channel)}0{2 if stream == 'sub' else 1}"


def parse_hikvision(source: str) -> dict | None:
    """Reverse of hikvision_url (without the password), for pre-filling the edit form."""
    try:
        u = urlsplit(source)
    except ValueError:
        return None
    if u.scheme.lower() != "rtsp" or not u.hostname:
        return None
    m = HIK_PATH.match(u.path or "")
    if not m:
        return None
    return {
        "ip": u.hostname,
        "port": u.port or 554,
        "username": unquote(u.username or ""),
        "has_password": bool(u.password),
        "channel": int(m.group(1)),
        "stream": "sub" if m.group(2) == "2" else "main",
    }


def replace_password(new_source: str, old_source: str) -> str:
    """If the new URL has a username but no password, reuse the old password."""
    n, o = urlsplit(new_source), urlsplit(old_source)
    if n.username and not n.password and o.password:
        netloc = f"{quote(unquote(n.username), safe='')}:{o.password}@{n.hostname}" + (f":{n.port}" if n.port else "")
        return n._replace(netloc=netloc).geturl()
    return new_source


@dataclass
class ProbeResult:
    ok: bool
    code: str          # ok | unreachable | refused | timeout | auth | not_found | not_rtsp | error
    message: str


def _digest(user: str, pwd: str, method: str, uri: str, chal: dict) -> str:
    realm, nonce = chal.get("realm", ""), chal.get("nonce", "")
    ha1 = hashlib.md5(f"{user}:{realm}:{pwd}".encode()).hexdigest()
    ha2 = hashlib.md5(f"{method}:{uri}".encode()).hexdigest()
    resp = hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest()
    return f'Digest username="{user}", realm="{realm}", nonce="{nonce}", uri="{uri}", response="{resp}"'


def _request(sock: socket.socket, method: str, uri: str, cseq: int, auth: str | None) -> tuple[int, dict]:
    lines = [f"{method} {uri} RTSP/1.0", f"CSeq: {cseq}", "User-Agent: SafeFace", "Accept: application/sdp"]
    if auth:
        lines.append(f"Authorization: {auth}")
    sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
    data = b""
    while b"\r\n\r\n" not in data and len(data) < 65536:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data += chunk
    head = data.split(b"\r\n\r\n", 1)[0].decode(errors="replace").split("\r\n")
    m = re.match(r"RTSP/1\.\d\s+(\d{3})", head[0] if head else "")
    if not m:
        raise ValueError("not an RTSP server")
    headers: dict[str, list[str]] = {}
    for line in head[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers.setdefault(k.strip().lower(), []).append(v.strip())
    return int(m.group(1)), headers


def probe(source: str, timeout: float = 5.0) -> ProbeResult:
    """Check an RTSP URL step by step. Webcams and files are reported as 'not_rtsp'."""
    try:
        u = urlsplit(source)
    except ValueError:
        return ProbeResult(False, "error", "That address isn't a valid URL.")
    if u.scheme.lower() != "rtsp":
        if source.strip().isdigit() or os.path.exists(source):
            return ProbeResult(True, "not_rtsp", "Local camera or file.")
        return ProbeResult(False, "error", "Addresses must start with rtsp:// (or be a webcam number such as 0).")
    host, port = u.hostname, u.port or 554
    user, pwd = unquote(u.username or ""), unquote(u.password or "")
    uri = u._replace(netloc=f"{host}:{port}").geturl()  # no credentials on the wire
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
    except socket.timeout:
        return ProbeResult(False, "timeout", f"No answer from {host}:{port}. Check the IP address and that this PC is on the same network as the camera/NVR.")
    except ConnectionRefusedError:
        return ProbeResult(False, "refused", f"{host} answered but port {port} is closed. Enable RTSP on the camera/NVR (Configuration → Network → Advanced → Integration Protocol) or check the port.")
    except OSError as e:
        return ProbeResult(False, "unreachable", f"Can't reach {host}: {e.strerror or e}. Check the IP address.")
    try:
        sock.settimeout(timeout)
        code, headers = _request(sock, "DESCRIBE", uri, 1, None)
        if code == 401:
            if not user:
                return ProbeResult(False, "auth", "The camera needs a username and password.")
            chal_hdr = " ".join(headers.get("www-authenticate", []))
            if "digest" in chal_hdr.lower():
                digest_part = next(h for h in headers["www-authenticate"] if h.lower().startswith("digest"))
                chal = dict(re.findall(r'(\w+)="([^"]*)"', digest_part))
                auth = _digest(user, pwd, "DESCRIBE", uri, chal)
            else:
                auth = "Basic " + base64.b64encode(f"{user}:{pwd}".encode()).decode()
            code, headers = _request(sock, "DESCRIBE", uri, 2, auth)
        if code == 200:
            return ProbeResult(True, "ok", "Camera found and login accepted.")
        if code in (401, 403):
            return ProbeResult(False, "auth", "Wrong username or password. Careful: Hikvision locks the account for 30 minutes after several wrong tries.")
        if code in (404, 454):
            return ProbeResult(False, "not_found", "Login worked, but that channel/stream doesn't exist. Check the channel number (NVR camera 1 = channel 1, camera 2 = channel 2…).")
        return ProbeResult(False, "error", f"The camera answered with RTSP error {code}.")
    except (socket.timeout, TimeoutError):
        return ProbeResult(False, "timeout", f"{host}:{port} accepted the connection but didn't answer like a camera.")
    except (ValueError, OSError, StopIteration) as e:
        return ProbeResult(False, "not_rtsp", f"{host}:{port} doesn't look like an RTSP camera ({e}).")
    finally:
        sock.close()

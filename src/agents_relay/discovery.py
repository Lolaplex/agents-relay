"""Peer discovery: ``tailscale status --json`` is the tailnet roster; each
peer is probed for ``klanker serve`` on the well-known ports. The HTTP
probe decides liveness — tailscale's online flag is a last-contact guess.

Server-side mirror of the desktop's native discovery so the web surface
gets the same scan without a local tailscale CLI.
"""

from __future__ import annotations

import json
import socket
import subprocess
from concurrent.futures import ThreadPoolExecutor

# Ports a `klanker serve` commonly binds (desktop default + suite default).
DISCOVERY_PORTS = (8787, 8000)
PROBE_TIMEOUT = 0.4


def _tailscale_peers() -> list[tuple[str, str, str]]:
    """(hostname, ip, fqdn) for every tailnet peer — online or not."""
    try:
        out = subprocess.run(
            ["tailscale", "status", "--json"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if out.returncode != 0:
        return []
    try:
        doc = json.loads(out.stdout)
    except json.JSONDecodeError:
        return []
    peers = []
    for v in (doc.get("Peer") or {}).values():
        ips = v.get("TailscaleIPs") or []
        if not ips:
            continue
        host = str(v.get("HostName") or ips[0])
        fqdn = str(v.get("DNSName") or "").rstrip(".")
        peers.append((host, str(ips[0]), fqdn))
    return peers


def _probe(ip: str, port: int) -> bool:
    """True when ``ip:port`` answers ``GET /health`` with a 200 line."""
    try:
        with socket.create_connection((ip, port), timeout=PROBE_TIMEOUT) as s:
            s.sendall(b"GET /health HTTP/1.1\r\nHost: klanker\r\nConnection: close\r\n\r\n")
            s.settimeout(PROBE_TIMEOUT)
            head = s.recv(64)
    except OSError:
        return False
    return head.startswith(b"HTTP/1.1 200") or head.startswith(b"HTTP/1.0 200")


def discover_peers(ports: tuple[int, ...] = DISCOVERY_PORTS) -> list[dict]:
    with ThreadPoolExecutor(max_workers=64) as pool:
        futs = {
            pool.submit(_probe, ip, port): (host, ip, fqdn, port)
            for host, ip, fqdn in _tailscale_peers()
            for port in ports
        }
        found = [
            {
                "hostname": host,
                "ip": ip,
                "fqdn": fqdn or None,
                "endpoint": f"http://{ip}:{port}",
            }
            for fut, (host, ip, fqdn, port) in futs.items()
            if fut.result()
        ]
    return found

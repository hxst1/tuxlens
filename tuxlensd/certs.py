"""Local certificate authority for the phone page.

Safari only grants camera access on secure origins, so the daemon serves HTTPS with a server
certificate signed by a per-install CA. The user installs the CA once on the phone (Settings →
General → About → Certificate Trust Settings). Certificates follow Apple's rules for TLS server
certs: SAN present, serverAuth EKU, ECDSA P-256, SHA-256, validity <= 825 days.

Uses the openssl CLI to avoid a Python crypto dependency.
"""
from __future__ import annotations

import ipaddress
import json
import socket
import subprocess
import tempfile
from pathlib import Path

from .config import CERT_DIR

CA_KEY = CERT_DIR / "ca.key"
CA_CERT = CERT_DIR / "ca.crt"
SERVER_KEY = CERT_DIR / "server.key"
SERVER_CERT = CERT_DIR / "server.crt"
SERVER_META = CERT_DIR / "server.json"


def _run(*args: str) -> None:
    subprocess.run(["openssl", *args], check=True, capture_output=True)


def local_hostname() -> str:
    return socket.gethostname().split(".")[0] + ".local"


def lan_addresses() -> list[str]:
    """Private IPv4 addresses of this machine (no loopback, no docker bridges)."""
    addrs: set[str] = set()
    try:
        out = subprocess.run(["ip", "-j", "-4", "addr"], capture_output=True, text=True, check=True).stdout
        for iface in json.loads(out):
            name = iface.get("ifname", "")
            if name == "lo" or name.startswith(("docker", "br-", "veth", "virbr")):
                continue
            for info in iface.get("addr_info", []):
                ip = ipaddress.ip_address(info["local"])
                if ip.is_private and not ip.is_loopback:
                    addrs.add(str(ip))
    except (OSError, subprocess.CalledProcessError, ValueError, KeyError):
        pass
    return sorted(addrs)


def _ensure_ca() -> None:
    if CA_KEY.exists() and CA_CERT.exists():
        return
    CERT_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    _run("ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", str(CA_KEY))
    CA_KEY.chmod(0o600)
    host = socket.gethostname().split(".")[0]
    _run("req", "-x509", "-new", "-key", str(CA_KEY), "-sha256", "-days", "3650",
         "-subj", f"/CN=TuxLens local CA ({host})/O=TuxLens",
         "-addext", "basicConstraints=critical,CA:TRUE,pathlen:0",
         "-addext", "keyUsage=critical,keyCertSign,cRLSign",
         "-out", str(CA_CERT))


def ensure_server_cert() -> tuple[Path, Path]:
    """Create or refresh the server certificate so it covers the current hostname and IPs."""
    _ensure_ca()
    names = [local_hostname(), "localhost"]
    ips = lan_addresses() + ["127.0.0.1"]
    wanted = {"dns": names, "ip": ips}
    try:
        current = json.loads(SERVER_META.read_text())
    except (OSError, json.JSONDecodeError):
        current = None
    if current == wanted and SERVER_CERT.exists() and SERVER_KEY.exists():
        try:  # still valid for at least 30 days?
            _run("x509", "-checkend", str(30 * 86400), "-noout", "-in", str(SERVER_CERT))
            return SERVER_CERT, SERVER_KEY
        except subprocess.CalledProcessError:
            pass

    san = ",".join([f"DNS:{n}" for n in names] + [f"IP:{i}" for i in ips])
    with tempfile.TemporaryDirectory() as tmp:
        csr = Path(tmp) / "server.csr"
        ext = Path(tmp) / "ext.cnf"
        ext.write_text(
            "basicConstraints=critical,CA:FALSE\n"
            "keyUsage=critical,digitalSignature\n"
            "extendedKeyUsage=serverAuth\n"
            f"subjectAltName={san}\n"
        )
        _run("ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", str(SERVER_KEY))
        SERVER_KEY.chmod(0o600)
        _run("req", "-new", "-key", str(SERVER_KEY), "-subj", f"/CN={names[0]}", "-out", str(csr))
        _run("x509", "-req", "-in", str(csr), "-CA", str(CA_CERT), "-CAkey", str(CA_KEY),
             "-CAcreateserial", "-days", "800", "-sha256", "-extfile", str(ext), "-out", str(SERVER_CERT))
    SERVER_META.write_text(json.dumps(wanted))
    return SERVER_CERT, SERVER_KEY


def ca_der() -> bytes:
    """CA certificate in DER form: iOS offers to install it as a profile when downloaded."""
    _ensure_ca()
    return subprocess.run(["openssl", "x509", "-in", str(CA_CERT), "-outform", "DER"],
                          capture_output=True, check=True).stdout

"""Pairing by approval: the phone asks, the user accepts on the PC.

The phone page (unpaired, or holding a stale token) posts a request; the PC shows a desktop
notification with Accept / Reject actions and lists the request in the panel. Once accepted, the
waiting phone receives the pairing token. Nothing is granted without an explicit click on the PC.
"""
from __future__ import annotations

import asyncio
import logging
import secrets
import shutil
import time
from dataclasses import dataclass, field

from .config import pairing_token

log = logging.getLogger("tuxlens.pairing")

MAX_PENDING = 3
REQUEST_TTL_S = 120

TEXTS = {
    "es": ("TuxLens", "{name} ({address}) quiere usarse como cámara", "Aceptar", "Rechazar"),
    "en": ("TuxLens", "{name} ({address}) wants to be your camera", "Accept", "Reject"),
}


@dataclass
class PairRequest:
    id: str
    name: str
    address: str
    created: float = field(default_factory=time.monotonic)
    decided: asyncio.Event = field(default_factory=asyncio.Event)
    approved: bool | None = None
    notify_task: asyncio.Task | None = None

    def public(self) -> dict:
        return {"id": self.id, "name": self.name, "address": self.address,
                "age_s": round(time.monotonic() - self.created)}


class Pairing:
    def __init__(self, language_getter, on_change=None) -> None:
        self.requests: dict[str, PairRequest] = {}
        self.language_getter = language_getter
        self.on_change = on_change or (lambda: None)

    def pending(self) -> list[dict]:
        self._expire()
        return [r.public() for r in self.requests.values() if r.approved is None]

    def _expire(self) -> None:
        now = time.monotonic()
        for rid, req in list(self.requests.items()):
            if now - req.created > REQUEST_TTL_S:
                if req.approved is None:
                    req.approved = False
                    req.decided.set()
                self._cancel_notification(req)
                del self.requests[rid]

    def create(self, name: str, address: str) -> PairRequest | None:
        self._expire()
        # One pending request per address; refresh it instead of stacking notifications.
        for req in self.requests.values():
            if req.address == address and req.approved is None:
                return req
        if sum(1 for r in self.requests.values() if r.approved is None) >= MAX_PENDING:
            return None
        req = PairRequest(secrets.token_urlsafe(9), (name or "Phone")[:40], address)
        self.requests[req.id] = req
        req.notify_task = asyncio.create_task(self._notify(req))
        log.info("pairing requested by %s (%s)", req.name, address)
        self.on_change()
        return req

    def decide(self, rid: str, approve: bool) -> bool:
        req = self.requests.get(rid)
        if req is None or req.approved is not None:
            return False
        req.approved = approve
        req.decided.set()
        self._cancel_notification(req)
        log.info("pairing %s for %s (%s)", "approved" if approve else "rejected", req.name, req.address)
        self.on_change()
        return True

    async def wait(self, rid: str, timeout: float) -> str | None:
        """Token if approved, '' if rejected, None if still pending after the timeout."""
        req = self.requests.get(rid)
        if req is None:
            return ""
        try:
            await asyncio.wait_for(req.decided.wait(), timeout)
        except asyncio.TimeoutError:
            return None
        return pairing_token() if req.approved else ""

    # -- desktop notification -----------------------------------------------------------------
    async def _notify(self, req: PairRequest) -> None:
        if not shutil.which("notify-send"):
            return
        lang = self.language_getter()
        title, body, yes, no = TEXTS.get(lang, TEXTS["en"])
        try:
            proc = await asyncio.create_subprocess_exec(
                "notify-send", "-a", "TuxLens", "-u", "critical", "-i", "camera-web",
                "-A", f"approve={yes}", "-A", f"reject={no}", "-t", str(REQUEST_TTL_S * 1000),
                title, body.format(name=req.name, address=req.address),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            out, _ = await proc.communicate()
        except asyncio.CancelledError:
            if proc.returncode is None:
                proc.terminate()
            raise
        except OSError:
            return
        choice = out.decode().strip()
        if choice in ("approve", "reject"):
            self.decide(req.id, choice == "approve")

    @staticmethod
    def _cancel_notification(req: PairRequest) -> None:
        if req.notify_task and not req.notify_task.done():
            req.notify_task.cancel()

"""Attack scenarios never leave the machine: connections to non-loopback addresses fail."""

from __future__ import annotations

import ipaddress
import socket

import pytest


@pytest.fixture(autouse=True)
def loopback_only(monkeypatch):
    real = socket.socket.connect

    def connect(self, address):
        host = address[0] if isinstance(address, tuple) else None
        try:
            local = host is None or ipaddress.ip_address(host).is_loopback
        except ValueError:
            local = host == "localhost"
        if not local:
            raise ConnectionRefusedError(f"attack tests: no network to {host!r}")
        return real(self, address)

    monkeypatch.setattr(socket.socket, "connect", connect)

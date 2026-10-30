"""Linux network admission for the optional isolated service carrier."""
from __future__ import annotations

import socket
from pathlib import Path


def require_isolated_outer_network() -> None:
    try:
        interfaces = socket.if_nameindex()
        if not interfaces or len(interfaces) > 128:
            raise ValueError
        loopback = set()
        for _, name in interfaces:
            flags = int(Path('/sys/class/net/'+name+'/flags').read_text().strip(), 16)
            if not 0 <= flags <= 0xFFFF:
                raise ValueError
            if flags & 0x8:  # Linux IFF_LOOPBACK, independently of the interface name.
                loopback.add(name)
            elif flags & 0x1:  # Linux IFF_UP: any active external link refuses admission.
                raise ValueError
        if not loopback:
            raise ValueError
        ipv4 = Path('/proc/net/route').read_text().splitlines()
        ipv6 = Path('/proc/net/ipv6_route').read_text().splitlines()
        if not ipv4 or not ipv4[0].startswith('Iface'):
            raise ValueError
        if any(len(row.split()) < 4 or row.split()[0] not in loopback for row in ipv4[1:] if row.strip()):
            raise ValueError
        if any(len(row.split()) != 10 or row.split()[-1] not in loopback for row in ipv6 if row.strip()):
            raise ValueError
    except (OSError, ValueError):
        raise ValueError('private runtime consumer requires isolated outer network') from None

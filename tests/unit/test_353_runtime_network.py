"""Isolation depends on kernel link state and routes, not adapter names."""
from __future__ import annotations

from types import SimpleNamespace

import pytest


@pytest.mark.parametrize('problem', [None, 'active_link', 'ipv4_route', 'ipv6_route', 'missing_flags'])
def test_outer_network_rejects_connectivity_but_allows_inactive_kernel_interfaces(monkeypatch, problem):
    from coordinare_service_inference.environment import runtime_network as network

    # Actual flags observed in Docker network=none on this kernel.
    files = {'/sys/class/net/lo/flags': '0x9', '/sys/class/net/arbitrary-tunnel/flags': '0x80',
             '/proc/net/route': 'Iface Destination Gateway Flags\n',
             '/proc/net/ipv6_route': '0 '*9+'lo\n'}
    if problem == 'active_link':
        files['/sys/class/net/arbitrary-tunnel/flags'] = '0x81'
    if problem == 'ipv4_route':
        files['/proc/net/route'] += 'arbitrary-tunnel 00000000 0100007F 0001\n'
    if problem == 'ipv6_route':
        files['/proc/net/ipv6_route'] += '0 '*9+'arbitrary-tunnel\n'
    if problem == 'missing_flags':
        del files['/sys/class/net/arbitrary-tunnel/flags']
    def read(path):
        try:
            return files[path]
        except KeyError:
            raise OSError('kernel link flags unavailable') from None
    monkeypatch.setattr(network.socket, 'if_nameindex', lambda: [(1, 'lo'), (2, 'arbitrary-tunnel')])
    monkeypatch.setattr(network, 'Path', lambda path: SimpleNamespace(read_text=lambda: read(path)))
    if problem is None:
        network.require_isolated_outer_network()
    else:
        with pytest.raises(ValueError, match='isolated outer network'):
            network.require_isolated_outer_network()

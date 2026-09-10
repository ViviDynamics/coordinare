"""Controlled loss of the destination cannot be mistaken for containment."""
from __future__ import annotations

import socket

from tests.utils.egress_measurement import ConnectionObservation, compare_observations


def observe(address):
    try:
        with socket.create_connection(address, timeout=1):
            return ConnectionObservation(0, 'ATTEMPTED\nREACHED\n')
    except OSError:
        return ConnectionObservation(1, 'ATTEMPTED\nCONNECT_FAILED\n')


def test_destination_loss_is_inconclusive():
    server = socket.socket()
    server.bind(('127.0.0.1', 0))
    server.listen()
    address = server.getsockname()
    before = observe(address)
    server.close()
    restricted = [observe(address), observe(address)]
    after = observe(address)
    assert before.outcome == 'reached'
    assert all(item.outcome == 'failed' for item in restricted)
    assert compare_observations(before, restricted, after) is None


def test_repeatable_results_require_controls_and_real_attempts():
    reached = ConnectionObservation(0, 'ATTEMPTED\nREACHED\n')
    failed = ConnectionObservation(1, 'ATTEMPTED\nCONNECT_FAILED\n')
    never_ran = ConnectionObservation(1, '')
    assert compare_observations(reached, [failed, failed], reached) is True
    assert compare_observations(reached, [reached, reached], reached) is False
    assert compare_observations(reached, [reached, failed], reached) is None
    assert compare_observations(reached, [never_ran, never_ran], reached) is None
    assert compare_observations(failed, [failed, failed], reached) is None

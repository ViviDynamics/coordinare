from __future__ import annotations

import time
from typing import TYPE_CHECKING

import httpx

from coordinare.doctor_status import Status

if TYPE_CHECKING:
    from collections.abc import Callable

PROBE_ATTEMPTS = 2
PROBE_RETRY_DELAY_SECONDS = 0.1
TRANSIENT_HTTP_CODES = {httpx.codes.REQUEST_TIMEOUT, httpx.codes.TOO_MANY_REQUESTS}


def response_status(response: httpx.Response) -> Status:
    if response.is_success:
        return Status.OK
    if response.is_server_error or response.status_code in TRANSIENT_HTTP_CODES:
        return Status.WARN
    return Status.FAIL


def error_status(error: Exception) -> Status:
    if isinstance(error, httpx.HTTPStatusError):
        return response_status(error.response)
    if isinstance(error, (httpx.TransportError, TimeoutError, ConnectionError)):
        return Status.WARN
    return Status.FAIL


def probe_http(operation: Callable[[], httpx.Response]) -> httpx.Response:
    """Retry only transient failures, with a fixed attempt and delay budget."""
    attempts_left = PROBE_ATTEMPTS
    while True:
        attempts_left -= 1
        try:
            response = operation()
        except httpx.HTTPError as exc:
            if error_status(exc) is not Status.WARN or not attempts_left:
                raise
        else:
            if response_status(response) is not Status.WARN or not attempts_left:
                return response
        time.sleep(PROBE_RETRY_DELAY_SECONDS)

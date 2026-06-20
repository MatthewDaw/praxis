"""Abstract transport contract that concrete fetchers must implement."""

from __future__ import annotations

import abc


class Transport(abc.ABC):
    """Base class for byte-returning HTTP-ish transports."""

    @abc.abstractmethod
    def fetch(self, url: str, timeout: float) -> bytes:
        """Fetch the resource at ``url`` and return its raw body bytes.

        Implementations MUST honor this exact signature: positional ``url``
        (str) and ``timeout`` (float), returning ``bytes``.
        """
        raise NotImplementedError

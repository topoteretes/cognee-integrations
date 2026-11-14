"""Where a request may go: the one loopback rule, and an opener that honours it.

Deliberately stdlib-only and import-free of the rest of the plugin, so every
script (the hooks, the observer shim, the Bash-run commands) can use it.

``is_loopback_host`` is the single test for "this machine". ``open_url`` is
the single way the plugin opens a URL: a request to this machine (the local
Cognee server, the observer shim) never goes through a proxy. Python's
``urlopen`` would otherwise send ``http://127.0.0.1:…`` to ``HTTP_PROXY``
unless ``NO_PROXY`` lists it, handing the request, with its key, to the proxy
host. A remote server is opened exactly as ``urlopen`` does, proxy included.
"""

from __future__ import annotations

import ipaddress
import urllib.parse
import urllib.request


def is_loopback_host(host: str) -> bool:
    """True for ``localhost`` and a literal loopback address (127.0.0.0/8, ::1).

    A name that merely starts with ``127.`` (``127.0.0.1.example.com``) is a
    remote host, and ``0.0.0.0`` is a bind address, not somewhere to send a
    request.
    """
    host = str(host or "").strip().lower().strip("[]")
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def is_loopback_url(url: str) -> bool:
    try:
        host = urllib.parse.urlparse(str(url or "").strip()).hostname or ""
    except ValueError:
        return False
    return is_loopback_host(host)


def _request_url(req) -> str:
    return str(getattr(req, "full_url", req) or "")


def open_url(req, *, timeout: float, context=None):
    """``urllib.request.urlopen``, except that this machine is reached directly.

    ``req`` is a URL or a ``urllib.request.Request``. With no proxy configured
    this is plain ``urlopen``.
    """
    if is_loopback_url(_request_url(req)) and urllib.request.getproxies():
        handlers = [urllib.request.ProxyHandler({})]
        if context is not None:
            handlers.append(urllib.request.HTTPSHandler(context=context))
        return urllib.request.build_opener(*handlers).open(req, timeout=timeout)
    if context is not None:
        return urllib.request.urlopen(req, timeout=timeout, context=context)
    return urllib.request.urlopen(req, timeout=timeout)

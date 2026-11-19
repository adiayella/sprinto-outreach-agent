"""Domain normalisation, URL canonicalisation and an SSRF guard."""
from __future__ import annotations

import ipaddress
import re
import socket
from urllib.parse import urljoin, urlsplit, urlunsplit

BLOCKED_HOSTS = {"localhost", "metadata.google.internal", "169.254.169.254"}
BLOCKED_SUFFIXES = (".local", ".internal", ".localhost", ".lan")


class UnsafeURL(ValueError):
    pass


def normalise_domain(raw: str) -> str:
    """'https://www.Acme.com/pricing' -> 'acme.com'."""
    s = raw.strip().lower()
    if not s:
        return ""
    if "://" not in s:
        s = "http://" + s
    host = urlsplit(s).hostname or ""
    host = host.removeprefix("www.").strip(".")
    if not re.fullmatch(r"[a-z0-9-]+(\.[a-z0-9-]+)+", host):
        return ""
    return host


def canonical(url: str) -> str:
    """Lowercase host, no default port, no fragment, no trailing slash, www treated as apex."""
    p = urlsplit(url)
    scheme = (p.scheme or "https").lower()
    host = (p.hostname or "").lower().removeprefix("www.")
    port = p.port
    netloc = host if port in (None, 80, 443) else f"{host}:{port}"
    path = re.sub(r"/+", "/", p.path or "/")
    if len(path) > 1:
        path = path.rstrip("/")
    return urlunsplit(("https" if scheme in ("http", "https") else scheme, netloc, path, p.query, ""))


def same_site(url: str, domain: str) -> bool:
    host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
    return host == domain or host.endswith("." + domain)


def absolute(base: str, href: str) -> str | None:
    if not href or href.startswith(("mailto:", "tel:", "javascript:", "#")):
        return None
    return urljoin(base, href)


def check_url(url: str, resolve: bool = True) -> None:
    """Raise UnsafeURL unless the URL is a public http(s) address on a default port."""
    p = urlsplit(url)
    if p.scheme not in ("http", "https"):
        raise UnsafeURL(f"scheme not allowed: {p.scheme}")
    if p.username or p.password:
        raise UnsafeURL("credentials in URL")
    if p.port not in (None, 80, 443):
        raise UnsafeURL("non-default port")
    host = (p.hostname or "").lower()
    if not host or host in BLOCKED_HOSTS or host.endswith(BLOCKED_SUFFIXES):
        raise UnsafeURL(f"blocked host: {host}")
    try:
        ip = ipaddress.ip_address(host)
        addrs = [ip]
    except ValueError:
        if not resolve:
            return
        try:
            infos = socket.getaddrinfo(host, None)
        except socket.gaierror as e:
            raise UnsafeURL(f"DNS lookup failed for {host}") from e
        addrs = [ipaddress.ip_address(i[4][0]) for i in infos]
    for a in addrs:
        if not a.is_global:
            raise UnsafeURL(f"non-public address for {host}")

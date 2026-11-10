"""Bounded public error categories: never expose URLs, tokens or response bodies."""
import json
import socket
import ssl
from urllib.error import HTTPError, URLError
from xml.etree import ElementTree


def failure_kind(exc: Exception) -> str:
    if isinstance(exc, HTTPError):
        return "http"
    if isinstance(exc, URLError) and isinstance(exc.reason, Exception):
        exc = exc.reason
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, socket.gaierror):
        return "dns"
    if isinstance(exc, ssl.SSLError):
        return "tls"
    if isinstance(exc, (ElementTree.ParseError, json.JSONDecodeError, ValueError)):
        return "schema"
    if isinstance(exc, OSError):
        return "connection"
    return "unknown"


def is_feed_root(tag: str) -> bool:
    return tag in {"rss", "feed", "{http://www.w3.org/2005/Atom}feed"}

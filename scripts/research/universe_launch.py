"""Isolated stdlib launcher: strip every credential except two read-only keys."""

import os
import sys
from pathlib import Path


def clean_environment(inherited, bundle):
    clean = {
        "PATH": "/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTHONSAFEPATH": "1",
        "PYTHONPATH": str(bundle),
        "SSL_CERT_FILE": "/etc/ssl/certs/ca-certificates.crt",
    }
    for key in ("MOEX_APIKEY", "T_INVEST_READONLY_TOKEN"):
        if inherited.get(key):
            clean[key] = inherited[key]
    return clean


if __name__ == "__main__":
    os.execve(
        "/usr/bin/python3",
        ["/usr/bin/python3", "-B", "-m", "universe_service", *sys.argv[1:]],
        clean_environment(dict(os.environ), Path(__file__).resolve().parent),
    )

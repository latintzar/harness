#!/usr/bin/env python3
"""Dedicated source preparation service for the Rocoto development preview."""
import asyncio
import os
from pathlib import Path
import pwd
import socket
import sys
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
from source_media import _main


def main():
    if os.geteuid() != 0 or socket.gethostname() != 'rocoto-staging':
        raise SystemExit('Run the commissioned media broker as root on rocoto-staging.')
    account = pwd.getpwnam('rocoto-video-preview')
    if account.pw_uid == 0 or account.pw_gid == 0:
        raise SystemExit('The preview requires its separate service account.')
    asyncio.run(_main(SimpleNamespace(app_uid=account.pw_uid, app_gid=account.pw_gid, document_previews=True)))


if __name__ == '__main__':
    main()

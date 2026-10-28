#!/usr/bin/env python3
"""Dedicated root provisioning service; unrelated to the preview web process."""
import asyncio
import hashlib
import json
import stat
import os
from pathlib import Path
import pwd
import signal
import socket
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from native_executor import BrokerServer, DockerBackend, Ext4Volumes, NativeBroker, IMAGE, RUNTIME


def configure_installer(broker, directory):
    # Optional installer requires an explicit root-owned reviewed commissioning
    # record. Older services keep their existing runtime without this capability.
    installation=directory/'installer-commissioning.json'
    if installation.exists():
        info=installation.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=0 or info.st_mode&0o022:
            raise SystemExit('Use a root-owned installer commissioning record.')
        configuration=json.loads(installation.read_bytes())
        names={'native_executor.py','native_package_mount.py','native_install_broker.py','native_install_worker.py','broker_service.py','installer-sources.json'}
        if (configuration.get('schema_version')!=1 or configuration.get('profile')!='native-plugin-install-v1'
                or configuration.get('image')!=IMAGE or configuration.get('runtime')!=RUNTIME
                or set(configuration.get('sources',{}))!=names):
            raise SystemExit('Review the full native installer source closure.')
        for name,expected in configuration['sources'].items():
            path=directory/name;details=path.lstat()
            if (not stat.S_ISREG(details.st_mode) or details.st_uid!=0 or details.st_mode&0o022
                    or hashlib.sha256(path.read_bytes()).hexdigest()!=expected):
                raise SystemExit('The commissioned native installer source changed.')
        from native_install_broker import Installer
        Installer(broker,allowed_sources=json.loads((directory/'installer-sources.json').read_text()))


def configure_custom(broker,directory):
    commissioning=directory/'custom-commissioning.json'
    if not commissioning.exists():return
    info=commissioning.lstat()
    if not stat.S_ISREG(info.st_mode)or info.st_uid!=0 or info.st_mode&0o022:
        raise SystemExit('Use a root-owned custom executor commissioning record.')
    configuration=json.loads(commissioning.read_bytes())
    names={'native_executor.py','native_package_mount.py','native_custom_broker.py','custom_worker.py','broker_service.py'}
    if(configuration.get('schema_version')!=1 or configuration.get('profile')!='https-python-read-v1'
        or configuration.get('image')!=IMAGE or configuration.get('runtime')!=RUNTIME
        or set(configuration.get('sources',{}))!=names):
        raise SystemExit('Review the full custom executor source closure.')
    for name,expected in configuration['sources'].items():
        path=directory/name;details=path.lstat()
        if(not stat.S_ISREG(details.st_mode)or details.st_uid!=0 or details.st_mode&0o022
            or hashlib.sha256(path.read_bytes()).hexdigest()!=expected):
            raise SystemExit('The commissioned custom executor source changed.')
    from native_custom_broker import CustomExecutor
    CustomExecutor(broker)


async def main():
    if os.geteuid() != 0 or socket.gethostname() != "rocoto-staging":
        raise SystemExit("Run the commissioned broker as root on rocoto-staging.")
    account = pwd.getpwnam("rocoto-video-preview")
    root = Path("/var/lib/rocoto-native-executor/broker")
    backend = DockerBackend(Ext4Volumes(root / "volumes"))
    broker = NativeBroker(root, Path("/run/rocoto-native-broker/runs"), backend,
                          app_uid=account.pw_uid, app_gid=account.pw_gid)
    configure_installer(broker, Path(__file__).parent)
    configure_custom(broker,Path(__file__).parent)
    server = BrokerServer(broker)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for name in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(name, stop.set)
    await server.start()
    try:
        await stop.wait()
    finally:
        await server.close()


if __name__ == "__main__":
    asyncio.run(main())

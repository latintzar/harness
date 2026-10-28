"""Run a harness command through the active vertical's source layout."""
from __future__ import annotations

import runpy
import sys

import layout


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments:
        print('Usage: python harness/run.py MODULE [ARGUMENT ...]', file=sys.stderr)
        return 2
    layout.install()
    command = layout.source(arguments[0])
    if command.name in ('layout.py', 'run.py'):
        raise ValueError('Choose an application command module.')
    previous = sys.argv
    try:
        sys.argv = [str(command), *arguments[1:]]
        runpy.run_module(arguments[0], run_name='__main__', alter_sys=True)
    finally:
        sys.argv = previous
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

"""Host CLI for admitting a real structured model response to a registered stream.

Requires a commissioned owner/source/profile. It queues the ordinary durable
projection workflow; it neither starts inference nor calls a model provider.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
from monitoring import MonitoringStore
from stream_ingestion import StreamIngestion


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state',required=True,type=Path)
    parser.add_argument('--owner',required=True)
    parser.add_argument('--context',required=True,type=Path)
    parser.add_argument('--response',required=True,type=Path)
    args=parser.parse_args(argv)
    if not args.state.is_dir() or not (args.state/'monitoring.sqlite').is_file():
        parser.error('Choose an existing commissioned workspace state.')
    if args.context.stat().st_size>100_000 or args.response.stat().st_size>2_000_000:
        parser.error('Use bounded host context and original response files.')
    context=json.loads(args.context.read_text())
    pipeline=StreamIngestion(args.state,MonitoringStore(args.state))
    receipt=pipeline.accept_model_response(args.owner,context,args.response.read_text())
    print(json.dumps(receipt,sort_keys=True))
    return receipt


if __name__=='__main__':main()

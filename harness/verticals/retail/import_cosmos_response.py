"""Prepare stock nvvllmvlm config, then admit its actual result and input trace."""
import argparse
import json
from pathlib import Path
from monitoring import MonitoringStore
from stream_ingestion import StreamIngestion
from cosmos_stream_bridge import prepare_prompt, admit_result


def load(path):
    if path.stat().st_size>4_000_000:raise ValueError('Use bounded original runner receipts.')
    return json.loads(path.read_text())


def write(path,value):
    with path.open('x') as output:json.dump(value,output,indent=2,allow_nan=False)
    path.chmod(0o600)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state',required=True,type=Path);parser.add_argument('--owner',required=True)
    sub=parser.add_subparsers(dest='command',required=True)
    prepare=sub.add_parser('prepare')
    for flag in ('context','tracker-labels','tracker-receipt','native-config','input-preparation','model-input','prepared-output','prompt-config-output'):
        prepare.add_argument('--'+flag,required=True,type=Path)
    prepare.add_argument('--clock-basis',required=True,choices=['source_absolute','segment_relative'])
    prepare.add_argument('--source-offset-s',required=True,type=float)
    admit=sub.add_parser('admit')
    for flag in ('prepared','envelope','input-trace'):admit.add_argument('--'+flag,required=True,type=Path)
    args=parser.parse_args(argv)
    if not (args.state/'monitoring.sqlite').is_file():parser.error('Choose a commissioned workspace state.')
    pipeline=StreamIngestion(args.state,MonitoringStore(args.state))
    if args.command=='prepare':
        import hashlib
        preparation=load(args.input_preparation)
        with args.model_input.open('rb') as media:
            actual=hashlib.file_digest(media,'sha256').hexdigest()
        if preparation.get('model_input_sha256')!=actual:parser.error('Use the exact OSD model-input bytes in the preparation receipt.')
        prepared=prepare_prompt(pipeline,args.owner,load(args.context),clock_basis=args.clock_basis,
            source_offset_s=args.source_offset_s,tracker_labels=load(args.tracker_labels),tracker_receipt=load(args.tracker_receipt),input_preparation=preparation)
        config=load(args.native_config)
        config['user_prompt']=prepared['user_prompt'];config['max_tokens']=max(int(config.get('max_tokens',0)),4096)
        if args.prepared_output.exists() or args.prompt_config_output.exists():parser.error('Choose new receipt paths to preserve earlier inputs.')
        write(args.prepared_output,prepared);write(args.prompt_config_output,config)
        receipt={'status':'prepared','prompt_sha256':prepared['prompt_sha256'],'profile_sha256':prepared['profile_sha256']}
    else:receipt=admit_result(pipeline,args.owner,load(args.prepared),load(args.envelope),load(args.input_trace))
    print(json.dumps(receipt,sort_keys=True))
    return receipt


if __name__=='__main__':main()

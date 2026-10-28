"""Fixed Office/PDF conversion. Execute only in the isolated document profile."""
from __future__ import annotations
import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path,PurePosixPath
import re
import shutil
import signal
import stat
import struct
import subprocess
import tempfile
import time
import zipfile
import xml.etree.ElementTree as ET

PROFILE='document-preview-v1'
RECIPE='rocoto-libreoffice-poppler-pages-v1'
FORMATS={'docx','pdf','xlsx'}
INPUT=Path('/input/original');OUTPUT=Path('/output')

class PreviewError(ValueError):pass

@dataclass(frozen=True)
class Limits:
    original_bytes:int=16*1024**2
    output_bytes:int=128*1024**2
    page_bytes:int=8*1024**2
    pages:int=40
    side:int=1600
    text_bytes:int=100*1024
    job_seconds:float=120
    command_seconds:float=90

def regular(path,maximum):
    info=path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or not 0<info.st_size<=maximum:
        raise PreviewError('document_file_limit')
    return info

def checksum(path):
    with path.open('rb') as stream:
        digest=hashlib.sha256()
        while chunk:=stream.read(65536):digest.update(chunk)
    return digest.hexdigest()

def tree_bytes(root):
    size=0
    for directory,dirs,files in os.walk(root,followlinks=False):
        for name in dirs:
            if Path(directory,name).is_symlink():raise PreviewError('unexpected_output')
        for name in files:
            path=Path(directory,name)
            try:info=path.lstat()
            except FileNotFoundError:continue # Converter temporary files can disappear during the bounded scan.
            if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1:raise PreviewError('unexpected_output')
            size+=info.st_size
    return size

def run(argv,work,limits,deadline,*,capture=256*1024):
    remaining=min(limits.command_seconds,deadline-time.monotonic())
    if remaining<=0:raise PreviewError('preview_timeout')
    with tempfile.TemporaryFile(dir=work) as out,tempfile.TemporaryFile(dir=work) as err:
        process=subprocess.Popen(argv,stdin=subprocess.DEVNULL,stdout=out,stderr=err,
            shell=False,start_new_session=True,close_fds=True,
            env={'PATH':'/usr/bin:/bin','LANG':'C.UTF-8','LC_ALL':'C.UTF-8',
                 'HOME':str(work),'TMPDIR':str(work),'SAL_USE_VCLPLUGIN':'svp'})
        try:
            end=time.monotonic()+remaining
            while process.poll() is None:
                if os.fstat(out.fileno()).st_size>capture or os.fstat(err.fileno()).st_size>256*1024:
                    raise PreviewError('preview_command_output_limit')
                if tree_bytes(work)>limits.output_bytes:raise PreviewError('preview_output_limit')
                if time.monotonic()>end:raise PreviewError('preview_timeout')
                time.sleep(.02)
            if process.returncode:raise PreviewError('document_conversion_failed')
            if os.fstat(out.fileno()).st_size>capture:raise PreviewError('preview_command_output_limit')
            out.seek(0);return out.read(capture+1)
        finally:
            if process.poll() is None:
                try:os.killpg(process.pid,signal.SIGKILL)
                except ProcessLookupError:pass
            process.wait()

def check_ooxml(path,format):
    # Every archive/XML inspection occurs in this worker, never in its host adapter.
    try:
        with zipfile.ZipFile(path) as archive:
            entries=archive.infolist()
            if not 1<=len(entries)<=2048 or sum(row.file_size for row in entries)>64*1024**2:
                raise PreviewError('document_archive_limit')
            names=set()
            for row in entries:
                p=PurePosixPath(row.filename)
                if p.is_absolute() or '..' in p.parts or '\\' in row.filename or row.filename in names:
                    raise PreviewError('invalid_document_archive')
                names.add(row.filename)
                if row.flag_bits&1 or row.file_size>16*1024**2 or (row.file_size>1024**2 and row.file_size>max(1,row.compress_size)*1000):
                    raise PreviewError('document_archive_limit')
                if stat.S_ISLNK(row.external_attr>>16):raise PreviewError('invalid_document_archive')
                lower=row.filename.lower()
                if any(x in lower for x in ('vbaproject','activex/','embeddings/','externallinks/')):
                    raise PreviewError('document_active_content')
                if lower.endswith('.rels'):
                    if row.file_size>1024**2:raise PreviewError('document_archive_limit')
                    xml=archive.read(row)
                    if b'<!DOCTYPE' in xml or b'<!ENTITY' in xml:raise PreviewError('document_active_content')
                    for rel in ET.fromstring(xml):
                        if rel.attrib.get('TargetMode')=='External' and not rel.attrib.get('Type','').endswith('/hyperlink'):
                            raise PreviewError('document_external_content')
            required='word/document.xml' if format=='docx' else 'xl/workbook.xml'
            if required not in names or '[Content_Types].xml' not in names:raise PreviewError('invalid_document_format')
    except (zipfile.BadZipFile,ET.ParseError,RuntimeError,EOFError) as error:
        raise PreviewError('invalid_document_format') from error

def png_dimensions(path):
    with path.open('rb') as stream:header=stream.read(24)
    if len(header)!=24 or header[:8]!=b'\x89PNG\r\n\x1a\n' or header[12:16]!=b'IHDR':
        raise PreviewError('invalid_preview_page')
    return struct.unpack('>II',header[16:24])

def prepare(file_id,format,*,source=INPUT,output=OUTPUT,limits=Limits()):
    if not re.fullmatch(r'file-[a-f0-9]{32}',file_id) or format not in FORMATS:raise PreviewError('invalid_preview_request')
    if not 1<=limits.pages<=40 or not 1<=limits.side<=1600 or limits.original_bytes>16*1024**2:
        raise PreviewError('invalid_preview_limits')
    source,output=Path(source),Path(output)
    if output.is_symlink() or not output.is_dir() or any(output.iterdir()):raise PreviewError('output_must_be_empty')
    initial=regular(source,limits.original_bytes);original_sha=checksum(source)
    work=Path(tempfile.mkdtemp(prefix='.document-',dir=output));deadline=time.monotonic()+limits.job_seconds
    try:
        doc=work/('original.'+format);shutil.copyfile(source,doc)
        if format in {'docx','xlsx'}:
            check_ooxml(doc,format)
            profile=work/'profile';(profile/'user').mkdir(parents=True)
            (profile/'user/registrymodifications.xcu').write_text('<?xml version="1.0"?><oor:items xmlns:oor="http://openoffice.org/2001/registry"><item oor:path="/org.openoffice.Office.Common/Security/Scripting"><prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop><prop oor:name="DisableMacrosExecution" oor:op="fuse"><value>true</value></prop></item></oor:items>')
            filter_name='writer_pdf_Export' if format=='docx' else 'calc_pdf_Export'
            run(['/usr/bin/libreoffice','--headless','--nologo','--nodefault','--nofirststartwizard','--norestore',
                '-env:UserInstallation='+profile.as_uri(),'--convert-to','pdf:'+filter_name,
                '--outdir',str(work),str(doc)],work,limits,deadline)
            pdf=work/'original.pdf'
        else:pdf=doc
        regular(pdf,64*1024**2)
        info=run(['/usr/bin/pdfinfo',str(pdf)],work,limits,deadline).decode('utf-8','replace')
        match=re.search(r'^Pages:\s+(\d+)\s*$',info,re.M)
        if not match or not 1<=int(match.group(1))<=limits.pages:raise PreviewError('document_page_limit')
        count=int(match.group(1));ready=work/'pages';ready.mkdir()
        pages=[]
        for page in range(1,count+1):
            prefix=ready/f'page-{page:04d}'
            run(['/usr/bin/pdftoppm','-f',str(page),'-l',str(page),'-singlefile','-scale-to',str(limits.side),
                '-png',str(pdf),str(prefix)],work,limits,deadline)
            path=prefix.with_suffix('.png');size=regular(path,limits.page_bytes).st_size;width,height=png_dimensions(path)
            if not 1<=width<=limits.side or not 1<=height<=limits.side:raise PreviewError('preview_dimensions_limit')
            pages.append({'page':page,'path':path.name,'width':width,'height':height,'size_bytes':size,'sha256':checksum(path)})
        extracted=work/'extracted.txt'
        run(['/usr/bin/pdftotext','-layout','-enc','UTF-8',str(pdf),str(extracted)],work,limits,deadline)
        if not extracted.is_file() or extracted.stat().st_size>16*1024**2:raise PreviewError('document_text_limit')
        with extracted.open('rb') as stream:text=stream.read(limits.text_bytes+1)
        truncated=len(text)>limits.text_bytes;text=text[:limits.text_bytes].decode('utf-8','ignore').replace('\x00','').encode()
        (ready/'text.txt').write_bytes(text)
        manifest={'schema_version':1,'profile':PROFILE,'recipe':RECIPE,'file_id':file_id,'format':format,
            'original_sha256':original_sha,'original_size_bytes':initial.st_size,'page_count':count,'pages':pages,
            'text':{'path':'text.txt','size_bytes':len(text),'sha256':hashlib.sha256(text).hexdigest(),'truncated':truncated},
            'renderer':'LibreOffice PDF export + Poppler' if format!='pdf' else 'Poppler'}
        (ready/'manifest.json').write_text(json.dumps(manifest,sort_keys=True,separators=(',',':')))
        if tree_bytes(ready)>limits.output_bytes:raise PreviewError('preview_output_limit')
        after=source.stat()
        if (after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns)!=(initial.st_dev,initial.st_ino,initial.st_size,initial.st_mtime_ns,initial.st_ctime_ns) or checksum(source)!=original_sha:
            raise PreviewError('document_changed')
        ready.rename(output/'preview');return manifest
    finally:shutil.rmtree(work,ignore_errors=True)

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--file-id',required=True);parser.add_argument('--format',required=True,choices=sorted(FORMATS));args=parser.parse_args()
    try:
        result=prepare(args.file_id,args.format)
        print(json.dumps({'status':'ready','page_count':result['page_count'],'manifest':'preview/manifest.json'}))
    except (PreviewError,OSError,ValueError) as error:
        print(json.dumps({'status':'failed','code':str(error) if isinstance(error,PreviewError) else 'document_conversion_failed'}));raise SystemExit(2)

if __name__=='__main__':main()

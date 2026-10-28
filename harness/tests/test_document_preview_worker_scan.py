"""Filesystem accounting only; this test never parses a document on the host."""
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).parents[1]))
import document_preview_worker as worker

def test_concurrent_converter_temporary_file_removal_is_safe(tmp_path,monkeypatch):
    path=tmp_path/'transient';path.write_bytes(b'temporary')
    original=Path.lstat
    def disappears(self,*args,**kwargs):
        if self==path:
            self.unlink();raise FileNotFoundError()
        return original(self,*args,**kwargs)
    monkeypatch.setattr(Path,'lstat',disappears)
    assert worker.tree_bytes(tmp_path)==0

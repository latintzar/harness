"""Keep short wire guards out of SQLite's last-connection WAL recovery race."""
import asyncio
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0,str(Path(__file__).parents[1]))
from custom_coordinator import CustomCoordinator,_Journal
from test_custom_execution import setup

@pytest.fixture
def anyio_backend():return 'asyncio'

@pytest.mark.parametrize('code',[sqlite3.SQLITE_BUSY_RECOVERY,sqlite3.SQLITE_BUSY_SNAPSHOT,sqlite3.SQLITE_LOCKED_SHAREDCACHE])
def test_extended_busy_is_indeterminate_not_revocation(tmp_path,monkeypatch,code):
 _,manager,key=setup(tmp_path/'company')
 lease=manager.begin_test(key,owner='alice',collection='shop',expected_revision=2)
 snapshot=manager.test_snapshot(lease,owner='alice',collection='shop')
 journal=_Journal(tmp_path/'journal')
 def locked(*args,**kwargs):
  error=sqlite3.OperationalError('database is locked');error.sqlite_errorcode=code;raise error
 monkeypatch.setattr(sqlite3,'connect',locked)
 try:
  assert journal.allows_current('batch')is None
  assert manager.authority_probe(snapshot,owner='alice',collection='shop')is None
  assert manager.authority_current(snapshot,owner='alice',collection='shop')is False
 finally:journal.close()

def test_journal_keeps_wal_open_across_wire_guards_and_observes_committed_stop(tmp_path):
 journal=_Journal(tmp_path/'journal')
 batch=SimpleNamespace(id='batch',key='connector',owner='alice',collection='shop',snapshot=SimpleNamespace(sha256='a'*64),kind='invoke',test_lease=None)
 journal.insert_batch(batch)
 wal=Path(str(journal.path)+'-wal')
 try:
  # A continuously open WAL prevents open/last-close from triggering recovery
  # under the zero-wait final guard. Keep no read transaction or stale snapshot.
  anchor=journal.anchor;identity=wal.stat().st_ino
  for n in range(10):
   journal.add_job('batch',str(n));journal.job_state(str(n),'running')
   assert wal.stat().st_ino==identity and journal.allows_current('batch')is True
  with journal.db()as writer:
   writer.execute('BEGIN IMMEDIATE')
   writer.execute("UPDATE batches SET state='stopping' WHERE id='batch'")
   assert journal.allows_current('batch')is True
  assert journal.allows_current('batch')is False
 finally:journal.close()
 with pytest.raises(sqlite3.ProgrammingError,match='closed'):
  anchor.execute('SELECT 1')
 journal.close()

@pytest.mark.anyio
@pytest.mark.parametrize('gateway_failed',[False,True])
async def test_coordinator_shutdown_closes_idle_wal_anchor_even_after_gateway_error(tmp_path,gateway_failed):
 _,manager,_=setup(tmp_path/'company')
 class Gateway:
  async def close(self):
   if gateway_failed:raise ValueError('synthetic failure')
 coordinator=CustomCoordinator(manager,Gateway(),object())
 anchor=coordinator.journal.anchor
 try:await coordinator.close()
 except ValueError:assert gateway_failed
 with pytest.raises(sqlite3.ProgrammingError,match='closed'):anchor.execute('SELECT 1')
 assert coordinator.journal.anchor is None

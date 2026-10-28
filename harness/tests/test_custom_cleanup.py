import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
import pytest
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parents[1]))
from test_custom_execution import setup,evidence,activate
from custom_execution import ExecutionError


def receipt(snapshot,job):
 return {'snapshot_sha256':snapshot.sha256,'cleanup_confirmed':True,'broker_run_id':'custom-'+job,'receipt_sha256':'e'*64}


def test_admitted_job_blocks_new_test_and_direct_activation_until_exact_cleanup(tmp_path):
 _,manager,key=setup(tmp_path)
 lease=manager.begin_test(key,owner='alice',collection='shop',expected_revision=2)
 snap=manager.test_snapshot(lease,owner='alice',collection='shop')
 manager.complete_test(lease,evidence(manager,lease),owner='alice',collection='shop')
 job='a'*32
 manager.admit_job(job,snap,owner='alice',collection='shop')
 assert manager.authority_current(snap,owner='alice',collection='shop')
 with pytest.raises(ExecutionError):manager.begin_test(key,owner='alice',collection='shop',expected_revision=2)
 with pytest.raises(ExecutionError):manager.activate(lease.id,owner='alice',collection='shop',expected_active_generation=0)
 manager.confirm_job_cleanup(job,receipt(snap,job),owner='alice',collection='shop')
 assert manager.activate(lease.id,owner='alice',collection='shop',expected_active_generation=0)['active_status']=='active'


def test_uncertain_active_invocation_closes_wire_and_active_lookup(tmp_path):
 _,manager,key=setup(tmp_path);activate(manager,key)
 snap=manager.active_snapshot(key,owner='alice',collection='shop');job='b'*32
 manager.admit_job(job,snap,owner='alice',collection='shop')
 manager.mark_job_uncertain(job,owner='alice',collection='shop')
 assert not manager.authority_current(snap,owner='alice',collection='shop')
 with pytest.raises(ExecutionError):manager.active_snapshot(key,owner='alice',collection='shop')
 manager.confirm_job_cleanup(job,receipt(snap,job),owner='alice',collection='shop')
 assert manager.active_snapshot(key,owner='alice',collection='shop')==snap


def test_cleanup_survives_revocation_and_is_owner_job_snapshot_bound(tmp_path):
 _,manager,key=setup(tmp_path);snap=manager.snapshot(key,owner='alice',collection='shop',expected_revision=2);job='c'*32
 manager.admit_job(job,snap,owner='alice',collection='shop');manager.revoke(key,owner='alice',collection='shop')
 manager.mark_job_uncertain(job,owner='alice',collection='shop')
 for owner,collection,bad in [('bob','shop',receipt(snap,job)),('alice','elsewhere',receipt(snap,job)),('alice','shop',{**receipt(snap,job),'snapshot_sha256':'0'*64}),('alice','shop',{**receipt(snap,job),'cleanup_confirmed':False})]:
  with pytest.raises(ExecutionError):manager.confirm_job_cleanup(job,bad,owner=owner,collection=collection)
 manager.confirm_job_cleanup(job,receipt(snap,job),owner='alice',collection='shop')
 manager.confirm_job_cleanup(job,receipt(snap,job),owner='alice',collection='shop')
 with pytest.raises(ExecutionError):manager.admit_job(job,snap,owner='alice',collection='shop')


def test_stop_while_completion_waits_for_database_never_passes(tmp_path):
 store,manager,key=setup(tmp_path);lease=manager.begin_test(key,owner='alice',collection='shop',expected_revision=2);proof=evidence(manager,lease)
 cancelled=threading.Event();entered=threading.Event()
 with store.db()as db,ThreadPoolExecutor()as pool:
  db.execute('BEGIN IMMEDIATE')
  def complete():
   entered.set();return manager.complete_test(lease,proof,owner='alice',collection='shop',current=lambda:not cancelled.is_set())
  task=pool.submit(complete);assert entered.wait(1);cancelled.set();db.commit()
  with pytest.raises(ExecutionError):task.result(3)
 assert manager.test_result(lease.id,owner='alice',collection='shop')['status']=='pending'

# Saved-state migration

[migrate_state.py](../migrate_state.py) exports existing local thread history through `initialize` and `thread/read`, then verifies and installs scoped private snapshots. [history.py](history.py) retains the original public messages, ownership and image provenance. [app_server.py](app_server.py) owns the historical JSON-RPC transport used by that explicit operator command.

The application runs through [the managed adapter](../../runtime/README.md). This directory is excluded from application artifacts. Historical local-runtime proofs temporarily import this transport by its migration path while their mixed mechanics tests move to managed fixtures; their original evidence remains under the deployment records and Git history.

The extraction retains the original transport and export semantics. It starts no model turn and changes no installed broker or protected worker bytes. Migration itself follows [the operator runbook](../../../docs/HISTORICAL-RECORDS.md#record-5).

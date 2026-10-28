# Historical execution adapters

These seven Rocoto adapters preserve the local app-server and self-hosted executor implementations for their original tests and operator proofs. OpenAI's stock core is external software; this directory contains Rocoto adapter code.

| Previous path under `harness/runtime/` | Retained path in this directory |
|---|---|
| `isolated_turn.py` | `isolated_turn.py` |
| `isolated_native.py` | `isolated_native.py` |
| `native_thread_tools.py` | `native_thread_tools.py` |
| `native_policy.py` | `native_policy.py` |
| `native_files.py` | `native_files.py` |
| `native_api_smoke.py` | `native_api_smoke.py` |
| `managed_executor.py` | `managed_executor.py` |

Historical callers import `deploy.migration.legacy.<module>` after the ordinary harness layout bootstrap. The package installs no import aliases or paths. The existing proof drivers retain their names, assertions and execution controls; the bridge/provider proof hash lookups now name this directory explicitly. Dated receipts continue describing the source they actually verified.

The active runtime uses the OpenAI-managed Agents API. `build_preview.artifact_files` selects `runtime/`, `mechanics/`, `sdk/` and the active vertical, so this deployment-only directory is excluded from both bare and retail artifacts. `test_legacy_runtime_layout.py` verifies that exclusion, explicit legacy imports and clean managed imports. Existing historical integration fixtures may still target behavior retired from the shared bridge; this relocation preserves those fixtures rather than changing their expectations.

## Rollback

The source checkpoint immediately before relocation is `eaa6547a176b0c572bb1092b5d52aaab3d95a116`. Revert the dedicated relocation commit through the repository's ordinary PR workflow to restore the seven paths and their original imports. For a checkout-only rollback, reverse the seven `git mv` operations in the table, reverse only the qualified historical imports/monkeypatch paths and the two proof lookup lists, and remove the newly added package marker and relocation test/documentation. Preserve unrelated changes made after the checkpoint.

The relocation changes source paths only. Installed releases, service state, broker images, the eight commissioned helpers and provider sessions retain their existing custody. Publishing a new artifact remains a separate operation.

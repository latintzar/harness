# Local operator tools

`authorize_local_budget.py` writes an explicit development allowance through the existing company-bound `CompanyUsage.native_agent_pool` and `import_operator_budget` interfaces. It performs zero provider requests. It records an operator authorization, separately from real deposits and provider trial credit.

Use a fresh state directory outside the checkout, owned by your Unix user with mode 0700. The command requires acknowledgement of your own provider billing and accepts at most $20 with a 1–30 day expiry. Existing immutable funding, reservation, membership, model and scope checks remain active. A previously commissioned pool uses its existing reconciliation/renewal interfaces instead of changing funding sources.

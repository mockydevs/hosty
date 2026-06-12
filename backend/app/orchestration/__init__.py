"""v2 orchestration (ADR-013): the reconciler is the ONLY writer to the
host. API routes write desired state to the DB and ask for convergence;
everything that touches the machine flows through planner -> executor."""

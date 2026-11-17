"""Student-agent simulation harness (truth sheets + intents)."""
__all__ = ["run_session", "run_batch", "summarize_metrics"]

def __getattr__(name):
    if name in ("run_session", "run_batch"):
        from sim import runner
        return getattr(runner, name)
    if name == "summarize_metrics":
        from sim.metrics import summarize_metrics
        return summarize_metrics
    raise AttributeError(name)

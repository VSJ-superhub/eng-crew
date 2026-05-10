"""Human-in-the-loop gate for eng-crew."""
import os, sys, json, threading, time
_events = {}; _decisions = {}
def dashboard_prompt(subtasks, run_id, **_):
    from .tracker import update_run_status, get_hitl_decision, clear_hitl_decision
    ev = threading.Event(); _events[run_id] = ev
    update_run_status(run_id, "awaiting_approval")
    deadline = time.monotonic() + 18000; decision = None
    while time.monotonic() < deadline:
        if ev.wait(timeout=2): decision = _decisions.pop(run_id, None); break
        decision = get_hitl_decision(run_id)
        if decision: clear_hitl_decision(run_id); break
    _events.pop(run_id, None); _decisions.pop(run_id, None)
    if not decision: return subtasks, False, None
    if decision.get("approved"): update_run_status(run_id, "running")
    return subtasks, decision["approved"], decision.get("feedback")
def prompt_user(subtasks, run_id, ci_mode=False):
    if ci_mode: return subtasks, True, None
    print("Approve plan? [y/n/f]"); resp = input("> ").lower()
    if resp == "y": return subtasks, True, None
    return subtasks, False, None

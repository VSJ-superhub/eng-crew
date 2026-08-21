---
name: repair-verification-failure-resumed
description: Fix a change that failed the verification gate, resuming the session that made it. Omits the task restatement because that context is already present.
---
Verification failed on the change you just made. Fix it.

=== VERIFICATION FAILURES ===
{failures}

Fix the cause of these failures. The failure may be in the implementation or in a test the
change made stale, so decide which is actually wrong rather than forcing either to match the
other. Do not delete, skip, or weaken a test to make it pass. Re-run the failing command
yourself to confirm the fix before you finish.

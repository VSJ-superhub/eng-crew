---
name: resolve-merge-conflicts
description: Resolve the conflicts left by merging a ticket's done prerequisites onto HEAD, so the ticket can be built on their work. Used by eng_crew.resolve.
---
You are in a git worktree in the middle of a merge. The branch `{ref}` holds
work that is already done and reviewed, and merging it onto the project's
current HEAD stopped on conflicts. Your only job is to finish this merge so
that both sides' work survives.

## Why the merge is happening
Ticket #{ticket_id} ("{title}") is waiting to be built on top of this work:

{prerequisites}

## The conflicted files
{conflicted}

Resolve every conflict:

- Read both sides of each conflict, and whatever surrounds it, before editing.
  Both sides are finished work. The right answer almost always keeps both —
  two imports, both routers registered, both fields — rather than picking one.
- Where they genuinely contradict, keep HEAD's behaviour and adapt the branch's
  code to it, since HEAD is what has moved on since the branch was cut.
- Remove every conflict marker (`<<<<<<<`, `=======`, `>>>>>>>`).
- Change nothing that is not needed to make the merge correct. This is not the
  place to build ticket #{ticket_id} or to improve either side.
- Run the project's tests and build, and fix anything the merge broke.
- `git add` each file you resolved. Do **not** commit: the merge commit is made
  for you once the result has been checked.

When you finish, say in two or three sentences what each conflict was and how
you resolved it.

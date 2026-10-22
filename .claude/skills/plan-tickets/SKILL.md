---
name: plan-tickets
description: Turn a project goal into a queue of tickets with acceptance criteria. Used by eng_crew.planning when a project is planned.
---
You are a senior engineering manager. Turn the GOAL below into a queue of tickets.

A ticket is one unit of work a single engineer can finish end to end. It is not a
phase, not a milestone, and not "everything about authentication". If a ticket
would take more than about a day, split it.

PROJECT CONTEXT:
{project_context}

GOAL:
{goal}

Rules:
- Every ticket needs acceptance criteria: concrete, checkable statements of what
  must be true when it is done. A ticket without them cannot be worked on, so do
  not emit one.
- Order the tickets so that a ticket only depends on work earlier in the list.
  `priority` is the queue position: 10 is pulled before 20.
- Say what a ticket actually waits on in `depends_on`: the 1-based positions of
  the tickets in this list that must be finished before it can start (the first
  ticket is 1). Omit it, or leave it empty, when a ticket can be built on its
  own — a ticket is only held back by a real prerequisite, and a dependency
  that is not needed stalls the queue. Dependencies must not form a cycle.
- `type` is one of: feature, bug, chore.
- Decide up front on a small set of **areas** — the parts of this product work
  belongs to, three to six of them, named from the project context (e.g.
  "Engine", "Website", "Admin", "Infra"). List them in `areas`, and give every
  ticket an `area` from that list, spelled exactly as it appears there. Areas
  are the roadmap's columns: a new name per ticket makes the roadmap useless.
- Write the description as the instruction the engineer will actually read.

Respond with ONLY valid JSON in this exact format — no prose, no markdown fences:
{
  "areas": ["Engine", "Website"],
  "tickets": [
    {
      "title": "Short imperative summary",
      "description": "What to build and where, in a couple of sentences.",
      "acceptance_criteria": "- the first checkable statement\n- the second",
      "type": "feature",
      "area": "Engine",
      "priority": 10,
      "depends_on": []
    }
  ]
}

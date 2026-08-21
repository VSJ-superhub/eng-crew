---
name: architect-decompose
description: Decompose a task into discrete, parallelizable subtasks as JSON. Used by ArchitectAgent on the full multi-agent path.
---
You are a senior software architect. Decompose the following task into discrete, parallelizable subtasks.

PROJECT CONTEXT:
{project_context}

TASK:
{raw_task}

Respond with ONLY valid JSON in this exact format — no prose, no markdown fences:
{
  "subtasks": [
    {
      "id": "s1",
      "description": "...",
      "target_files": ["path/to/file.py"],
      "agent_type": "backend",
      "dependencies": []
    }
  ]
}

agent_type must be one of: architect, critic, backend, frontend, database, ai_pipeline, infrastructure, generic.
dependencies is a list of subtask ids that must complete before this one starts.
target_files contains paths relative to the project root that this subtask will create or modify.

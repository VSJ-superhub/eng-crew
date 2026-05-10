import asyncio
import re
from pathlib import Path
from fastapi import APIRouter, Body, Request, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel
from typing import List, Optional
import json
import os
OLLAMA_SUMMARIZER_MODEL = os.environ.get("ENG_CREW_LOCAL_LLM_MODEL", "")

_RUST_BRIDGE = Path(os.environ.get("ENG_CREW_OLLAMA_BRIDGE", "ollama_stream"))

router = APIRouter(prefix="/api/intake", tags=["intake"])

_INTAKE_SYSTEM = """\
You are a senior engineering team lead helping scope a software task.
The project context is provided below — do NOT ask the user which project they are working on.
Your job is to ask focused clarifying questions about what they want to build,
identify which systems will be affected, and help them arrive at a clear task description.

Rules:
- The project is already selected — never ask which project or repo
- Ask 1-2 questions at a time, not a list of 10
- Be concise — this is a planning chat, not a doc
- When the user seems ready, summarize the task in 2-3 sentences
- Do NOT write any code
- Do NOT ask about timelines or estimates
"""

_CLAUDE_MD_SYSTEM = """\
You are a senior staff engineer generating an ARCHITECTURE.md file for a software project.
ARCHITECTURE.md is read by an AI engineering team before every task — it must be specific,
complete, and actionable.

You will be given the ARCHITECTURE_TEMPLATE.md that defines the required sections and format.
Follow the template exactly — keep every section heading, fill every field with real project
details, write "TBD" where information is unknown (never omit a section).
Remove all comment lines (lines starting with #) from the output.
Output ONLY the markdown content — no commentary, no code fences around the whole file.
"""

_ARCHITECTURE_TEMPLATE_PATH = Path(os.environ.get(
    "ENG_CREW_ARCHITECTURE_TEMPLATE",
    str(Path(__file__).parent.parent.parent.parent / "ARCHITECTURE_TEMPLATE.md"),
))

def _load_architecture_template() -> str:
    try:
        if _ARCHITECTURE_TEMPLATE_PATH.exists():
            return _ARCHITECTURE_TEMPLATE_PATH.read_text(encoding="utf-8", errors="replace")
    except Exception:
        pass
    return ""

class IntakeChatMessage(BaseModel):
    message: str
    history: List[dict] = []
    project_name: Optional[str] = None
    project_description: Optional[str] = None
    tech_stack: Optional[List[str]] = []

class ExtractPayload(BaseModel):
    history: List[dict]

class GenerateClaudeMdPayload(BaseModel):
    project_name: str
    project_path: str
    tech_stack: Optional[List[str]] = []
    description: Optional[str] = ""
    notes: Optional[str] = ""

class ParseMarkdownPayload(BaseModel):
    content: str
    project_id: Optional[int] = None

class GenerateArchitecturePayload(BaseModel):
    content: str
    project_name: Optional[str] = None
    project_path: Optional[str] = None

class SaveArchitecturePayload(BaseModel):
    project_path: str
    content: str

def _build_intake_prompt(system: str, history: List[dict], message: str) -> str:
    parts = [system]
    if history:
        parts.append("\n=== CONVERSATION SO FAR ===")
        for m in history:
            role = "USER" if m["role"] == "user" else "ASSISTANT"
            parts.append(f"{role}: {m['content']}")
    parts.append(f"\n=== NEW USER MESSAGE ===\n{message}")
    parts.append("\nRespond as the engineering team lead. Be concise.")
    return "\n".join(parts)

async def _run_claude_streaming(prompt: str):
    cmd = [
        "claude", "-p", prompt,
        "--allowedTools", "none",
        "--output-format", "stream-json",
        "--model", "claude-haiku-4-5-20251001",
        "--max-turns", "1",
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        async for raw in proc.stdout:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "assistant":
                for block in event.get("message", {}).get("content", []):
                    if block.get("type") == "text":
                        yield f"data: {json.dumps({'text': block['text']})}\n\n"
            elif event.get("type") == "content_block_delta":
                delta = event.get("delta", {})
                if delta.get("type") == "text_delta":
                    yield f"data: {json.dumps({'text': delta['text']})}\n\n"
        await proc.wait()
        if proc.returncode != 0:
            stderr = await proc.stderr.read()
            err = stderr.decode("utf-8", errors="replace")[:300]
            yield f"data: {json.dumps({'error': err})}\n\n"
    except FileNotFoundError:
        yield f"data: {json.dumps({'error': 'claude CLI not found — is it installed?'})}\n\n"
    except Exception as e:
        yield f"data: {json.dumps({'error': str(e)})}\n\n"
    yield "data: [DONE]\n\n"

async def _run_ollama_streaming(model: str, prompt: str):
    try:
        proc = await asyncio.create_subprocess_exec(
            str(_RUST_BRIDGE), model, "-",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        proc.stdin.write(prompt.encode("utf-8"))
        await proc.stdin.drain()
        proc.stdin.close()
        async for raw in proc.stdout:
            chunk = raw.decode("utf-8", errors="replace").rstrip("\n")
            if chunk:
                yield f"data: {json.dumps({'text': chunk})}\n\n"
        await proc.wait()
        if proc.returncode != 0:
            stderr = await proc.stderr.read()
            err = stderr.decode("utf-8", errors="replace")[:300]
            yield f"data: {json.dumps({'error': err})}\n\n"
    except FileNotFoundError:
        yield f"data: {json.dumps({'error': 'ollama_stream bridge not found'})}\n\n"
    except Exception as e:
        yield f"data: {json.dumps({'error': str(e)})}\n\n"
    yield "data: [DONE]\n\n"

async def _run_ollama_collect(model: str, prompt: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        str(_RUST_BRIDGE), model, "-",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    proc.stdin.write(prompt.encode("utf-8"))
    await proc.stdin.drain()
    proc.stdin.close()
    stdout, _ = await proc.communicate()
    return stdout.decode("utf-8", errors="replace")

@router.post("/chat")
async def api_intake_chat(payload: IntakeChatMessage):
    system = _INTAKE_SYSTEM
    if payload.project_name:
        system += f"\n\nProject: {payload.project_name}"
    if payload.project_description:
        system += f"\nDescription: {payload.project_description}"
    if payload.tech_stack:
        system += f"\nTech stack: {', '.join(payload.tech_stack)}"

    prompt = _build_intake_prompt(system, payload.history, payload.message)

    return StreamingResponse(
        _run_claude_streaming(prompt),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

@router.post("/generate-claude-md")
async def api_intake_generate_claude_md(payload: GenerateClaudeMdPayload):
    template = _load_architecture_template()
    system = _CLAUDE_MD_SYSTEM
    if template:
        system += f"\n\n=== ARCHITECTURE_TEMPLATE.md (follow this structure exactly) ===\n{template}"
    
    parts = [system, f"\nProject name: {payload.project_name}"]
    if payload.tech_stack:
        parts.append(f"Tech stack: {', '.join(payload.tech_stack)}")
    if payload.description:
        parts.append(f"Description: {payload.description}")
    if payload.notes:
        parts.append(f"Additional context from the developer:\n{payload.notes}")

    # Scan actual files for extra context
    try:
        root = Path(payload.project_path)
        structure_hints = []
        for name in ["README.md", "pyproject.toml", "package.json", "requirements.txt"]:
            fp = root / name
            if fp.exists():
                content = fp.read_text(encoding="utf-8", errors="replace")[:800]
                structure_hints.append(f"\n--- {name} (excerpt) ---\n{content}")
        if structure_hints:
            parts.append("\nExisting project files (for context):" + "".join(structure_hints))
    except Exception:
        pass

    prompt = "\n".join(parts)
    return StreamingResponse(
        _run_claude_streaming(prompt),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

@router.post("/extract")
async def api_intake_extract(payload: ExtractPayload):
    history_text = "\n".join(
        f"{m['role'].upper()}: {m['content']}" for m in payload.history
    )
    prompt = (
        f"Given this planning conversation, extract the task as JSON.\n\n"
        f"{history_text}\n\n"
        f'Output ONLY valid JSON with no markdown: {{"title": "short task name", '
        f'"description": "clear description of what needs to be built", '
        f'"tech_stack": ["list", "of", "technologies"]}}'
    )
    try:
        if OLLAMA_SUMMARIZER_MODEL:
            text = await _run_ollama_collect(OLLAMA_SUMMARIZER_MODEL, prompt)
        else:
            cmd = [
                "claude", "-p", prompt,
                "--allowedTools", "none",
                "--output-format", "json",
                "--model", "claude-haiku-4-5-20251001",
                "--max-turns", "1",
            ]
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            text = ""
            if proc.returncode == 0:
                try:
                    outer = json.loads(stdout.decode("utf-8", errors="replace"))
                    text = outer.get("result", outer.get("content", "")) or ""
                except Exception:
                    text = stdout.decode("utf-8", errors="replace")

        start, end = text.find("{"), text.rfind("}") + 1
        data = json.loads(text[start:end])
    except Exception:
        last_user = next(
            (m["content"] for m in reversed(payload.history) if m["role"] == "user"), ""
        )
        data = {"title": last_user[:80], "description": last_user, "tech_stack": []}

    return JSONResponse(data)

@router.post("/parse-markdown")
async def api_intake_parse_markdown(payload: ParseMarkdownPayload):
    prompt = (
        "You are a software project planner. The user has provided a markdown planning document. "
        "Extract a flat list of actionable development tasks from it.\n\n"
        "Rules:\n"
        "- Each task should be a concrete unit of work (feature, fix, migration, etc.)\n"
        "- Title should be short and clear (under 80 chars)\n"
        "- Description should add detail if the markdown provides it, otherwise leave empty\n"
        "- Output ONLY valid JSON, no markdown fences\n\n"
        "JSON structure: {\"tasks\": [{\"title\": \"...\", \"description\": \"...\"}], \"summary\": \"...\"}\n\n"
        f"=== DOCUMENT ===\n{payload.content}"
    )
    try:
        if OLLAMA_SUMMARIZER_MODEL:
            text = await _run_ollama_collect(OLLAMA_SUMMARIZER_MODEL, prompt)
        else:
            cmd = [
                "claude", "-p", prompt,
                "--allowedTools", "none",
                "--output-format", "json",
                "--model", "claude-haiku-4-5-20251001",
                "--max-turns", "1",
            ]
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            text = ""
            if proc.returncode == 0:
                try:
                    outer = json.loads(stdout.decode("utf-8", errors="replace"))
                    text = outer.get("result", outer.get("content", "")) or ""
                except Exception:
                    text = stdout.decode("utf-8", errors="replace")

        start, end = text.find("{"), text.rfind("}") + 1
        data = json.loads(text[start:end])
        return JSONResponse(data)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@router.post("/generate-architecture")
async def api_intake_generate_architecture(payload: GenerateArchitecturePayload):
    template = _load_architecture_template()
    parts = [
        "You are a senior staff engineer. The user has provided a planning or reference document "
        "(business plan, README, PRD, notes, etc.). Your job is to produce a complete ARCHITECTURE.md "
        "for their software project based on that document.",
        "",
        "Rules:",
        "- Follow the ARCHITECTURE_TEMPLATE.md structure exactly — same section headings, same order.",
        "- Extract every concrete technical detail you can find in the source document.",
        "- Where a section cannot be filled from the source, write 'TBD' — never omit a section.",
        "- Remove all comment lines (lines starting with #) from the template before outputting.",
        "- Output ONLY the markdown content — no preamble, no code fences around the whole file.",
    ]
    if payload.project_name:
        parts += ["", f"Project name: {payload.project_name}"]
    if payload.project_path:
        parts += [f"Project path: {payload.project_path}"]
    parts += ["", "=== SOURCE DOCUMENT ===", payload.content]

    prompt = "\n".join(parts)
    arch_model = OLLAMA_SUMMARIZER_MODEL
    generator = (
        _run_ollama_streaming(arch_model, prompt)
        if arch_model
        else _run_claude_streaming(prompt)
    )
    return StreamingResponse(
        generator,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

@router.post("/save-architecture")
async def api_intake_save_architecture(payload: SaveArchitecturePayload):
    if not os.path.isdir(payload.project_path):
        return JSONResponse({"error": f"Project path not found: {payload.project_path}"}, status_code=400)
    dest = Path(payload.project_path) / "ARCHITECTURE.md"
    try:
        dest.write_text(payload.content, encoding="utf-8")
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    return JSONResponse({"ok": True, "path": str(dest)})

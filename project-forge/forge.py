"""Project Forge — KG theme extraction + Claude project synthesis.

Three-stage pipeline:
  1. extract_themes()  — query Bonfires KG, cluster into themes
  2. synthesize()      — Claude generates project ideas from selected themes
  3. generate_mockup() — Claude generates an HTML wireframe for a project

Supports two Claude backends:
  - Claude Code SDK (local dev): requires `claude` CLI installed and authenticated
  - Anthropic API (deployment): requires ANTHROPIC_API_KEY environment variable
"""

import asyncio
import json
import os
import shutil
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from pathlib import Path

# Allow Claude Code SDK to run even when called from within a Claude Code session
os.environ.pop("CLAUDECODE", None)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
DELVE_API_KEY = os.environ.get("DELVE_API_KEY", "8n5l-sJnrHjywrTnJ3rJCjo1f1uLyTPYy_yLgq_bf-d")
BONFIRE_ID = os.environ.get("BONFIRE_ID", "698b70002849d936f4259848")
BASE_URL = os.environ.get("DELVE_BASE_URL", "https://tnt-v2.api.bonfires.ai")

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-5-20250929")

# Use SDK if claude CLI is available AND no API key is explicitly set
USE_SDK = shutil.which("claude") is not None and not ANTHROPIC_API_KEY

# ---------------------------------------------------------------------------
# KG client
# ---------------------------------------------------------------------------

def delve(query: str, num_results: int = 20) -> dict:
    """Synchronous /delve call."""
    payload = json.dumps({
        "query": query,
        "bonfire_id": BONFIRE_ID,
        "num_results": num_results,
    }).encode()
    req = urllib.request.Request(
        f"{BASE_URL}/delve",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {DELVE_API_KEY}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())

# ---------------------------------------------------------------------------
# Stage 1: Theme extraction
# ---------------------------------------------------------------------------

THEME_QUERIES = [
    "themes and big ideas discussed across talks and conversations",
    "problems people want to solve and challenges identified",
    "projects tools and platforms being built or proposed",
    "coordination infrastructure public goods and governance",
    "local currency community economics and regenerative systems",
    "AI agents autonomy collaboration and agentic systems",
    "knowledge graphs collective intelligence and sensemaking",
]


@dataclass
class Theme:
    name: str
    description: str
    entities: list[str] = field(default_factory=list)
    episodes: list[str] = field(default_factory=list)
    edge_types: list[str] = field(default_factory=list)
    strength: int = 0  # how many queries surfaced this cluster


def extract_themes() -> dict:
    """Query the KG across multiple angles and return raw material for synthesis.

    Returns a dict with:
      - episodes: list of {name, content_preview}
      - entities: list of {name, uuid}
      - edges: list of {name, source, target}
      - raw_themes: the query labels we used
    """
    all_episodes = {}  # uuid -> {name, content}
    all_entities = {}  # uuid -> name
    all_edges = []
    seen_edges = set()

    for q in THEME_QUERIES:
        try:
            data = delve(q, num_results=20)
        except Exception as e:
            print(f"  [warn] query failed: {q[:40]}... — {e}")
            continue

        for ep in data.get("episodes", []):
            uuid = ep.get("uuid", "")
            if uuid and uuid not in all_episodes:
                content = ep.get("content", "")
                # Parse JSON-wrapped content
                if isinstance(content, str) and content.startswith("{"):
                    try:
                        parsed = json.loads(content)
                        content = parsed.get("content", content)
                    except json.JSONDecodeError:
                        pass
                all_episodes[uuid] = {
                    "name": ep.get("name", ""),
                    "content": str(content)[:500],
                }

        for ent in data.get("entities", []):
            uuid = ent.get("uuid", "")
            name = ent.get("name", "")
            if uuid and uuid not in all_entities:
                all_entities[uuid] = name

        for edge in data.get("edges", []):
            key = (
                edge.get("source_uuid", ""),
                edge.get("target_uuid", ""),
                edge.get("name", ""),
            )
            if key not in seen_edges:
                seen_edges.add(key)
                all_edges.append({
                    "name": edge.get("name", ""),
                    "source_uuid": edge.get("source_uuid", ""),
                    "target_uuid": edge.get("target_uuid", ""),
                })

    return {
        "episodes": [
            {"name": v["name"], "content_preview": v["content"]}
            for v in all_episodes.values()
            if v["name"]
        ],
        "entities": [
            {"name": name, "uuid": uuid}
            for uuid, name in all_entities.items()
            if name
        ],
        "edges": all_edges[:100],  # cap for prompt size
        "query_count": len(THEME_QUERIES),
        "episode_count": len(all_episodes),
        "entity_count": len(all_entities),
        "edge_count": len(all_edges),
    }


# ---------------------------------------------------------------------------
# Claude helper — supports both SDK and direct API
# ---------------------------------------------------------------------------

def _call_anthropic_api(prompt: str, max_tokens: int = 8192) -> str:
    """Call the Anthropic Messages API directly. Used for deployment."""
    payload = json.dumps({
        "model": CLAUDE_MODEL,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read())
    for block in data.get("content", []):
        if block.get("type") == "text":
            return block["text"]
    return ""


async def _call_claude(prompt: str) -> str:
    """Call Claude via SDK or API depending on environment."""
    if USE_SDK:
        from claude_code_sdk import query, ClaudeCodeOptions, ResultMessage
        result = ""
        async for message in query(
            prompt=prompt,
            options=ClaudeCodeOptions(max_turns=3, allowed_tools=[]),
        ):
            if isinstance(message, ResultMessage) and message.result:
                result = message.result
        return result
    else:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _call_anthropic_api, prompt)


# ---------------------------------------------------------------------------
# Stage 2: Project synthesis
# ---------------------------------------------------------------------------

PROJECT_SCHEMA = {
    "type": "object",
    "properties": {
        "projects": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Creative project name"},
                    "tagline": {"type": "string", "description": "One-line hook, under 15 words"},
                    "description": {"type": "string", "description": "2-3 paragraph description of what this project does and why it matters"},
                    "themes": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Which KG themes this draws from",
                    },
                    "tech_stack": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Key technologies involved",
                    },
                    "complexity": {
                        "type": "string",
                        "enum": ["weekend", "month", "quarter"],
                        "description": "How long to build an MVP",
                    },
                    "key_insight": {"type": "string", "description": "The novel connection or insight that makes this project interesting"},
                    "first_step": {"type": "string", "description": "What you'd build in the first 4 hours"},
                },
                "required": ["name", "tagline", "description", "themes", "tech_stack", "complexity", "key_insight", "first_step"],
            },
        }
    },
    "required": ["projects"],
}


async def synthesize_projects(themes_data: dict, selected_themes: list[str] | None = None) -> dict:
    """Generate project ideas from KG themes.

    Args:
        themes_data: output of extract_themes()
        selected_themes: optional filter — if provided, only use episodes/entities
                        matching these theme keywords

    Returns:
        Parsed JSON matching PROJECT_SCHEMA
    """
    # Build a condensed prompt with the KG material
    episode_summaries = "\n".join(
        f"- {ep['name']}: {ep['content_preview'][:200]}"
        for ep in themes_data["episodes"][:40]
    )
    entity_names = ", ".join(
        ent["name"] for ent in themes_data["entities"][:60]
    )
    edge_summary = "\n".join(
        f"- {e['name']}" for e in themes_data["edges"][:30]
    )

    theme_filter = ""
    if selected_themes:
        theme_filter = f"\nThe user is particularly interested in these themes: {', '.join(selected_themes)}. Prioritize projects that connect these themes in novel ways.\n"

    prompt = f"""You are Project Forge — a creative engine that synthesizes themes from a collective intelligence knowledge graph into novel, buildable project ideas.

Here is material from the EthBoulder 2026 knowledge graph (a weekend hackathon/unconference about Ethereum, public goods, AI agents, and regenerative systems in Boulder, Colorado):

## Episodes (conversations and events captured)
{episode_summaries}

## Key Entities (people, orgs, concepts)
{entity_names}

## Relationships
{edge_summary}
{theme_filter}
Generate 5 creative, ambitious but buildable project ideas. Each should:
1. Cross-pollinate 2-3 themes from the KG in a surprising way
2. Be technically specific (not vague platitudes)
3. Have a clear "what you'd build first" path
4. Connect to real people/orgs/tools mentioned in the KG
5. Range from weekend hacks to quarter-long builds

Be creative. Find non-obvious connections. The best ideas combine things nobody thought to combine.

Return ONLY valid JSON — an object with a "projects" array. Each project must have: name, tagline, description, themes (array), tech_stack (array), complexity ("weekend" or "month" or "quarter"), key_insight, first_step. No markdown fences, no explanation, just raw JSON."""

    text = await _call_claude(prompt)
    text = text.strip()

    # Strip markdown fences if present
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        text = text.rsplit("```", 1)[0]
    text = text.strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Try to find JSON in the response
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            try:
                return json.loads(text[start:end])
            except json.JSONDecodeError:
                return {"projects": [], "error": "Could not parse JSON from result"}
        return {"projects": [], "error": "No JSON found in result"}


# ---------------------------------------------------------------------------
# Stage 3: Mockup generation
# ---------------------------------------------------------------------------

async def generate_mockup(project: dict) -> str:
    """Generate an HTML wireframe mockup for a project.

    Args:
        project: a single project dict from synthesize_projects()

    Returns:
        HTML string of the wireframe mockup
    """
    prompt = f"""Generate a single-page HTML wireframe/mockup for this project:

**{project['name']}**
{project['tagline']}

{project['description']}

Tech stack: {', '.join(project.get('tech_stack', []))}
First step: {project.get('first_step', '')}

Requirements for the mockup:
- Single self-contained HTML file with all CSS inline
- Show the main UI screens/sections as a scrollable page
- Use a modern, clean design with good typography
- Include placeholder content that feels real (not lorem ipsum)
- Show key interactions as static states (e.g., "before click" and "after click" sections)
- Use a color scheme that feels appropriate for the project
- Add annotations/callouts explaining key UI elements (small gray text)
- Make it look like a polished design prototype, not a wireframe sketch
- Include a header with the project name and a brief description
- Mobile-friendly / responsive

Return ONLY the complete HTML — no markdown fences, no explanation, just the raw HTML starting with <!DOCTYPE html>."""

    html_result = await _call_claude(prompt)
    # Clean up in case it's wrapped in markdown fences
    if "```html" in html_result:
        html_result = html_result.split("```html", 1)[1]
        html_result = html_result.rsplit("```", 1)[0]
    elif "```" in html_result:
        html_result = html_result.split("```", 1)[1]
        html_result = html_result.rsplit("```", 1)[0]
    return html_result.strip()


# ---------------------------------------------------------------------------
# Stage 4: Full scaffold
# ---------------------------------------------------------------------------

async def scaffold_project(project: dict, output_dir: str) -> list[dict]:
    """Scaffold a full project directory.

    In SDK mode, uses Claude Code SDK with file tools to write files directly.
    In API mode, asks Claude for file contents and writes them ourselves.

    Args:
        project: a single project dict
        output_dir: where to write the files

    Returns:
        List of {tool, path} for each file written
    """
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    if USE_SDK:
        return await _scaffold_with_sdk(project, output_dir)
    else:
        return await _scaffold_with_api(project, output_dir)


async def _scaffold_with_sdk(project: dict, output_dir: str) -> list[dict]:
    """Scaffold using Claude Code SDK (has file write tools)."""
    from claude_code_sdk import query, ClaudeCodeOptions, AssistantMessage, ToolUseBlock

    prompt = f"""Create a project scaffold in {output_dir} for:

**{project['name']}**
{project['tagline']}

{project['description']}

Tech stack: {', '.join(project.get('tech_stack', []))}
Key insight: {project.get('key_insight', '')}
First step: {project.get('first_step', '')}

Create:
1. README.md — project overview, getting started, architecture
2. A main application file appropriate for the tech stack
3. A configuration file if needed
4. A simple test or example that proves the core concept works
5. Any supporting files the project needs

Keep it minimal but functional. This should be a real starting point someone can build from, not a toy demo. Focus on the core insight."""

    files_written = []
    async for message in query(
        prompt=prompt,
        options=ClaudeCodeOptions(
            max_turns=20,
            allowed_tools=["Read", "Write", "Edit", "Bash"],
            permission_mode="acceptEdits",
            cwd=output_dir,
        ),
    ):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, ToolUseBlock) and block.name in ("Write", "Edit"):
                    path = block.input.get("file_path", block.input.get("path", ""))
                    files_written.append({"tool": block.name, "path": path})

    return files_written


async def _scaffold_with_api(project: dict, output_dir: str) -> list[dict]:
    """Scaffold using Anthropic API (no file tools — we write files ourselves)."""
    prompt = f"""Generate a project scaffold for:

**{project['name']}**
{project['tagline']}

{project['description']}

Tech stack: {', '.join(project.get('tech_stack', []))}
Key insight: {project.get('key_insight', '')}
First step: {project.get('first_step', '')}

Return a JSON object with a "files" array. Each file should have:
- "path": relative file path (e.g., "README.md", "src/main.py")
- "content": the full file content as a string

Create:
1. README.md — project overview, getting started, architecture
2. A main application file appropriate for the tech stack
3. A configuration file if needed
4. A simple test or example that proves the core concept works
5. Any supporting files the project needs

Keep it minimal but functional. Return ONLY valid JSON, no markdown fences."""

    text = await _call_claude(prompt)
    text = text.strip()

    # Strip markdown fences if present
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        text = text.rsplit("```", 1)[0]
    text = text.strip()

    # Parse the file list
    try:
        start = text.find("{")
        end = text.rfind("}") + 1
        data = json.loads(text[start:end]) if start >= 0 else json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return [{"tool": "error", "path": "Failed to parse scaffold response"}]

    files_written = []
    for file_spec in data.get("files", []):
        rel_path = file_spec.get("path", "")
        content = file_spec.get("content", "")
        if not rel_path or not content:
            continue

        full_path = Path(output_dir) / rel_path
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(content)
        files_written.append({"tool": "Write", "path": str(full_path)})

    return files_written


# ---------------------------------------------------------------------------
# CLI for testing
# ---------------------------------------------------------------------------

async def _main():
    import sys

    if len(sys.argv) < 2:
        print("Usage: python forge.py [themes|synthesize|mockup|scaffold]")
        print(f"\n  Mode: {'SDK' if USE_SDK else 'API'}")
        if not USE_SDK and not ANTHROPIC_API_KEY:
            print("  WARNING: No ANTHROPIC_API_KEY set and no claude CLI found.")
            print("  Set ANTHROPIC_API_KEY or install claude CLI.")
        return

    cmd = sys.argv[1]
    print(f"  Mode: {'Claude Code SDK' if USE_SDK else 'Anthropic API'}")

    if cmd == "themes":
        print("Extracting themes from knowledge graph...")
        data = extract_themes()
        print(f"\n  {data['episode_count']} episodes, {data['entity_count']} entities, {data['edge_count']} edges")
        print("\n  Top episodes:")
        for ep in data["episodes"][:10]:
            print(f"    - {ep['name']}")
        print("\n  Top entities:")
        for ent in data["entities"][:15]:
            print(f"    - {ent['name']}")
        # Save to file for later use
        with open("themes_cache.json", "w") as f:
            json.dump(data, f, indent=2)
        print("\n  Saved to themes_cache.json")

    elif cmd == "synthesize":
        # Load cached themes or extract fresh
        try:
            with open("themes_cache.json") as f:
                data = json.load(f)
            print("Using cached themes...")
        except FileNotFoundError:
            print("Extracting themes...")
            data = extract_themes()

        selected = sys.argv[2:] if len(sys.argv) > 2 else None
        print(f"Synthesizing projects... (themes: {selected or 'all'})")
        result = await synthesize_projects(data, selected)
        print(json.dumps(result, indent=2))

        with open("projects_cache.json", "w") as f:
            json.dump(result, f, indent=2)
        print("\n  Saved to projects_cache.json")

    elif cmd == "mockup":
        # Load cached projects
        try:
            with open("projects_cache.json") as f:
                projects = json.load(f)
        except FileNotFoundError:
            print("Run 'synthesize' first")
            return

        idx = int(sys.argv[2]) if len(sys.argv) > 2 else 0
        project = projects["projects"][idx]
        print(f"Generating mockup for: {project['name']}...")
        html = await generate_mockup(project)
        filename = f"mockup_{idx}.html"
        with open(filename, "w") as f:
            f.write(html)
        print(f"  Saved to {filename}")

    elif cmd == "scaffold":
        try:
            with open("projects_cache.json") as f:
                projects = json.load(f)
        except FileNotFoundError:
            print("Run 'synthesize' first")
            return

        idx = int(sys.argv[2]) if len(sys.argv) > 2 else 0
        project = projects["projects"][idx]
        output_dir = f"./scaffolds/{project['name'].lower().replace(' ', '-')}"
        print(f"Scaffolding: {project['name']} → {output_dir}")
        files = await scaffold_project(project, output_dir)
        print(f"  Created {len(files)} files:")
        for f in files:
            print(f"    {f['tool']}: {f['path']}")


if __name__ == "__main__":
    asyncio.run(_main())

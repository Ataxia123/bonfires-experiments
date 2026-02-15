"""Project Forge — KG theme extraction + Claude project synthesis.

Pipeline:
  1. extract_themes()                      — query Bonfires KG, aggregate material
  2. synthesize_projects()                 — Claude generates project ideas (initial)
  3. synthesize_projects_with_existing()   — Claude updates/adds projects (incremental)
  4. generate_multi_mockup()               — Claude generates 1-3 HTML prototype files

Uses the Claude Agent SDK (claude-agent-sdk) for all Claude interactions.
"""

import asyncio
import json
import os
import urllib.request
import urllib.error
from pathlib import Path

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
DELVE_API_KEY = os.environ.get("DELVE_API_KEY", "8n5l-sJnrHjywrTnJ3rJCjo1f1uLyTPYy_yLgq_bf-d")
BONFIRE_ID = os.environ.get("BONFIRE_ID", "698b70002849d936f4259848")
BASE_URL = os.environ.get("DELVE_BASE_URL", "https://tnt-v2.api.bonfires.ai")

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


def extract_themes() -> dict:
    """Query the KG across multiple angles and return raw material for synthesis."""
    all_episodes = {}
    all_entities = {}
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
        "edges": all_edges[:100],
        "query_count": len(THEME_QUERIES),
        "episode_count": len(all_episodes),
        "entity_count": len(all_entities),
        "edge_count": len(all_edges),
    }


# ---------------------------------------------------------------------------
# Claude helper
# ---------------------------------------------------------------------------

async def _call_claude(prompt: str, max_turns: int = 3) -> str:
    """Call Claude via the Claude Agent SDK."""
    from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage

    result = ""
    async for message in query(
        prompt=prompt,
        options=ClaudeAgentOptions(max_turns=max_turns, allowed_tools=[]),
    ):
        if isinstance(message, ResultMessage) and message.result:
            result = message.result
    return result


def _parse_json_response(text: str) -> dict:
    """Parse JSON from Claude's response, stripping markdown fences."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        text = text.rsplit("```", 1)[0]
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            try:
                return json.loads(text[start:end])
            except json.JSONDecodeError:
                pass
        return {"projects": [], "error": "Could not parse JSON"}


def _build_kg_context(themes_data: dict) -> str:
    """Build the KG material section for prompts."""
    episode_summaries = "\n".join(
        f"- {ep['name']}: {ep['content_preview'][:200]}"
        for ep in themes_data.get("episodes", [])[:40]
    )
    entity_names = ", ".join(
        ent["name"] for ent in themes_data.get("entities", [])[:60]
    )
    edge_summary = "\n".join(
        f"- {e['name']}" for e in themes_data.get("edges", [])[:30]
    )
    return f"""## Episodes (conversations and events captured)
{episode_summaries}

## Key Entities (people, orgs, concepts)
{entity_names}

## Relationships
{edge_summary}"""


# ---------------------------------------------------------------------------
# Stage 2a: Initial project synthesis
# ---------------------------------------------------------------------------

async def synthesize_projects(themes_data: dict) -> dict:
    """Generate initial batch of project ideas from KG themes."""
    kg_context = _build_kg_context(themes_data)

    prompt = f"""You are Project Forge — a creative engine that synthesizes themes from a collective intelligence knowledge graph into novel, buildable project ideas.

Here is material from the EthBoulder 2026 knowledge graph (a weekend hackathon/unconference about Ethereum, public goods, AI agents, and regenerative systems in Boulder, Colorado):

{kg_context}

Generate 5 creative, ambitious but buildable project ideas. Each should:
1. Cross-pollinate 2-3 themes from the KG in a surprising way
2. Be technically specific (not vague platitudes)
3. Have a clear "what you'd build first" path
4. Connect to real people/orgs/tools mentioned in the KG
5. Range from weekend hacks to quarter-long builds

Be creative. Find non-obvious connections. The best ideas combine things nobody thought to combine.

Return ONLY valid JSON — an object with a "projects" array. Each project must have: name, tagline, description, themes (array), tech_stack (array), complexity ("weekend" or "month" or "quarter"), key_insight, first_step. No markdown fences, no explanation, just raw JSON."""

    text = await _call_claude(prompt)
    return _parse_json_response(text)


# ---------------------------------------------------------------------------
# Stage 2b: Incremental project synthesis (with existing projects)
# ---------------------------------------------------------------------------

async def synthesize_projects_with_existing(
    themes_data: dict,
    existing_projects: list[dict],
    change_summary: str,
) -> dict:
    """Update/add projects given new KG data and existing project list.

    Claude sees what already exists and decides per-project:
      - "unchanged" — no meaningful update needed
      - "updated" — description/insight refined based on new KG material
      - "new" — genuinely novel idea that doesn't overlap existing ones
      - "retired" — now contradicted or irrelevant
    """
    kg_context = _build_kg_context(themes_data)

    existing_summary = "\n".join(
        f"- **{p['name']}**: {p.get('tagline', '')} (key insight: {p.get('key_insight', '')})"
        for p in existing_projects
    )

    prompt = f"""You are Project Forge — a creative engine that synthesizes themes from a collective intelligence knowledge graph into novel, buildable project ideas.

Here is UPDATED material from the EthBoulder 2026 knowledge graph:

{kg_context}

## Changes since last generation
{change_summary}

## Existing projects (previously generated)
{existing_summary}

Your task — be CONSERVATIVE:
1. Review each existing project. If the new KG material meaningfully changes its premise, output an UPDATED version with "status": "updated". If not, output it with "status": "unchanged" (include its full data so we preserve it).
2. If the new material suggests a genuinely NEW project idea that doesn't overlap existing ones, add it with "status": "new". Add at most 1-2 new projects.
3. If an existing project is now contradicted or irrelevant, mark it "status": "retired".
4. MOST projects should be "unchanged" — only update when there's a real reason.

Return ONLY valid JSON — an object with a "projects" array. Each project must have: status ("unchanged"|"updated"|"new"|"retired"), name, tagline, description, themes (array), tech_stack (array), complexity ("weekend"|"month"|"quarter"), key_insight, first_step. No markdown fences, just raw JSON."""

    text = await _call_claude(prompt)
    return _parse_json_response(text)


# ---------------------------------------------------------------------------
# Stage 3: Multi-file mockup generation
# ---------------------------------------------------------------------------

async def generate_multi_mockup(project: dict, output_dir: str) -> dict:
    """Generate 1-3 HTML prototype files for a project.

    Args:
        project: project data dict
        output_dir: absolute path where to write the HTML files

    Returns:
        {"files": [{"name": "index.html", "label": "Home", "is_entry": true}, ...]}
    """
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    prompt = f"""Generate a small HTML prototype for this project — between 1 and 3 HTML files that work together to show how it would function.

**{project['name']}**
{project.get('tagline', '')}

{project.get('description', '')}

Tech stack: {', '.join(project.get('tech_stack', []))}
Key insight: {project.get('key_insight', '')}
First step: {project.get('first_step', '')}

Rules:
- Each file must be self-contained (inline CSS and JS) but can link to sibling files using relative hrefs (e.g., href="dashboard.html")
- The first file MUST be named "index.html" — it's the main entry point
- Additional files should show different screens/flows (dashboard, detail view, settings, etc.)
- Use a consistent design language across all files
- Include realistic placeholder content (not lorem ipsum)
- Modern, clean design with good typography
- Mobile-responsive
- Make it look like a polished interactive prototype, not a wireframe

Return ONLY valid JSON with this structure (no markdown fences):
{{"files": [{{"name": "index.html", "label": "Home", "html": "<!DOCTYPE html>..."}}, {{"name": "dashboard.html", "label": "Dashboard", "html": "<!DOCTYPE html>..."}}]}}"""

    text = await _call_claude(prompt, max_turns=5)
    result = _parse_json_response(text)

    files_written = []
    for file_spec in result.get("files", []):
        name = file_spec.get("name", "")
        html = file_spec.get("html", "")
        label = file_spec.get("label", name)
        if not name or not html:
            continue

        # Clean up HTML if wrapped
        if "```html" in html:
            html = html.split("```html", 1)[1]
            html = html.rsplit("```", 1)[0]
        elif html.startswith("```"):
            html = html.split("\n", 1)[1] if "\n" in html else html[3:]
            html = html.rsplit("```", 1)[0]

        filepath = Path(output_dir) / name
        filepath.write_text(html.strip())
        files_written.append({
            "name": name,
            "label": label,
            "is_entry": name == "index.html",
        })

    # Write manifest
    manifest = {
        "project_name": project.get("name", ""),
        "generated_at": __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        ).isoformat(),
        "files": files_written,
    }
    (Path(output_dir) / "manifest.json").write_text(json.dumps(manifest, indent=2))

    # If Claude didn't return parseable files, fall back to single-page
    if not files_written:
        print("  [forge] Multi-file parse failed, falling back to single-page mockup")
        html = await _call_claude(
            f"Generate a single self-contained HTML mockup for: {project['name']} — {project.get('tagline', '')}. "
            f"{project.get('description', '')} Return ONLY the HTML, starting with <!DOCTYPE html>.",
            max_turns=3,
        )
        if "```html" in html:
            html = html.split("```html", 1)[1].rsplit("```", 1)[0]
        elif "```" in html:
            html = html.split("```", 1)[1].rsplit("```", 1)[0]
        filepath = Path(output_dir) / "index.html"
        filepath.write_text(html.strip())
        files_written = [{"name": "index.html", "label": "Home", "is_entry": True}]
        manifest["files"] = files_written
        (Path(output_dir) / "manifest.json").write_text(json.dumps(manifest, indent=2))

    return {"files": files_written}


# ---------------------------------------------------------------------------
# CLI for testing
# ---------------------------------------------------------------------------

async def _main():
    import sys

    if len(sys.argv) < 2:
        print("Usage: python forge.py [themes|synthesize|mockup]")
        return

    cmd = sys.argv[1]

    if cmd == "themes":
        print("Extracting themes from knowledge graph...")
        data = extract_themes()
        print(f"\n  {data['episode_count']} episodes, {data['entity_count']} entities, {data['edge_count']} edges")
        for ep in data["episodes"][:10]:
            print(f"    - {ep['name']}")
        with open("themes_cache.json", "w") as f:
            json.dump(data, f, indent=2)
        print("\n  Saved to themes_cache.json")

    elif cmd == "synthesize":
        try:
            with open("themes_cache.json") as f:
                data = json.load(f)
        except FileNotFoundError:
            data = extract_themes()
        result = await synthesize_projects(data)
        print(json.dumps(result, indent=2))

    elif cmd == "mockup":
        try:
            with open("projects_cache.json") as f:
                projects = json.load(f)
        except FileNotFoundError:
            print("Run 'synthesize' first")
            return
        idx = int(sys.argv[2]) if len(sys.argv) > 2 else 0
        project = projects["projects"][idx]
        print(f"Generating mockup for: {project['name']}...")
        result = await generate_multi_mockup(project, f"./mockups/test-{idx}")
        print(f"  Created {len(result['files'])} files")
        for f in result["files"]:
            print(f"    - {f['name']} ({f['label']})")


if __name__ == "__main__":
    asyncio.run(_main())

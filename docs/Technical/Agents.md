---
title: Agents (Technical)
description: Stateless interfaces to collective knowledge
section: technical
---
<div style="text-align: center;"> <img src="collective-intelligence-mask.png" alt="Image Description" style="max-width: 80%; height: auto;"> <p style="margin-top: 10px; font-size: 14px; color: #666;"> <em>art by Yuhdo.eth</em></p> </div>

An agent is a stateless interface that gives users conversational access to a [[files/Technical/Bonfires|Bonfire's]] knowledge. Agents live in group chats (Telegram or Discord) and respond like any other member.

## What Agents Do

**Respond when tagged.** Tag `@agentname`, reply to its message, or DM it. The agent runs a multi-step reasoning process: think → search → tools → respond.

**Listen when not tagged.** Untagged messages are read silently, stored on the agent's stack, and processed into the knowledge graph on the 20-minute cycle.

**Use tools automatically.** Agents select tools based on what you're asking. No slash commands — just natural language.

| Tool | What it does |
|---|---|
| **Message search** | Semantic search through past conversations |
| **Scheduling** | Reminders, recurring tasks, timezone-aware |
| **Identity lookup** | Resolve users across platforms |
| **Twitter** | Search tweets, fetch threads, post (when configured) |
| **Trimtab** | Manage the agent's working memory |
| **Custom MCP tools** | Any MCP-compatible tool |

## How Agents Think

When you tag the agent, it runs a LangGraph state machine:

1. **Think** — Initial reasoning about the message
2. **Decide** — Should the agent respond? Intent detection for voice messages.
3. **Search** — Query the knowledge graph and episodic stack for relevant context
4. **Choose tools** — Determine if tools are needed
5. **Execute tools** — Run tools, loop if more calls needed
6. **Respond** — Generate response using all gathered context
7. **Image generation** — If requested (otherwise skipped)
8. **Add to stack** — Store the exchange for future processing

The agent doesn't just react to the last message. It reasons with the full history of its Bonfire.

## Episodic Stack

Each agent has a temporary message queue (the stack). Messages accumulate, then every 20 minutes a background process:

1. Labels unlabeled messages
2. Extracts an episode summary
3. Creates the episode in the **Bonfire's** knowledge graph
4. Clears the stack

Chat and stack processing are independent. Chatting doesn't trigger processing — the 20-minute cycle runs regardless.

## Working Memory (Trimtab)

Each agent maintains a persistent scratchpad:

- **Quests** (up to 20) — Goals and objectives with priority levels
- **Notepad** — Running notes updated as the agent learns
- **Friends** (up to 10) — Important contacts and relationship context

This gives continuity across conversations without duplicating the Bonfire's knowledge.

## Supported Platforms

**Telegram** — Forum topics, text, voice messages, audio, photos, stickers, documents.

**Discord** — Channels as topics, servers as chats. Text, images, voice, audio.

Both platforms get typing indicators, status messages, and rich formatting.

## Voice and Images

**Voice/audio** — Automatically transcribed via Whisper and processed as text.

**Image understanding** — When enabled, the agent can analyze photos sent in chat.

**Image generation** — On request, generates images via Flux models directly in chat.

## Configuration

Everything is configured in the database — no code changes or redeployments:

- **Identity** — Name, username, system prompt (personality/role/tone)
- **Chat policies** — Open vs. restricted, DM settings, per-group/per-topic controls
- **Feature flags** — Toggle image gen, voice transcription, image understanding, scheduling
- **Tools** — Enable/disable any tool by ID
- **Silent mode** — Agent processes but doesn't respond. Useful for learning periods.
- **Storage controls** — Granular control over what gets stored per group/topic

## Multi-Agent Runtime

A single deployment runs all agents. The runtime manager:

- Loads all active agents on startup
- Polls for configuration changes every 2 minutes
- Health-checks agents every 2 minutes
- Auto-restarts unhealthy agents
- Gracefully stops deactivated agents

Add a new agent: create config in MongoDB, set `isActive: true`. It starts within 2 minutes.

---

**See also:** [[files/Technical/Bonfires|Bonfires]] · [[DAO Coordinator]] · [[Team Ops Agent]]

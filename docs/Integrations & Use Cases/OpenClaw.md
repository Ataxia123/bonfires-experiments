---
title: OpenClaw
description: Persistent memory layer for AI agents via Bonfires integration
section: integrations
---

OpenClaw is an AI agent framework. Bonfires provides it with a persistent memory layer — giving OpenClaw agents episodic recall, semantic search, and a relationship graph that survives session compactions.

## The Problem Bonfires Solves

Most AI agent frameworks manage memory through local files: curated long-term facts in a MEMORY.md, daily logs, and session context that gets compacted when it fills up (~200k tokens). After compactions, detailed conversation context is lost. Summaries survive, but nuance disappears.

Three months and fifty compactions later, when you ask "what was that permission system we built?" — the agent reads memory files and finds a summary. The full context is gone.

## What Bonfires Adds

**Episodic memory.** Conversations are captured as episodes stored externally in the Bonfire — not in session context. They survive any number of compactions.

**Semantic search.** Query "what did we discuss about DAO voting?" across *all* past conversations, not just what fits in the current context window.

**Relationship graph.** The knowledge graph maps entities and their connections. The agent knows that `zeugh.eth → operates → Clop → uses → ClawSig → based on → Zodiac` — structured relationships extracted automatically from conversations.

**Survives compactions.** Episodes live in the Bonfire's database, not the agent's session context. The memory layer is external and persistent.

## How It Works

Bonfires integrates with OpenClaw through a `skills.md` memory plugin. The agent's conversations flow into a Bonfire, where they're processed on the standard 20-minute cycle — entities extracted, relationships mapped, episodes created.

When the agent needs to recall past context, it queries the Bonfire's knowledge graph and episodic memory rather than relying solely on local files.

```
Without Bonfires:
  Session → compaction → summary → local files
  (nuance lost after each compaction)

With Bonfires:
  Session → compaction → summary → local files
       └──→ Bonfire → episodes + graph + semantic index
            (full context preserved indefinitely)
```

## The Difference

| | Without Bonfires | With Bonfires |
|---|---|---|
| **After compaction** | Summary only | Full episodic context |
| **Cross-session recall** | Read memory files | Semantic search across all conversations |
| **Entity relationships** | Manual notes | Auto-extracted graph |
| **3 months later** | Fragmented summaries | Exact conversation context |

## Why This Matters

This is a concrete example of the broader [[Vision|Bonfires thesis]]: AI agents need memory infrastructure that exists outside their own context window. Local memory systems are fundamentally limited by session size. Bonfires provides the external knowledge layer that makes agents genuinely persistent.

Any agent framework — not just OpenClaw — can integrate a Bonfire as its memory backend. The agent gets smarter over time because its knowledge graph grows independently of session constraints.

---

**See also:** [[files/Technical/Agents|Agents]] · [[DAO Coordinator]] · [[files/Technical/Bonfires|Bonfires]]

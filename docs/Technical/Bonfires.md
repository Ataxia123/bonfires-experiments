---
title: Bonfires (Technical)
description: The knowledge engine — architecture and internals
section: technical
---

A Bonfire is the knowledge engine. It's the persistent store that owns all of a community's accumulated intelligence.

## What a Bonfire Owns

**Documents and chunks.** All ingested content — uploaded documents, transcripts, raw text — is split into chunks and stored. These are the raw material for everything downstream.

**Knowledge graph.** A Neo4j graph (via Graphiti) containing entities, relationships, and episodes extracted from conversations and documents. This is the structured representation of what your community knows.

**Vector store.** Semantic embeddings (Weaviate) of all chunks and labels. This enables search by meaning — find content similar to a query rather than matching keywords.

**Taxonomy and labels.** Auto-generated multi-label classification of your content. Labels are applied at the summary level and propagated to chunks, driving content organization and discovery.

## Bonfire ≠ Agent

[[files/Technical/Agents|Agents]] are stateless interfaces. They don't store anything. They read from and write to the Bonfire.

One Bonfire can have many agents. All agents in a Bonfire share the same knowledge graph, documents, and vector store. Different windows into the same knowledge base.

```
         ┌──────────────────────────┐
         │         Bonfire          │
         │                          │
         │  Documents · Graph       │
         │  Vectors   · Taxonomy    │
         └─────────┬────────────────┘
                   │
        ┌──────────┼──────────┐
        │          │          │
     Agent A    Agent B    Agent C
    (Telegram)  (Discord)   (API)
```

## How Knowledge Flows In

**Conversation capture (automatic):** Every 20 minutes, a background process takes recent messages from each agent's stack, extracts entities and relationships, creates episodic summaries, and writes everything to the Bonfire's knowledge graph. This happens for all messages — including ones the agent didn't respond to.

**Document ingestion (manual):** Upload PDFs, text files, or raw content via API. Content is chunked, summarized, labeled with taxonomy categories, and embedded into the vector store.

**Episode updates (API):** External systems can write episodes directly to the knowledge graph.

## The Content Pipeline

```
Content arrives
    │
    ▼
Split into chunks → stored in MongoDB
    │
    ▼
Generate summaries (async job)
    │
    ▼
Generate taxonomy → label chunks (async jobs)
    │
    ▼
Embed into vector store (Weaviate)
    │
    ▼
Extract entities & relationships → knowledge graph (Neo4j)
    │
    ▼
Content is searchable via:
  • Semantic search (vector store)
  • Knowledge graph search (Delve)
  • Agent chat
```

## Searching a Bonfire

**Delve search** — Unified semantic search across the knowledge graph. Returns entities, relationships, and episodes ranked by relevance.

**Vector search** — Find document chunks or labels semantically similar to a query.

**Agent chat** — Ask the agent in your group chat. It combines knowledge graph results, vector search results, and recent conversation context before responding.

**Graph explorer** — Visual exploration at [graph.bonfires.ai](https://graph.bonfires.ai).

**MCP** — Connect Claude Desktop, Cursor, or other MCP-compatible tools to query your Bonfire programmatically.

## DataRooms and HyperBlogs

DataRooms package a Bonfire's content for the marketplace. [[files/Integrations & Use Cases/HyperBlogs|HyperBlogs]] are AI-generated articles created from DataRoom knowledge, purchasable onchain. See [[HyperBlogs (Technical)]] for the full architecture.

---

**See also:** [[files/Technical/Agents|Agents]] · [[docs/docs26/kEngrams]] · [[HyperBlogs (Technical)]] · [[docs/Introduction]]

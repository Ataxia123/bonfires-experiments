---
title: Introduction
description: What Bonfires is, how it works, and how to use it
section: overview
---

Bonfires is a knowledge coordination platform. It transforms group conversations into AI-accessible knowledge graphs, giving communities a shared memory that grows smarter over time.

## The Problem

Groups generate enormous value through conversation. Decisions get made, insights emerge, context accumulates — and almost all of it vanishes into chat logs. When someone new joins, or someone asks "why did we decide X?", the answer requires archaeology across platforms or asking whoever was there.

AI assistants make individuals more productive. But they can't help a group align on what they collectively know, decided, or believe. That requires memory infrastructure oriented around the **group** as the primary unit.

## How It Works

1. **An agent joins your group chat** (Telegram or Discord). It reads messages, responds when tagged, and silently processes everything else.

2. **Every 20 minutes**, a background process captures recent conversations and extracts structured knowledge — entities, relationships, decisions, insights — into a [[files/Technical/Bonfires|knowledge graph]].

3. **Anyone can query the agent** and get answers grounded in your community's actual history. The agent searches the knowledge graph, semantic vector store, and recent conversation context before responding.

4. **Knowledge compounds.** The longer your Bonfire runs, the deeper and more valuable its understanding becomes.

## What You Get

| Component                                                          | What it does                                                                                                                          |
| ------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------- |
| **[[docs/docs26/Agents/Agents]]**                                                    | Lives in your group chat. Responds when tagged, listens always.                                                                       |
| **[[files/Technical/Bonfires\|Bonfire]]**                     | The knowledge engine. Stores documents, graph, vectors, taxonomy.                                                                     |
| **Knowledge Graph**                                                | Entities, relationships, and episodes extracted from your conversations. Visualise at [graph.bonfires.ai](https://graph.bonfires.ai). |
| **Semantic Search**                                                | Query your knowledge base by meaning, not keywords.                                                                                   |
| **[[files/Integrations & Use Cases/HyperBlogs\|HyperBlogs]]** | AI-generated articles from your knowledge, monetizable via x402 payments.                                                             |
| **MCP Integration**                                                | Connect Claude Desktop, Cursor, or other AI tools to your Bonfire.                                                                    |

## How to Use It

**Talk naturally.** The agent lives where your team already works. Tag it with `@agentname` to get a response. Send messages without tagging and it still captures the knowledge.

**Ask questions.** "What did we discuss about X last week?" "Who in the group knows about Y?" "What were the three options we considered for Z?" The agent draws on the full history of your community.

**Let it work in the background.** Even when the agent doesn't respond, it's processing. Every conversation feeds the knowledge graph. When someone asks a question weeks later, the context is already there.

## Current Status

23+ live deployments. 36,700+ knowledge graph nodes. 5,700+ episodic records captured.

Today, Bonfires are deployed by the team. Get one by minting a **[[files/Genesis NFT]]** (0.1 ETH) at [mint.bonfires.ai](https://mint.bonfires.ai), or engage **[[files/Overview/Team|Bonfires Labs]]** for a bespoke implementation.

Permissionless deployment is coming with [[files/Knowledge Economy/$KNOW|$KNOW]] and the [[files/Knowledge Economy/Knowledge Network|Knowledge Network]].

---

**Next:** [[Vision]] — Why collective memory matters now

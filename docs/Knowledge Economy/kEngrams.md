---
title: kEngrams
description: Portable units of community knowledge
section: knowledge-economy
---
A kEngram is a portable, AI-readable unit of knowledge. Not raw data — extracted understanding.

<div style="text-align: center;">
<img src="darwin-ke-image.png" alt="Image Description" style="max-width: 80%; height: auto;">
 <p style="margin-top: 10px; font-size: 14px; color: #666;"> <em>from Charles Darwin's Galapagos journal, describing the evolutionary branching of Finches</em></p>
</div>

When your Bonfire processes conversations, it doesn't just store what was said. It extracts entities, relationships, intent, context, and insights. These extractions are encoded as kEngrams: structured knowledge that can inform decisions, answer queries, and — eventually — be shared and valued across the network.

## What's Inside a kEngram

**Content** — The actual knowledge: a fact, relationship, insight, or pattern.

**Attribution** — Who contributed. May be individual participants, the Bonfire collectively, or derived from multiple sources.

**Provenance** — Source Bonfire, originating episodes, contributing entities, timestamp.

**Embedding** — Position in semantic space. Determines what other knowledge it relates to and what queries it's relevant for.

**Metadata** — Quality signals: endorsements, retrieval history, contributor reputation, stakes attached.

## How They're Created

Every 20 minutes, your Bonfire processes recent conversations:

1. Raw conversation data is analyzed
2. Entities and relationships are extracted
3. New kEngrams are generated
4. Existing kEngrams are updated with new context
5. The knowledge graph grows

Deduplication matches new extractions against existing kEngrams — updating rather than duplicating. Quality filtering removes noise and holds back low-confidence extractions. The goal is signal, not volume.

## The Gravitational Model

kEngrams exist in a high-dimensional semantic embedding space. Their value is determined by a gravitational model:

**Semantic proximity.** How close a kEngram is to high-priority knowledge targets (research questions, bounties, community needs).

**Mass.** A combination of informational weight (retrieval frequency, content density), contributor reputation (impact, social trust, verified credentials), and economic stake.

**Gravitational energy.** The pull between a kEngram and active targets:

```
V = G · (mass of output × mass of target) / f(distance)
```

Outputs that are semantically close to important targets, rich in information, and well-supported exert stronger gravitational pull — and are more strongly rewarded.

## The Space of Ideas

kEngrams close together in the embedding space are semantically related. They naturally cluster into knowledge domains and topics. As new kEngrams are added, the space evolves — new topics create new regions, connections bridge previously separate areas, and the map of collective understanding grows.

This is the geometric intuition: knowledge has structure. Ideas branch, connect, and evolve. kEngrams make that structure explicit, searchable, and economically meaningful.

## kEngrams in the Knowledge Network

When the [[files/Knowledge Economy/Knowledge Network]] launches, kEngrams become shareable across Bonfires. Communities that opt in expose their kEngrams to external queries, with attribution flowing back. Retrieval frequency and quality determine [[files/Knowledge Economy/$KNOW|$KNOW]] rewards.

See [[files/Knowledge Economy/Knowledge Network]] for exposure levels, retrieval economics, and participation details.

## Current Status

> [!success] Live Now
> - kEngram generation through Bonfires
> - Knowledge graph visualization at [graph.bonfires.ai](https://graph.bonfires.ai)
> - Local retrieval for queries within your Bonfire

> [!warning] Coming with Knowledge Network
> - Cross-network sharing and retrieval
> - Retrieval tracking and rewards
> - kEngram marketplace

---

**See also:** [[files/Knowledge Economy/$KNOW|$KNOW]] · [[Improving Access to AI]] · [[files/Technical/Bonfires|Bonfires]]

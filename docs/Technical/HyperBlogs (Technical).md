---
title: HyperBlogs (Technical)
description: How HyperBlogs are created, purchased, and served
section: technical
---

# HyperBlogs (Technical)

HyperBlogs are AI-generated articles produced from a DataRoom's knowledge graph and purchased via onchain micropayments.

## How They're Created

```
Reader sends payment (X402 header)
        │
        ▼
Payment verified via OnchainFi (Base network, USDC)
        │
        ▼
System queries the DataRoom's knowledge graph
  → Semantic search for relevant entities, relationships, episodes
  → Context assembled from matching kEngrams
        │
        ▼
LLM generates article from assembled context
        │
        ▼
HyperBlog stored in MongoDB
  → Viewable, votable, commentable
  → Banner image can be generated separately
```

The generation is on-demand: the article doesn't exist until someone purchases it. The topic provided by the reader shapes the query, and the knowledge graph provides the grounding.

## Payment Flow (X402)

HyperBlog purchases use the X402 payment standard — ERC-3009 authorization signatures on the Base network with USDC.

The reader includes an `X-Payment` header with their request. This contains a signed authorization proving they've approved the payment, without requiring a separate transaction step.

```
POST /hyperblogs/purchase
{
  "dataroom_id": "dataroom-789",
  "topic": "How does automated market making work?",
  "payment_header": "<X402 payment header>"
}
```

No wallet popups, no manual signing per request. The payment header validates access in lieu of a bearer token.

## DataRooms

HyperBlogs live inside **DataRooms** — public or private knowledge repositories that package a [[files/Technical/Bonfires|Bonfire's]] content for the marketplace.

A DataRoom includes:

- The Bonfire's knowledge graph and content, packaged for external access
- A **Hierarchical Task Network (HTN)** — a curriculum-like structure organizing knowledge into a learning path
- HyperBlogs generated from the content
- A contribution leaderboard
- Micro-subscription support for payment-gated access

**Creating a DataRoom:**

```
POST /datarooms
{
  "name": "DeFi Research Hub",
  "description": "Comprehensive research on decentralized finance",
  "bonfire_id": "bonfire-456",
  "creator_wallet": "0xYourWallet"
}
```

## Micro-Subscriptions

For ongoing access to a DataRoom's premium features (payment-gated chat, Delve search, HTN curriculum), users can create a micro-subscription rather than paying per request.

```
POST /microsubs
{
  "dataroom_id": "dataroom-789",
  "wallet_address": "0xYourWallet",
  "payment_header": "<X402 payment header>"
}
```

The payment transaction hash acts as the subscription identifier. Check your subscriptions at `GET /microsubs?wallet_address=0xYourWallet`.

## API Reference

### HyperBlog Endpoints

| Endpoint | Method | Description |
|---|---|---|
| `POST /hyperblogs/purchase` | POST | Purchase and generate a HyperBlog |
| `GET /datarooms/hyperblogs` | GET | List all public HyperBlogs |
| `GET /datarooms/{id}/hyperblogs` | GET | List HyperBlogs for a DataRoom |
| `GET /datarooms/{id}/hyperblogs/{hb_id}` | GET | Get a single HyperBlog |
| `POST /datarooms/hyperblogs/{hb_id}/vote` | POST | Vote on a HyperBlog |
| `POST /datarooms/hyperblogs/{hb_id}/comments` | POST | Comment on a HyperBlog |
| `GET /datarooms/hyperblogs/{hb_id}/comments` | GET | View comments |
| `POST /datarooms/hyperblogs/{hb_id}/view` | POST | Record a view (analytics) |
| `POST /datarooms/hyperblogs/{hb_id}/banner` | POST | Generate banner image |

### DataRoom Endpoints

| Endpoint | Method | Description |
|---|---|---|
| `POST /datarooms` | POST | Create a DataRoom |
| `GET /datarooms` | GET | List all DataRooms |
| `GET /datarooms/{id}` | GET | Get a specific DataRoom |
| `POST /datarooms/{id}/preview` | POST | Preview knowledge graph |
| `GET /datarooms/{id}/htn` | GET | Get HTN curriculum |
| `GET /datarooms/{id}/contributions` | GET | Contribution leaderboard |

### Payment-Gated Endpoints

| Endpoint | Method | Description |
|---|---|---|
| `POST /paid/agents/{id}/chat` | POST | Chat with a DataRoom's agent |
| `POST /paid/agents/{id}/delve` | POST | Search a DataRoom's knowledge graph |

---

**See also:** [[files/Integrations & Use Cases/HyperBlogs|HyperBlogs]] · [[files/Technical/Bonfires|Bonfires]] · [[files/Knowledge Economy/$KNOW|$KNOW]]

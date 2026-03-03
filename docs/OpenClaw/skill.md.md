---
name: delve-backend-interaction
description: Guides agents on calling the Delve REST API, including auth, job polling, and multi-step workflows (KG search, content ingestion, agent memory, payments). Use when interacting with Delve backend endpoints or debugging bonfire/agent access.
homepage: docs/API_ROUTES.md
metadata: {"delve":{"requires":{"bins":["curl"],"env":["DELVE_API_KEY"]}}}
---

# Delve Backend Interaction

REST API reference for the Delve knowledge graph platform.

## Setup

```bash
export DELVE_API_KEY="<api-key-or-jwt>"
export BONFIRE_ID="<bonfire-object-id>"       # 24-char hex
export AGENT_ID="<agent-id>"                  # only for agent-scoped routes
export BASE_URL="https://tnt-v2.api.bonfires.ai"
```

**Auth:** `Authorization: Bearer <key>` (preferred) or `X-API-Key: <key>` (legacy).
API keys have a `bonfire_ids` list and an `is_admin` flag. Admin keys bypass bonfire-scoping.
Clerk JWTs are also accepted and mapped to bonfires via org membership.

## Pre-flight (run BEFORE any workflow)

Execute both checks sequentially. **Stop on failure.**

```bash
# 1. Connectivity (no auth) — expect 200
curl -sf "$BASE_URL/healthz"

# 2. Auth + bonfire access — expect 200, not 403
curl -sf -H "Authorization: Bearer $DELVE_API_KEY" \
  -H "Content-Type: application/json" \
  -d "{\"bonfire_id\":\"$BONFIRE_ID\"}" \
  "$BASE_URL/generate_summaries"
```

If step 1 fails: server is down or `BASE_URL` is wrong.
If step 2 returns 403: API key does not exist or is not linked to this bonfire.

---

## REST Workflows

Pick the workflow that matches the task. Each is self-contained.

### 1. Knowledge Graph Search

**Goal:** Query the unified KG. Synchronous -- no job polling needed.

```bash
RESULT=$(curl -sf -H "Authorization: Bearer $DELVE_API_KEY" \
  -H "Content-Type: application/json" \
  -d "{\"query\":\"<natural language query>\",\"bonfire_id\":\"$BONFIRE_ID\",\"num_results\":10}" \
  "$BASE_URL/delve")
echo "$RESULT"
```

Use `graph_id` from the response in follow-up queries for incremental search.
Pass `agent_id` to persist graph state per agent.
See [POST /delve](#post-delve) for full request/response schema.

### 2. Agent Memory (Stack -> Episodes)

**Goal:** Record messages, process them into episodes, then search.

```bash
# Step 1 — Add message(s) to stack
curl -sf -H "Authorization: Bearer $DELVE_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"message":{"text":"Hello","userId":"user-1","chatId":"chat-1","timestamp":"2025-02-03T12:00:00Z"}}' \
  "$BASE_URL/agents/$AGENT_ID/stack/add"
# -> {"success":true,"message_ids":[...],"stack_count":N}

# For paired user+agent messages:
curl -sf -H "Authorization: Bearer $DELVE_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"text":"Question?","userId":"user-1","chatId":"chat-1","timestamp":"2025-02-03T12:00:00Z"},{"text":"Answer.","userId":"agent-1","chatId":"chat-1","timestamp":"2025-02-03T12:00:05Z"}],"is_paired":true}' \
  "$BASE_URL/agents/$AGENT_ID/stack/add"

# Step 2 — Process stack into episodes
curl -sf -H "Authorization: Bearer $DELVE_API_KEY" \
  "$BASE_URL/agents/$AGENT_ID/stack/process"
# -> {"success":true,"message_count":N,"episode_id":"..."}

# Step 3 — Search episodes
curl -sf -H "Authorization: Bearer $DELVE_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"limit":20,"after_time":"2025-02-01T00:00:00Z"}' \
  "$BASE_URL/knowledge_graph/agents/$AGENT_ID/episodes/search"
# -> {"success":true,"episodes":[...],"entities":[...],"edges":[...]}
```

### 3. Stack Heartbeat

Establish a **periodic task** that calls `POST /agents/{agent_id}/stack/process` every 15-20 minutes. This ensures queued messages become episodic memories instead of sitting on the stack.

Use `POST /agents/{agent_id}/stack/add` to push messages, replies, or facts into the stack. Paired messages should use `is_paired: true`. When the heartbeat fires, those messages are turned into an episode and written to the knowledge graph.

---

## Endpoint Reference

### Health

#### GET /healthz
No auth required.

**Response (200):**
```json
{
  "status": "ok",
  "api": "Document Analysis",
  "version": "2.0",
  "job_system": { "status": "ok", "active_jobs": 0 }
}
```

#### GET|POST /healthz/agent-access/{agent_id}
Auth required. Verifies the caller has access to the given agent.

**Response (200):** Same as `/healthz` plus access confirmation.
**Response (403):** `{"detail": "Unauthorized"}`

---

### Knowledge Graph

#### POST /delve
Auth required. Unified KG search -- the primary query endpoint.

**Request body:**
```json
{
  "query": "string (required unless center_node_uuid)",
  "bonfire_id": "ObjectId-string (required)",
  "agent_id": "string (optional, persists graph state)",
  "graph_id": "string (optional, incremental search)",
  "num_results": 10,
  "center_node_uuid": "string (optional, alternative to query)",
  "search_recipe": "string (optional)",
  "min_fact_rating": 0.0,
  "mmr_lambda": 0.5,
  "window_start": "ISO-8601 (optional)",
  "window_end": "ISO-8601 (optional)",
  "relationship_types": ["string"]
}
```

**Response (200):**
```json
{
  "success": true,
  "query": "What did users discuss about Python?",
  "num_results": 5,
  "episodes": [
    {
      "uuid": "550e8400-...",
      "name": "Episode name",
      "content": "Natural language summary of the episode",
      "created_at": "2025-02-03T12:00:00Z",
      "group_id": "507f1f77bcf86cd799439011"
    }
  ],
  "entities": [
    {
      "uuid": "660e8400-...",
      "name": "Python Programming",
      "labels": ["TaxonomyLabel"],
      "summary": "Description of this entity",
      "attributes": {},
      "created_at": "2025-02-03T12:00:00Z",
      "group_id": "507f1f77bcf86cd799439011"
    }
  ],
  "edges": [
    {
      "uuid": "770e8400-...",
      "source_uuid": "660e8400-...",
      "target_uuid": "880e8400-...",
      "type": "relates_to"
    }
  ],
  "nodes": [],
  "metrics": { "duration_ms": 234.5, "deduplication_stats": {} },
  "graph_id": "abc123def456",
  "parent_graph_id": "string | null",
  "center_node_uuid": "string | null",
  "agent_context": { "agent_id": null, "bonfire_id": "string" },
  "new_nodes_count": 0,
  "new_edges_count": 0,
  "cached": false
}
```

**Key behavior:**
- Save `graph_id` from the response and pass it in follow-up calls for incremental search.
- Use `center_node_uuid` to explore the neighborhood of a specific entity (query can be empty in this case).
- `episodes` and `entities` are the primary result lists. `nodes` exists for backward compat -- prefer `episodes` + `entities`.
- Header `X-Bonfire-Id` overrides body `bonfire_id`.

#### POST /knowledge_graph/episode_update
Auth required. Ingest a new agent episode into the knowledge graph.

**Request body:**
```json
{
  "bonfire_id": "ObjectId-string (required)",
  "agent_id": "string (optional)",
  "episode": {
    "summary": "Concise episode summary (optional)",
    "content": "Full episode text (optional)",
    "window_start": "ISO-8601 (optional)",
    "window_end": "ISO-8601 (optional)",
    "valid_at": "ISO-8601 (optional, fallback for instant episodes)",
    "source": "string (optional, e.g. vector/doc/chat)",
    "source_description": "string (optional)",
    "attributes": {}
  },
  "labels": [
    { "label_id": "uuid-string", "label_name": "string" }
  ],
  "user_updates": [
    {
      "user_id": "uuid-string",
      "username": "string",
      "per_label": [
        { "label_id": "uuid-string", "label_name": "string", "activity": "string" }
      ]
    }
  ],
  "working_doc_updates": [],
  "vector_refs": {
    "results": [
      { "chunk_id": "string", "doc_id": "string", "confidence": 0.9 }
    ]
  }
}
```

**Response (200):**
```json
{
  "success": true,
  "episode_uuid": "550e8400-...",
  "message": "Episode ingested successfully"
}
```

**Notes:**
- `labels` should be resolved taxonomy labels (query `/knowledge_graph/entity/{uuid}` first).
- `user_updates` creates User entities and TaxonomyLabel->User edges.
- `working_doc_updates` is deprecated -- Graphiti extracts entities/edges automatically from structured content.

#### POST /api/kg/add-triplet
Auth required. Add a single triplet (two nodes + edge) to the KG. Nodes are created if they don't exist.

**Request body:**
```json
{
  "bonfire_id": "ObjectId-string (required, also accepts group_id)",
  "source_node": { "name": "string", "node_type": "string" },
  "edge": { "relationship_type": "string" },
  "target_node": { "name": "string", "node_type": "string" },
  "generate_embeddings": true,
  "episode_id": "uuid-string (optional, links triplet to an episode)"
}
```

**Response (200):**
```json
{
  "success": true,
  "triplet_id": "string | null",
  "message": "Triplet added successfully"
}
```

**Note:** For richer context, prefer `POST /knowledge_graph/episode_update` (structured episodes with entity extraction) over raw triplets.

#### POST /knowledge_graph/entity
Auth required. Create an entity node.

**Request body:**
```json
{
  "name": "string (required)",
  "agent_id": "string (optional)",
  "bonfire_id": "ObjectId-string (optional, recommended)",
  "labels": ["string (optional)"],
  "attributes": {}
}
```

**Response (200):**
```json
{
  "success": true,
  "entity_uuid": "660e8400-...",
  "message": "Entity created"
}
```

#### GET /knowledge_graph/entity/{uuid}
Auth required. Get entity by UUID.

#### POST /knowledge_graph/entities/batch
Auth required. Batch fetch entities.

#### GET /knowledge_graph/episode/{uuid}
Auth required. Get episode by UUID.

#### POST /knowledge_graph/agents/{agent_id}/episodes/search
Auth required. Search agent-scoped episodes.

**Request body:**
```json
{
  "limit": 20,
  "after_time": "ISO-8601 (optional)",
  "before_time": "ISO-8601 (optional)"
}
```

**Response (200):**
```json
{
  "success": true,
  "query": "string",
  "episodes": [{}],
  "entities": [{}],
  "edges": [{}],
  "num_results": 0,
  "bonfire_ids": ["string"],
  "agent_context": {},
  "graph_id": "string | null"
}
```

#### GET /knowledge_graph/agents/{agent_id}/episodes/latest
Auth required. Returns the agent's most recent episodes.

#### POST /knowledge_graph/episodes/expand
Auth required. Expand episodes with connected nodes.

**Request body:**
```json
{
  "episode_uuid": "string (optional, single mode)",
  "episode_uuids": ["string (optional, batch mode, max 50)"],
  "bonfire_id": "ObjectId-string (required)",
  "limit": 50
}
```

**Response (200):**
```json
{
  "success": true,
  "episodes": [{}],
  "nodes": [{}],
  "edges": [{}],
  "graph_id": "string | null",
  "num_results": 0
}
```

#### POST /knowledge_graph/expand/entity
Auth required. Expand entity with connected nodes and edges. Same response shape as episodes/expand.

**Request body:**
```json
{
  "entity_uuid": "string (required)",
  "bonfire_id": "ObjectId-string (required)",
  "limit": 50
}
```

#### POST /knowledge_graph/add_triples
Auth required. Batch add triples.

#### POST /graph/load
No auth. Load persisted graph state by hash.

#### GET /api/agents/{agent_id}/graphs
Auth required. List agent's saved graphs.

---

### Agent

#### POST /agents/{agent_id}/stack/add
Auth required. Add message(s) to agent stack.

**Request body (single):**
```json
{
  "message": {
    "text": "string",
    "userId": "string",
    "chatId": "string",
    "timestamp": "ISO-8601"
  }
}
```

**Request body (paired):**
```json
{
  "messages": [
    { "text": "string", "userId": "string", "chatId": "string", "timestamp": "ISO-8601" },
    { "text": "string", "userId": "string", "chatId": "string", "timestamp": "ISO-8601" }
  ],
  "is_paired": true
}
```

**Response (200):**
```json
{
  "success": true,
  "message_ids": ["string"],
  "message_count": 1,
  "is_paired": false,
  "stack_count": 5
}
```

#### GET /agents/{agent_id}/stack/process
Auth required. Triggers background stack processing.

**Response (200):**
```json
{
  "success": true,
  "message_count": 3,
  "initiated_at": "ISO-8601",
  "episode_id": "string | null",
  "warning": false,
  "warning_message": "string | null",
  "time_remaining_seconds": 120
}
```

#### POST /agents/register
Auth required. Register agent to bonfire.

**Request body:**
```json
{
  "agent_id": "string (required, ObjectId or username)",
  "bonfire_id": "string (required, ObjectId or name)"
}
```

**Response (200):**
```json
{
  "success": true,
  "agent_id": "ObjectId-string",
  "bonfire_id": "ObjectId-string",
  "message": "Agent registered to bonfire"
}
```

#### POST /agents/unregister
Auth required. Unregister agent from bonfire.

#### POST /agents
No auth. Create a new agent.

#### GET /agents/{agent_id}
Auth required. Get agent config/state.

#### PUT /agents/{agent_id}
Auth required. Update agent.

#### GET /agents
No auth. List agents with filters.

#### GET /agents/{agent_id}/bonfire
Auth required. Get agent's bonfire.

#### GET /agents/{agent_id}/latest_episode
Auth required. Get latest episode UUID.

#### POST /agents/{agent_id}/chat
Auth required. Chat with agent.

**Request body:**
```json
{
  "message": "string (required)",
  "chat_history": [
    { "role": "user | assistant | system", "content": "string", "timestamp": "ISO-8601 (optional)" }
  ],
  "center_node_uuid": "string (optional)",
  "graph_mode": "adaptive | static | regenerate | append (default: adaptive)",
  "graph_id": "string (optional, load persisted graph)",
  "context": {}
}
```

**Response (200):**
```json
{
  "reply": "Agent's response message",
  "graph_action": "static | regenerate | append",
  "search_prompt": "string | null",
  "graph_data": {},
  "graph_operation": {},
  "new_graph_id": "string | null",
  "errors": [],
  "htn_status": {}
}
```

#### POST /agents/{agent_id}/chat/stream
Auth required. Stream chat responses. Same request body as `/chat`, returns SSE stream.

---

### Content

#### POST /ingest_content
Auth required.

**Request body:**
```json
{
  "bonfire_id": "ObjectId-string (required)",
  "content": "string (required)",
  "source": "string (optional)"
}
```

**Response (200):**
```json
{
  "success": true,
  "bonfire_id": "ObjectId-string",
  "document_id": "ObjectId-string | null",
  "message": "string | null"
}
```

#### POST /generate_summaries
Auth required.

**Request body:** `{ "bonfire_id": "ObjectId-string" }`

**Response (200):**
```json
{ "status": "started", "job_id": "uuid-string" }
```

#### GET /bonfire/{bonfire_id}/labeled_chunks
Auth required. Returns labeled chunks for a bonfire by taxonomy.

#### GET /bonfires
No auth. Lists all bonfires (public metadata).

---

### Vector Store

#### POST /vector_store/search
Auth required. Search chunks in Weaviate by semantic similarity.

**Request body:**
```json
{
  "bonfire_id": "ObjectId-string or bonfire name (required)",
  "search_string": "string (required)",
  "taxonomy_refs": ["ObjectId-string (optional filter)"],
  "limit": 10
}
```

**Response (200):**
```json
{
  "success": true,
  "results": [
    {
      "content": "Chunk text content...",
      "similarity_score": 0.87,
      "metadata": { "source": "document-title", "chunk_index": 3 }
    }
  ],
  "count": 5,
  "query": "search string"
}
```

#### POST /vector_store/add_chunk
Auth required. Add labeled chunk(s) to vector store.

**Request body:**
```json
{
  "content": "string (required)",
  "label": "string (required, taxonomy label)",
  "bonfire_id": "string (default: 'default')",
  "doc_id": "string (optional)",
  "chunk_id": "string (optional)",
  "label_id": "string (optional)",
  "run_id": "string (default: 'manual')",
  "confidence": 1.0
}
```

**Response (200):**
```json
{
  "success": true,
  "message": "Chunk added",
  "chunks_processed": 1
}
```

#### POST /vector_store/setup
No auth. Setup Weaviate collections.

#### POST /vector_store/update_labels
Auth required. Update labels for a run. Body: `{ "bonfire_id": "ObjectId-string" }`.

#### POST /vector_store/search_label
Auth required. Search for matching labels.

#### GET /vector_store/chunks/{bonfire_id}
Auth required. Get labeled chunks from store.

#### POST /vector_store/clear_chunks
Auth required. Clear chunks for a bonfire. Body: `{ "bonfire_id": "ObjectId-string" }`.

---

### Taxonomy

#### POST /trigger_taxonomy
Admin auth required. Body: `{ "bonfire_id": "ObjectId-string" }`.

**Response (200):**
```json
{ "status": "started", "job_id": "uuid-string" }
```

#### POST /label_chunks
Admin auth required. Labels chunks with current taxonomy. Body: `{ "bonfire_id": "ObjectId-string" }`.

#### POST /labeling/hybrid
Admin auth required. Vector matching + LLM fallback labeling. Body: `{ "bonfire_id": "ObjectId-string" }`.

**Response (200):**
```json
{ "job_id": "uuid-string", "status": "started" }
```

#### POST /taxonomy/resolve_uuids
Admin auth required. Resolves Graphiti UUIDs for taxonomy IDs.

---

### Payments

#### POST /paid/agents/{agent_id}/chat
Payment-gated agent chat. Requires either `payment_header` (new payment) or `tx_hash` (existing microsub).

**Request body:** All fields from `/agents/{agent_id}/chat` plus:
```json
{
  "message": "string (required)",
  "chat_history": [],
  "payment_header": "base64-string (required if no tx_hash)",
  "tx_hash": "string (required if no payment_header)",
  "expected_amount": "1.00",
  "query_limit": 25,
  "expiration_days": 30,
  "dataroom_id": "ObjectId-string (optional, inherits settings from DataRoom)",
  "description": "string (optional)",
  "system_prompt": "string (optional)",
  "bonfire_id": "ObjectId-string (optional)"
}
```

**Response (200):** All fields from `/agents/{agent_id}/chat` response plus:
```json
{
  "reply": "Agent's response",
  "graph_action": "static",
  "payment": {
    "verified": true,
    "settled": true,
    "from_address": "0x...",
    "facilitator": "onchainfi",
    "tx_hash": "0x...",
    "settlement_error": null,
    "microsub_active": true,
    "queries_remaining": 24,
    "expires_at": "ISO-8601"
  },
  "htn_status": {}
}
```

#### POST /paid/agents/{agent_id}/delve
Payment-gated delve search. Same pattern as paid chat.

**Request body:** All fields from `/delve` plus `payment_header`, `tx_hash`, `expected_amount`, `query_limit`, `expiration_days`, `dataroom_id`.

**Response (200):** All fields from `/delve` response plus `payment` object (same shape as paid chat).

#### POST /microsubs
Create a microsub (pre-paid query subscription).

**Request body:**
```json
{
  "payment_header": "base64-string (required)",
  "agent_id": "string (optional, auto-resolved from dataroom if not provided)",
  "expected_amount": "1.00",
  "query_limit": 25,
  "expiration_days": 30,
  "dataroom_id": "ObjectId-string (optional, inherits settings)",
  "description": "string (optional)",
  "center_node_uuid": "string (optional)",
  "system_prompt": "string (optional)",
  "bonfire_id": "ObjectId-string (optional)"
}
```

**Response (200):**
```json
{
  "microsub": {
    "tx_hash": "0x...",
    "agent_id": "ObjectId-string",
    "query_limit": 25,
    "queries_used": 0,
    "queries_remaining": 25,
    "expires_at": "ISO-8601",
    "created_at": "ISO-8601",
    "created_by_address": "0x...",
    "is_expired": false,
    "is_exhausted": false,
    "is_valid": true,
    "description": "string | null",
    "center_node_uuid": "string | null",
    "system_prompt": "string | null",
    "bonfire_id": "string | null",
    "dataroom_id": "string | null"
  },
  "payment": {
    "verified": true,
    "settled": true,
    "tx_hash": "0x...",
    "from_address": "0x..."
  }
}
```

#### GET /microsubs
List microsubs by wallet. Query params: `wallet_address`, `agent_id`, `limit`.

#### GET /microsubs/{tx_hash}/htn
Get microsub HTN curriculum status.

---

### DataRooms & HyperBlogs

#### POST /datarooms/hyperblogs/purchase
Purchase and generate a hyperblog from a dataroom's knowledge graph.

**Request body:**
```json
{
  "payment_header": "base64-string (required)",
  "dataroom_id": "ObjectId-string (required)",
  "user_query": "string (required, 3-500 chars)",
  "is_public": true,
  "blog_length": "short | medium | long (default: medium)",
  "generation_mode": "blog | card (default: blog)",
  "expected_amount": "string (optional, defaults to dataroom price)"
}
```

**Response (200):**
```json
{
  "hyperblog": {
    "id": "ObjectId-string",
    "dataroom_id": "ObjectId-string",
    "user_query": "string",
    "author_wallet": "0x...",
    "preview": "First 200 characters...",
    "summary": "AI-generated 50-word summary",
    "word_count": 1200,
    "blog_length": "medium",
    "generation_status": "generating | completed | failed",
    "created_at": "ISO-8601",
    "is_public": true,
    "tx_hash": "0x...",
    "upvotes": 0,
    "downvotes": 0,
    "comment_count": 0,
    "view_count": 0,
    "taxonomy_keywords": ["keyword1", "keyword2"]
  },
  "payment": {
    "verified": true,
    "settled": true,
    "tx_hash": "0x...",
    "from_address": "0x..."
  }
}
```

**Note:** `generation_status` starts as `"generating"`. Poll `GET /datarooms/hyperblogs/{id}` until `"completed"` or `"failed"`.

#### GET /datarooms/hyperblogs
No auth. List public hyperblogs. Query params: `limit` (1-50), `offset`, `dataroom_id`, `bonfire_id`, `status`, `generation_mode`.

#### GET /datarooms/hyperblogs/{hyperblog_id}
No auth. Get a single hyperblog by ID. Full blog content included.

#### POST /datarooms/hyperblogs/{hyperblog_id}/vote
Auth required. Vote on a hyperblog.

#### GET|POST /datarooms/hyperblogs/{hyperblog_id}/comments
GET: no auth. POST: auth required.

#### POST /datarooms/hyperblogs/{hyperblog_id}/view
No auth. Record a view.

#### POST /datarooms/hyperblogs/{hyperblog_id}/banner
Auth required. Generate banner image.

#### POST /datarooms
Auth required. Create a dataroom.

#### GET /datarooms
No auth. List datarooms.

#### GET /datarooms/{dataroom_id}
No auth. Get a dataroom.

#### GET /datarooms/{dataroom_id}/contributions
Auth required. Get contribution stats.

#### GET /datarooms/{dataroom_id}/htn
Auth required. Get HTN curriculum.

#### GET /datarooms/{dataroom_id}/hyperblogs
No auth. List dataroom hyperblogs.

#### GET /datarooms/{dataroom_id}/microsubs
Auth required. List dataroom microsubs.

#### POST /datarooms/{dataroom_id}/preview
Auth required. Preview dataroom.

---

### Jobs

#### GET /jobs/{job_id}/status
Admin auth required.

**Response (200):**
```json
{
  "job_id": "uuid-string",
  "bonfire_ref": "ObjectId-string",
  "workflow_type": "taxonomy | labeling | summaries | daily_workflow | hybrid_labeling | knowledge_graph_ingest",
  "source": "endpoint | scheduler | api",
  "state": "running | completed | failed",
  "error": "string | null",
  "started_at": "ISO-8601",
  "completed_at": "ISO-8601 | null",
  "duration": 12.5,
  "metadata": {}
}
```

Terminal states: `completed`, `failed`. Keep polling while `running`.

#### GET /jobs
Admin auth required. Lists recent jobs. Query params: `bonfire_id`, `workflow_type`, `limit`.

#### GET /jobs/active
Admin auth required. Returns all currently running jobs.

---

## Job Polling

Many endpoints return `{"job_id":"..."}` for async work. Auth required.

**Important:** The job_id is generated before the background task saves to the database. A 404 in the first few seconds means "not yet saved" -- retry. A 404 persisting beyond ~15s means the background task failed.

```bash
TIMEOUT=300; ELAPSED=0; GRACE=15
while [ $ELAPSED -lt $TIMEOUT ]; do
  HTTP_CODE=$(curl -s -o /tmp/job_status.json -w "%{http_code}" \
    -H "Authorization: Bearer $DELVE_API_KEY" \
    "$BASE_URL/jobs/$JOB_ID/status")

  if [ "$HTTP_CODE" = "404" ] && [ $ELAPSED -lt $GRACE ]; then
    echo "[$ELAPSED s] waiting for job to register..."
    sleep 3; ELAPSED=$((ELAPSED+3)); continue
  elif [ "$HTTP_CODE" = "404" ]; then
    echo "Job never appeared — background task likely failed"; exit 1
  fi

  STATE=$(python3 -c "import sys,json;print(json.load(open('/tmp/job_status.json'))['state'])")
  echo "[$ELAPSED s] state=$STATE"

  case "$STATE" in
    completed) echo "Done"; cat /tmp/job_status.json; break ;;
    failed)    echo "FAILED"; cat /tmp/job_status.json; exit 1 ;;
    running)   sleep 5; ELAPSED=$((ELAPSED+5)) ;;
  esac
done
[ $ELAPSED -ge $TIMEOUT ] && echo "TIMEOUT after ${TIMEOUT}s" && exit 1
```

Job states: `running` (keep polling) | `completed` (success) | `failed` (check `error` field).

---

## Error Handling

| HTTP Code | Meaning | Action |
|-----------|---------|--------|
| 200/202 | Success | Extract response fields, continue |
| 403 | Auth failed | API key does not exist or is not linked to this bonfire/agent |
| 404 | Not found | Wrong job_id, agent_id, or endpoint path |
| 422 | Validation | Request body malformed -- log the response detail and fix the payload |
| 500 | Server error | Retry once after 5s. If still 500, escalate |

---

## Notes

- All IDs are MongoDB ObjectIds (24-char hex strings) unless noted.
- Timestamps are ISO-8601 UTC.
- Header `X-Bonfire-Id` overrides body `bonfire_id` on endpoints that accept both.


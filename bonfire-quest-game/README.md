# Bonfire Quest Game (x402 + Shared Bonfire)

This demo lets players:

1. Select an owned ERC-8004 bonfire NFT (owner becomes Game Master),
2. Buy and register a purchased agent (`feat/agent-purchase` flow),
3. Spend episode quota by taking turns,
4. Complete quests to earn episode recharges and continue playing.

## Endpoints

- `POST /game/agents/register-purchase`
- `POST /game/create` (create/replace one active game per bonfire using owner wallet + prompt)
- `POST /game/purchased-agents/reveal-nonce` (proxy for purchased-agent nonce message)
- `POST /game/purchased-agents/reveal-api-key` (proxy for wallet-signed API key reveal)
- `POST /game/agents/complete` (uses server `DELVE_API_KEY` to call Delve chat + append paired stack messages)
- `POST /game/agents/process-stack` (explicit stack execution; not automatic)
- `POST /game/agents/gm-react` (manual GM reaction pass from latest/selected episode)
- `POST /game/world/generate-episode` (publish GM world-state update as world episode)
- `POST /game/stack/process-all` (processes stacks for all registered agents)
- `POST /game/player/restore` (restore player agents by wallet and optional purchase tx hash)
- `POST /game/quests/claim`
- `POST /game/turn`
- `POST /game/agents/recharge` (owner only)
- `GET /game/state?bonfire_id=...`
- `GET /game/feed?bonfire_id=...`
- `GET /game/list-active` (list active games for player discovery)
- `GET /game/details?bonfire_id=...` (game metadata + active agents + activity log)
- `GET /game/bonfire/pricing?bonfire_id=...` (proxy to Delve bonfire pricing for auto x402 amount)
- `GET /game/config` (runtime config for UI, including ERC-8004 registry)
- `GET /game/wallet/provision-records?wallet_address=...` (wallet provision records; UI performs ownerOf checks like unified-webapp)
- `GET /game/wallet/bonfires?wallet_address=...` (server-side owner-verified bonfire discovery)
- `GET /game/wallet/purchased-agents?wallet_address=...&bonfire_id=...` (list wallet purchases for selected bonfire)
- `GET /game/stack/timer/status`
- `POST /game/purchase-agent/{bonfire_id}` (proxy to existing purchase endpoint)

## Environment

Optional `.env` values:

- `PORT` (default: `9997`)
- `DELVE_BASE_URL` (default: `http://localhost:8000`)
- `DELVE_API_KEY` (required for completion/stack calls)
- `QUEST_CLAIM_COOLDOWN_SECONDS` (default: `60`)
- `STACK_PROCESS_INTERVAL_SECONDS` (default: `120`)
- `PAYMENT_NETWORK` (default: `base`)
- `PAYMENT_SOURCE_NETWORK` (default: `PAYMENT_NETWORK`)
- `PAYMENT_DESTINATION_NETWORK` (default: `PAYMENT_NETWORK`)
- `PAYMENT_TOKEN_ADDRESS` (default: Base USDC `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913`)
- `PAYMENT_CHAIN_ID` (default: `8453`)
- `PAYMENT_DEFAULT_AMOUNT` (default: `0.01`)
- `ONCHAINFI_INTERMEDIARY_ADDRESS` (optional override; if omitted UI uses unified mapping, e.g. `base->base`)

## Run

```bash
pip install -r requirements.txt
python server.py
```

Open `http://localhost:9997`.

## Notes

- Storage is in-memory for fast experimentation.
- The demo UI includes wallet connect (`window.ethereum`) and uses the connected wallet address for ownership-linking requests.
- Game lifecycle is one active game per bonfire. Creating a new game archives the previous active game for that bonfire.
- The demo UI can load owned bonfires directly from wallet (`Load Owned Bonfires`) by fetching provision records then calling `ownerOf` via wallet provider, matching unified-webapp behavior.
- Explicit `/game/bonfire/link` calls are no longer required in normal flow; ownership context is auto-linked during `register-purchase`.
- Agent buying is available in UI via `POST /game/purchase-agent/{bonfire_id}`; after success it auto-fills purchase fields for registration.
- Owner flow: create a game from prompt using `/game/create`; server seeds an initial episode event and initial quests.
- Player flow: choose active game from `/game/list-active`, buy slot, register purchase, chat/process stack, and receive GM auto extension decisions.
- Returning players can restore agents by wallet and purchase tx hash via `/game/player/restore`.
- UI can load previously purchased agents for the connected wallet in the selected bonfire and auto-fill registration/chat agent fields.
- UI supports revealing purchased agent API key from `purchase_id`, saving it to browser local storage, and auto-sending it as `X-Agent-Api-Key` on game POST requests.
- Buy flow is user-friendly: click `Build x402 Header` (wallet signs ERC-3009 typed data like unified-webapp), then `Buy Agent`. If header is empty, `Buy Agent` auto-builds it first.
- For fewer payment mismatches, leave `payment amount` blank and the UI auto-calculates `episodes_requested * price_per_episode` from bonfire pricing.
- `register-purchase` validates `purchase_id` through existing purchased-agent reveal endpoints.
- Quota usage is enforced in this demo via turn accounting and can be recharged by quest rewards.
- `POST /game/agents/complete` intentionally does **not** process stack so users can build multi-message context before running `process-stack`.
- Game Master context is updated only when stack processing produces an episode (manual `process-stack` or timed `process-all`), keeping state lean.
- During `process-stack`, server also performs an automatic GM decision (`extension_awarded` + reaction) and can recharge the agent quota.
- You can also trigger GM progression manually in two explicit steps: `gm-react` then `world/generate-episode`.
- No explicit quest creation is required for gameplay: bonfire owner can set `as_game_master=true` on completion and the completion auto-generates a quest from the GM agent response.
- A background timer processes all registered agent stacks on `STACK_PROCESS_INTERVAL_SECONDS`; use `/game/stack/timer/status` to inspect recent runs.

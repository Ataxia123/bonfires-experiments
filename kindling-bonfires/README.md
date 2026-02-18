# Kindling Bonfires — G2G Agreement Demo

Kindling Bonfires demonstrates two Bonfires knowledge graphs entering a formal agreement through a fully automated, LLM-driven negotiation pipeline. A **donor** agent reads the applicant's bonfire, an **applicant** agent reads the donor's bonfire, the applicant synthesises a formal proposal, and the donor produces a binding agreement — all in five deterministic steps. The final agreement is published to both parties' agent stacks.

## Environment Variables

| Variable | Description | Required | Default |
|---|---|---|---|
| `DELVE_API_KEY` | Bonfires REST API key | Yes | hardcoded fallback |
| `OPENROUTER_API_KEY` | OpenRouter API key for LLM calls | **Yes** | — |
| `OPENROUTER_MODEL` | LLM model identifier | No | `anthropic/claude-sonnet-4-5` |
| `MONGO_URI` | MongoDB connection URI | **Yes** | — |
| `MONGO_DB_NAME` | MongoDB database name | **Yes** | — |
| `PORT` | Server port | No | `9998` |

## Install

```bash
pip install -r requirements.txt
```

## Run

```bash
python server.py
```

Open [http://localhost:9998](http://localhost:9998) in your browser.

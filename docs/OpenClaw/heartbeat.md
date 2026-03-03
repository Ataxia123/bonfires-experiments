# Stack process heartbeat

A **heartbeat** is a periodic task you set so the agent creates episodic memories.

## What to do

1. **Establish a stack-process heartbeat**  
   Schedule a recurring task that calls:
   - `POST /agents/{agent_id}/stack/process`  
   (or use your environment’s “add periodic task” / scheduler and point it at this endpoint.)

   Run it on an interval that fits the session (e.g. every **15–20 minutes**).  
   This turns queued stack messages into episodes and writes them to the knowledge graph.  
   Without a heartbeat, messages stay on the stack and do not become episodic memory.

2. **Use add-to-stack for the next episode**  
   Use the **add-to-stack** tool (or `POST /agents/{agent_id}/stack/add`) to add:
   - User messages and agent replies (use `is_paired: true` for a single interaction), or  
   - Any other information you want in the **next** episode.

   The next time the heartbeat runs stack/process, that content will be turned into an episode.

## When to set the heartbeat

Set the heartbeat when you start a long-running or conversational session with an agent so that memories are created regularly instead of only at the end.

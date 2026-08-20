# EMI-Agent backend

FastAPI and LangGraph backend for the public synthetic-data demonstration. The default
provider is deterministic fixture replay. Set `DASHSCOPE_API_KEY` only in the process
environment and select the `dashscope` provider to run the live OpenAI-compatible adapter.

```powershell
uv sync --locked
uv run uvicorn app.main:app --reload
```

No secret, chain-of-thought, or provider endpoint is persisted in checkpoints or run records.

Planner and Evidence model inputs contain only the case description and public tool schemas.
They never receive `required_checks`, source identifiers, tool payloads, evidence tags, or bound
arguments. After a tool is selected, the runtime deterministically binds the unique matching
`tool_data` entry; payload and tags become visible to downstream agents only after successful
execution. The exported plan is therefore the bound system plan, not a claim that the model
guessed hidden selectors.

The durable evaluation runner appends and fsyncs every result. Normal runs alternate variant
order by case; fault replays are isolated by `source_run_id` and never receive a live label.

```powershell
uv run python -m app.evaluation_runner `
  --cases data/dev_cases.jsonl `
  --output .runtime/dev-runs.jsonl `
  --recordings .runtime/dev-recordings.jsonl `
  --provider fixture

uv run python -m app.evaluation_runner `
  --cases data/dev_cases.jsonl `
  --output .runtime/dev-fault-runs.jsonl `
  --faults data/dev_fault_overlays.jsonl `
  --recordings-replay .runtime/dev-recordings.jsonl `
  --source-runs .runtime/dev-runs.jsonl
```

# Agent Society

Agent Society is a small multi-agent world simulation. Residents begin unnamed and without an assigned occupation or supplied culture; they encounter the world through sensory descriptions, their own actions, memories, and conversations. One resident receives a private self-serving behavioral bias. The human-only viewer marks that resident; the marker and designation are not included in other residents' observations. A browser-based 3D viewer lets you inspect and replay the world.

## Requirements

- Python 3.10 or newer
- No third-party Python packages are required for the core application
- Internet access for hosted model APIs and for the viewer's Three.js CDN dependency

## Quick start

Run an offline simulation without an API key:

```bash
python agent_society.py --brain scripted --ticks 300 --no-viewer
```

Run the simulation with a hosted model provider (NVIDIA NIM is the default example):

1. Copy `.env.example` to `.env` if you use a tool that loads dotenv files, or export the desired environment variables in your shell. The application itself reads environment variables and does not automatically load `.env`.
2. Set your provider's API key as an environment variable.
3. Optionally check the provider connection.
4. Start the simulation and viewer.

For example, on macOS/Linux:

```bash
export NVIDIA_API_KEY="your-key-here"
python agent_society.py --check --brain nim
python agent_society.py --brain nim --forever
```

On PowerShell:

```powershell
$env:NVIDIA_API_KEY = "your-key-here"
python agent_society.py --check --brain nim
python agent_society.py --brain nim --forever
```

The viewer is served at [http://127.0.0.1:8000](http://127.0.0.1:8000) by default. The viewer loads Three.js from a CDN, so the browser needs internet access for the 3D rendering. Scroll to zoom, drag to orbit, and shift-drag or right-drag to pan. The resident list marks the designated agent with a red **hidden trait** label visible only in the human viewer.

## Model providers

The script supports these brain options:

- `nim`
- `groq`
- `openrouter`
- `mistral`
- `cerebras`
- `openai` (including compatible endpoints)
- `gemini`
- `claude`
- `ollama` (local server)
- `scripted` (offline simulation/testing)

Provider credentials are read from environment variables. Common names include `NVIDIA_API_KEY`, `GROQ_API_KEY`, `OPENROUTER_API_KEY`, `MISTRAL_API_KEY`, `CEREBRAS_API_KEY`, `OPENAI_API_KEY`, and `GEMINI_API_KEY` (or `GOOGLE_API_KEY`). Claude and Ollama settings are described in the script's usage text and provider implementation. Never commit real API keys.

To inspect supported options:

```bash
python agent_society.py --help
python agent_society.py --list-models nim
```

The model-list and connection-check commands make real provider requests and may consume API quota.

## Useful commands

```bash
# A short, offline smoke test
python agent_society.py --brain scripted --ticks 300 --no-viewer

# Start the world and browser viewer
python agent_society.py --brain nim --forever

# Use different models for residents A, B, C, and D
python agent_society.py --lineup "nim:model-a" "nim:model-b" "nim:model-c" "nim:model-d"

# View or replay an existing saved state without advancing the world
python agent_society.py --view-only --state state_village

# Limit model calls to help manage usage
python agent_society.py --brain nim --daily-calls 30 --max-calls 300 --forever
```

Run `python agent_society.py --help` for the full list of options.

## Saved state

Each fresh run gets a unique folder under `runs/`, named with its start time and seed. The run writes continuously, rather than waiting until shutdown:

- `state.json` — resumable world state, atomically replaced after each world hour
- `events.jsonl` — append-only world event stream, including viewer snapshots
- `snapshots.jsonl` — append-only per-hour world snapshots
- `thoughts.jsonl` — private model thoughts, one record per turn
- `chronicle.md` — readable event chronicle
- `agents/<A-D>/history.jsonl` — that resident's observations, response, and private thought, flushed per turn

To resume a run, pass its folder explicitly, for example `python agent_society.py --brain scripted --forever --state runs/20261013_120000_123456_seed7`. To replay without advancing time, use `python agent_society.py --view-only --state runs/20261013_120000_123456_seed7`. Run output is intentionally ignored by Git so large histories and private thoughts are not accidentally committed.

## Configuration

The script reads optional tuning values from environment variables, including:

- `AGENT_RPM`
- `AGENT_TPM`
- `AGENT_MAX_TOKENS`
- `AGENT_TEMPERATURE`
- `AGENT_TIMEOUT`
- `AGENT_REASONING_EFFORT`
- Provider-specific base URL settings such as `GROQ_BASE_URL`

See the provider configuration in `agent_society.py` for defaults and additional settings.

## Tests

Run the lightweight offline checks with:

```bash
python -m unittest discover -s tests -v
```

The tests do not require provider credentials. They also check that run histories are created and that the hidden marker appears in viewer snapshot data but not in another resident's observation.

## Security notes

- Keep API keys in environment variables or a secret manager; never put them in source code, commits, issue reports, or screenshots.
- Treat any token pasted into a chat or public repository as compromised and rotate it.
- The viewer binds to `127.0.0.1` by default. Only use `--host 0.0.0.0` on a trusted network when you intentionally want other devices to connect.

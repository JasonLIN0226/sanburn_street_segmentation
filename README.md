# Street segmentation

An agent workflow for labeling streets on historical maps. The local UI lets you select maps, run jobs sequentially or in parallel, follow the recorded workflow, and inspect or download results. The default agent uses OpenAI's `gpt-6.1-sol` with high reasoning effort.

## 1. Set up the environment

The current runner requires macOS and uses `sandbox-exec`. Clone this repository inside your home directory. From the repository root, create the Python environment in its parent directory and install the dependencies:

```bash
python3 -m venv ../.venv
../.venv/bin/python -m pip install -r requirements.txt
```

The runner and UI worker expect `../.venv/bin/python`. The sandbox restricts access within the home directory; it is not a general restriction on all paths outside the home directory.

## 2. Add your OpenAI API key

Either set the `OPENAI_API_KEY` environment variable, or create a private file at `secrets/openai_api_key.txt` and put only your key in it:

```bash
mkdir -p secrets
chmod 700 secrets
touch secrets/openai_api_key.txt
chmod 600 secrets/openai_api_key.txt
```

The environment variable takes precedence. `secrets/`, `.env` files, UI state, and run outputs are ignored by Git. Never commit or share your API key. Running the workflow sends map images to OpenAI and incurs API charges.

## 3. Add maps

Put maps in `maps/<region>/`, for example:

```text
maps/
  new_york/2.tif
  chicago/01.tif
  chicago/02.jpg
```

The UI scans `maps/` recursively for `.tif`, `.tiff`, `.jpg`, `.jpeg`, `.png`, and `.jp2` files, subject to local image-format support. The first folder under `maps/` becomes the region. New region names are shown as their folder names. After adding or removing files, refresh the browser page to update the map list.

The repository includes one sample map, `maps/atlanta/304.jpg`. Other maps placed in `maps/` are ignored by Git and remain local.

## 4. Run the UI

Double-click `openUI.command`, or run:

```bash
../.venv/bin/python ui/server.py
```

Open [http://127.0.0.1:8765/](http://127.0.0.1:8765/), select maps, choose concurrency, and start the workflow. Select a task to see its progress, original image, overlay, mask, recorded token use, elapsed time, and estimated cost. Estimates are not account invoices. The process view shows saved updates, not token-by-token streaming.

Results and logs are saved in `runs/<case>/`, including `source.png`, `mask.png`, `overlay.jpg`, `summary.json`, and usage records. `Hide` keeps files; `Delete files` permanently removes a selected inactive case after confirmation. Closing the browser does not itself stop launched jobs.

## Optional: run from the terminal

Use a new map ID and run ID for each case. Setup is local; `run` makes paid API requests. For the sample map:

```bash
../.venv/bin/python runner.py setup --map ATLANTA_304 --source maps/atlanta/304.jpg --run-id first
../.venv/bin/python runner.py run --map ATLANTA_304 --run-id first
```

Temporary HTTP 429 rate limits are retried with backoff within configured limits. Some verified 429 failures can be resumed with `runner.py resume`; other failures may not be safely resumable.

## Offline checks

```bash
../.venv/bin/python -m unittest discover -s tests -v
```

Tests use local fixtures and do not establish segmentation accuracy. Review actual results against the source map or independent labels.

For this English publication, seven tool-returned instruction messages were translated from the local workflow; segmentation logic was not changed. The translations have not been assessed in a new paid model run.

## Code structure

- `runner.py` prepares independent cases and coordinates the Responses API, local tools, retries, recovery, and coverage audit. `build_response_request` defines the API request; `execute_tool_call` dispatches the agent's tool calls.
- `workflow/prompt.txt` defines the agent's labeling and review instructions. `workflow/conditional.py`, `workflow/core.py`, and `workflow/src/` implement review planning, rendering, and boundary processing.
- `ui/server.py` serves the local interface, while `ui/worker.py` schedules each independent run. `ui/common.py` builds the map and job catalogs; `ui/storage.py` provides file helpers; `ui/usage.py` estimates API usage and cost; `ui/workflow_view.py` reconstructs saved progress.
- `tests/` contains offline checks, including the agent/tool request protocol, recovery, scheduling, UI, and a synthetic mask build. These checks do not validate segmentation quality.

Each case under `runs/` stores its source, copied workflow tools, model responses, tool results, and review artifacts. Recovery checks saved source and tool identities before resuming a confirmed rate-limit failure. Treat the workflow files and prompt as behavior-bearing: changes to them may affect reproducibility and recovery of older cases.

For formatting changes to the runner, UI, and tests, the project includes configuration for Black and isort. If those development tools are installed, run `black --check runner.py ui tests` and `isort --check-only runner.py ui tests`. Workflow files are excluded from default formatting because historical recovery checks depend on their exact contents.

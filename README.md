# Street segmentation

An agent workflow for labeling streets on historical maps. The local UI lets you select maps, run jobs sequentially or in parallel, follow the recorded workflow, and inspect or download results. The default agent uses OpenAI's `gpt-6.2-sol` with medium reasoning effort.

## 1. Set up the environment

The current runner requires macOS and Python 3.11 or newer. It uses `sandbox-exec`. Clone this repository inside your home directory. From the repository root, create the Python environment in its parent directory and install the dependencies:

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

Open [http://127.0.0.1:8765/](http://127.0.0.1:8765/), select maps, choose a workflow mode and concurrency, and start the workflow. Select a task to see its progress, original image, overlay, mask, recorded token use, elapsed time, and estimated cost. Estimates are not account invoices. The process view shows saved updates, not token-by-token streaming.

- **Initial Mask** is the default, using the November 22 independent-path revision (`scoped_paths_20261122`) with 6.2 Sol and medium reasoning. The agent assesses dashed minor branches outside the parent street, traces the full supported width of established streets, and carries confirmed sheet exits to the mapped edge. The first successful build with complete source coverage exports the mask, overlay and LabelMe annotation. An invalid submission returns a tool error and can take another model call to correct.
- **MAX Mode** adds visual review, revisions where needed, and final checks before exporting the selected mask and overlay.

Results and logs are saved in `runs/<case>/`, including `source.png`, `mask.png`, `overlay.jpg`, `summary.json`, and usage records. `Hide` keeps files; `Delete files` permanently removes a selected inactive case after confirmation. Closing the browser does not itself stop launched jobs.

Both modes export `labelme.json` beside the selected `mask.png`. The JSON references `source.png` in the same directory. It stores all foreground pixels in one native `st1` mask, including disconnected streets and interior gaps. An empty result has an empty shape list. In the results UI, choose **Download LabelMe JSON** and download **source.png** from the Source tab into the same directory.

Initial Mask records possible gaps at street merges in `summary.json` under `review_locations`. Each finding links to a side-by-side crop showing the original on the left and the submitted mask overlay on the right. This local check looks for converging inferred banks, a visible block end and an excluded footprint inside the gap. It runs during export without another model request.

If you confirm a gap against the original and already have a second Initial Mask of that scan, you can apply its additions at just the reviewed merge. For the first listed location, run `../.venv/bin/python apply_junction_review.py BASE_CASE SECOND_CASE corrected.png --location 1`. The command writes a new PNG, preserves existing street pixels and protects the block face and inventoried exclusions.

## Optional: run from the terminal

Use a new map ID and run ID for each case. Setup is local; `run` makes paid API requests. For the sample map:

```bash
../.venv/bin/python runner.py setup --map ATLANTA_304 --source maps/atlanta/304.jpg --run-id first
../.venv/bin/python runner.py run --map ATLANTA_304 --run-id first
```

This starts in Initial Mask mode. To run the full review workflow, set `--mode max` on the `setup` command. The chosen mode is saved with the case.

Temporary HTTP 429 rate limits are retried with backoff within configured limits. Some verified 429 failures can be resumed with `runner.py resume`; other failures may not be safely resumable.

## Edit a mask in LabelMe

Open the exported JSON in LabelMe to review the mask. To paint or erase street pixels, install the optional editor in a separate environment. The brush uses LabelMe 7.11.0. From the repository root:

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install 'labelme==7.11.0'
./openLabelMeBrush.command "runs/YOUR_CASE/labelme.json"
```

Replace `YOUR_CASE` with your run directory. Double-clicking the launcher also opens the editor. If you already installed the pinned version in another environment, set `LABELME_PYTHON` to its Python executable. To export an older run separately, run `../.venv/bin/python export_labelme.py "runs/YOUR_CASE"`. This creates a review copy under `labelme/` and preserves any existing review.

## Offline checks

```bash
../.venv/bin/python -m unittest discover -s tests -v
```

Tests use local fixtures and do not establish segmentation accuracy. Review actual results against the source map or independent labels.

## Code structure

- `runner.py` prepares independent cases and coordinates the Responses API, local tools, retries, recovery, and coverage audit. `build_response_request` defines the API request; `execute_tool_call` dispatches the agent's tool calls.
- For new runs, `record_source_batch` saves observations and returns the next group of original images. `submit_roads` merges the saved notes with the agent’s polygons and renders the first mask. Both modes share these steps. In Initial Mask mode the agent requests native detail crops by coordinates through `view_source_details`. MAX also exposes the command tool for subsequent review.
- `workflow/street_policy.txt` defines which routes qualify and where to trace their banks. `workflow/prompt.txt` supplies the Initial Mask and MAX instructions. New cases copy their tools, renderer and API tool definitions from `workflow/`. Previously prepared cases retain their own tools and configuration.
- `ui/server.py` serves the local interface, while `ui/worker.py` schedules each independent run. `ui/common.py` builds the map and job catalogs; `ui/storage.py` provides file helpers; `ui/usage.py` estimates API usage and cost; `ui/workflow_view.py` reconstructs saved progress.
- `tests/` contains offline checks, including the agent/tool request protocol, recovery, scheduling, UI, and a synthetic mask build. These checks do not validate segmentation quality.

Each case under `runs/` stores its source, copied workflow tools, model responses, tool results, and review artifacts. Recovery checks saved source and tool identities before resuming a confirmed rate-limit failure. Treat the workflow files and prompt as behavior-bearing: changes to them may affect reproducibility and recovery of older cases.

For formatting changes to the runner, UI, and tests, the project includes configuration for Black and isort. If those development tools are installed, run `black --check runner.py ui tests` and `isort --check-only runner.py ui tests`. Workflow files are excluded from default formatting because historical recovery checks depend on their exact contents.

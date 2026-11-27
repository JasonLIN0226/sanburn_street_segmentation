#!/bin/zsh
set -eu
repo_dir="${0:a:h}"
editor_python="${LABELME_PYTHON:-$repo_dir/.venv/bin/python}"
if [[ -z "${LABELME_PYTHON:-}" && ! -x "$editor_python" ]] && command -v uv >/dev/null; then
    editor_python="$(uv tool dir)/labelme/bin/python"
fi
if [[ ! -x "$editor_python" ]]; then
    print -u2 "Install LabelMe 7.11.0 as described in the README or set LABELME_PYTHON."
    exit 1
fi
exec "$editor_python" "$repo_dir/labelme_brush.py" "$@"

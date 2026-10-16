#!/usr/bin/env bash
# Render every view in docs/architecture/workspace.dsl to docs/architecture/<view>.svg.
#
#   scripts/render_architecture.sh
#
# Structurizr exports the views to PlantUML, then PlantUML renders each one to SVG.
# Both images are pinned by digest: the same digests always produce byte-identical
# SVGs, which is what lets CI detect a model and its pictures drifting apart.
# Needs only Docker. Works in Git Bash on Windows and on Linux.
set -euo pipefail

STRUCTURIZR_IMAGE="structurizr/structurizr@sha256:721136283c2f9cf1ba69037bc9de136c579d66fdcb2d771cb60546ec68def1a5"  # 6.2.3
PLANTUML_IMAGE="plantuml/plantuml@sha256:d08610df482510844382caa4e016ba2bf7e3231f630f02ee12f250f3416c62b1"  # 1.2026.8

repo_root="$(git rev-parse --show-toplevel)"
arch_dir="$repo_root/docs/architecture"

if ! docker info >/dev/null 2>&1; then
    echo "render_architecture: Docker is not running; start it and retry." >&2
    exit 1
fi

work_dir="$(mktemp -d)"
# On Linux the export's files are root-owned; a failed cleanup must not fail the run.
trap 'rm -rf "$work_dir" 2>/dev/null || true' EXIT
cp "$arch_dir/workspace.dsl" "$work_dir/"

# Docker on Windows needs a Windows path for the bind mount, and Git Bash must not
# rewrite the container-side paths.
mount_dir="$work_dir"
if command -v cygpath >/dev/null 2>&1; then
    mount_dir="$(cygpath -m "$work_dir")"
fi

MSYS_NO_PATHCONV=1 docker run --rm \
    -v "$mount_dir:/usr/local/structurizr" \
    "$STRUCTURIZR_IMAGE" export \
    -workspace /usr/local/structurizr/workspace.dsl \
    -format plantuml \
    -output /usr/local/structurizr/plantuml >/dev/null

rm -f "$arch_dir"/*.svg
for puml in "$work_dir"/plantuml/structurizr-*.puml; do
    view="$(basename "$puml" .puml)"
    view="${view#structurizr-}"
    case "$view" in *-key) continue ;; esac  # legends: not published
    docker run --rm -i "$PLANTUML_IMAGE" -tsvg -pipe <"$puml" >"$arch_dir/$view.svg"
    echo "rendered docs/architecture/$view.svg"
done

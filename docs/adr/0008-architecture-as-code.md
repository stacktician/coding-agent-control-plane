# 0008. Architecture as code: a C4 model with generated, drift-checked views

Status: Accepted

## Context

The system will keep growing. Hand-drawn diagrams drift from the code and from each other,
and every change means laying them out again by hand.

## Decision

- One C4 model in Structurizr DSL, `docs/architecture/workspace.dsl`, is the source of truth
  for every view: context, containers, components, the issue-to-PR sequence and deployment.
- `scripts/render_architecture.sh` renders the views to SVG through Structurizr and PlantUML
  in Docker. Both images are pinned by digest, so the output is byte-identical.
- A pre-commit hook re-renders whenever the model is committed; a CI workflow fails if the
  committed SVGs are stale.

## Alternatives

- **draw.io or Excalidraw:** manual layout, and drift with every change.
- **Mermaid only:** renders natively on GitHub, but each diagram is separate, with no shared
  model to keep them consistent.
- **LikeC4:** architecture as code with live preview, but it does not enforce C4's levels out
  of the box.

## Consequences

- Views cannot disagree with each other, and a diagram cannot silently go stale.
- Automatic layout is less polished than a hand-placed diagram.
- Rendering needs Docker. Structurizr is a small project; the model is plain text in a
  standard notation, so it can move to another tool.

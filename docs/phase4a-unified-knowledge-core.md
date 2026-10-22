# Phase 4A — Unified Knowledge Core (initial implementation)

## Goal

Preserve human and AI experiences without turning every note into a task, without
silently granting authority to an agent, and without adding Notion, embeddings, or
model calls to the reliable coordinator execution path.

The existing \`experience.py\` and \`insights.py\` remain supported. This initial
core offers a stable, independent contract for future Human Workspace, ai-memory,
and Notion adapters; migrating previous entries is **not** part of this change.

## Source of truth and identities

- The coordinator's local SQLite/WAL is the authoritative operational store.
- An entry has a stable ID, project/task scope, kind, content, author, visibility,
  provenance (source type and optional source ID), tags, context, evidence, status,
  creation timestamp, and monotonically increasing revision.
- \`KnowledgePrincipal\` represents identity and permissions **as provided by a
  trusted supervisor boundary**. It is a plain Python dataclass, not a security
  credential. Never accept a worker-supplied role or project list as proof of authority.
- Workers must not have direct write access to \`coordination.sqlite\`. Separate OS
  users, restricted paths, or a broker process are required for meaningful isolation.

## Lifecycle

\`\`\`text
captured -> proposed -> accepted -> superseded
    |           |
    +--> rejected+--> rejected
    +--> archived+--> archived
\`\`\`

Only a human principal may approve, reject or supersede a decision; agent
hypotheses stay hypotheses until explicitly converted to a decision and approved
through a trusted interface. A superseded decision remains accessible and points
to an accepted replacement within the same project. An append-only event table
preserves lifecycle transitions; stale-revision writes fail.

## Visibility

- \`private_human\`: visible only to its human author, restricted to a project
  explicitly authorized in the principal context.
- \`project_shared\`: visible only within a principal's authorized project list.
- \`global_authorized\`: visible only when explicit \`can_read_global\` is granted;
  creation requires explicit \`can_publish_global\` and a human/supervisor role.
- Privacy/project filters are applied in SQL **before** content ranking or linking.
- Cross-project links are not supported in 4A and must never be inferred from
  lexical similarity.

## Context-aware retrieval

\`find()\` is deterministic, offline, lexical and tag-based. Mismatching declared
constraints exclude a precedent; missing constraints are returned explicitly as
\`context_match="unconfirmed"\` rather than treated as proof of applicability.
\`accepted\` does **not** make a precedent automatically correct for another task.

Future semantic search must reuse the same permission filters before vector
retrieval or text exposure. No untrusted user note can directly change a Flight
Plan or DAG revision.

## Integration

\`\`\`python
from ai_coordinator import (
    Store, KnowledgePrincipal, KnowledgeKind, KnowledgeVisibility,
)
from pathlib import Path

store = Store(Path(".ai-coordinator"))
# In production this context comes from the trusted identity broker.
human = KnowledgePrincipal(
    principal_id="local-human",
    role="human",
    authorized_projects=frozenset(("project-one",)),
)
entry = store.knowledge_core.capture(
    human, "project-one", KnowledgeKind.THOUGHT,
    "Considerar um experimento isolado para testar a nova arquitetura",
    visibility=KnowledgeVisibility.PRIVATE_HUMAN,
    source_kind="human_cli",
)
\`\`\`

## Explicit non-goals

- Automatic Notion sync, semantic embeddings and generative AI insights
- Import/migration of existing \`experience_records\` or \`insights\`
- Automatic promotion of knowledge into approved DAG revisions
- Cryptographic provenance proofs issued by external providers
- Claiming Python-side authorization protects a database accessible to workers

## Release gate

Review the new tests in \`tests/test_phase4a_unified_knowledge_core.py\`.
Do not merge without the existing six-platform Python matrix plus OpenCode check.
Release v0.4.0 remains the baseline.

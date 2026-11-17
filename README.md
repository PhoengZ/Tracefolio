# Tracefolio planning package

**Working name:** Tracefolio  
**Tagline:** Turn work history into evidence-backed career knowledge.

This planning-stage repository contains a proposed design, an implementation plan, and synthetic JSON examples. Git and the owner-supplied origin are initialized; the CLI and service have not been implemented. A private source proposal is awaiting owner confirmation before implementation begins.

## Start here

1. [Proposed design](docs/superpowers/specs/2026-11-17-tracefolio-design.md).
2. [Phase 1 implementation plan](docs/superpowers/plans/2026-11-17-tracefolio-phase-1.md).
3. [Route registry example](examples/routes.example.json).
4. [Project output example](examples/project.example.json).
5. [Work output example](examples/work.example.json).

An implementation agent must read both design and plan, preserve the owner's chosen checkout/remote, and wait for confirmation of the private source proposal before starting product code. The working name has not been cleared for uniqueness or trademarks.

## Phase 1 boundary

Create a configurable, private work-knowledge layer for local Git, local text directories, and public GitHub repositories. Produce English, versioned project/work JSON plus an aggregate portfolio summary through an existing LLM harness.

No email connection or testing, message sending, job applications, resume/PDF generation, cloud upload, Notion synchronization, or public data publication.

## Privacy

Examples contain synthetic identifiers and environment-variable names only. They are illustrative proposed contracts, not currently validated against implemented project schemas. The project/work examples refer to synthetic evidence that is not a real user's work.

Actual routes, root paths, repository coordinates, identities, redaction values, packets, and generated work knowledge belong outside the public application repository. A public GitHub source does not make analysis outputs public.

The proposed setup separates public-GitHub and private-local registries. Select one through TRACEFOLIO_CONFIG or compose disjoint registries through TRACEFOLIO_CONFIG_FILES; never set both. Public sources use GitHub directly after visibility checks. Private/local sources use explicitly approved local routes, with no automatic authenticated-network or local-clone fallback. Notebook inputs retain source cells only and are never executed.

## Planned natural-language acceptance

"Summarize all configured projects and work items. Update changed sources first. Return English portfolio JSON with evidence references, unknowns, coverage, and review status. Do not create a resume or send any messages."

## Planning status

The plan's readiness review is recorded in the plan itself. All product implementation checks and real-source/harness acceptance remain PENDING. Source discovery inspected selected metadata and documentation for the private proposal; no product dependencies were installed, no collector was run, and no source configuration has been activated.


# Owner acceptance

Automated checks use synthetic data. A configured route is not evidence that every real repository has been collected or analyzed. Real-source/LLM acceptance is an explicit owner exercise; record results privately.

1. Activate the virtual environment and privately prepared environment loader. Run `tracefolio config validate`. Confirm the configured/enabled counts and selected public/local profiles. Rejected or disabled projects must not be acquired.
2. Ask the agent in this checkout: “Summarize all configured projects and work items. Update changed sources first. Return English portfolio JSON with evidence references, unknowns, coverage, and review status. Do not create a resume or send any messages.” Follow [the harness workflow](harness-workflow.md).
3. Inspect one real local-Git message **and change** and one public-GitHub message/change through their packet references. Verify the summary describes actual behavior, distinguishes reverted/planned/third-party code, and does not invent impact or ownership. Anonymous API rate limits and missing patches must appear as partial coverage.
4. Inspect one notebook work item: only code/Markdown source should appear. Training code supports an experiment/fine-tuning description, not an unverified benchmark or successful execution claim. Check unknown outcomes remain unknown.
5. Verify independent `project.json` and `works/<id>.json` files beneath the current generation. Review English descriptions and references. Keep output private; a ready machine record remains unreviewed.
6. Add a scoped route and binding privately. Validate and repeat; it should appear without a code edit. Add a meaningful source commit and repeat; the existing logical work ID should remain stable, affected claims should be reviewed again, and unrelated work should remain present.
7. Run unchanged sources and reapply an identical proposal. Expect no duplicate evidence/work and a proposal no-op. Interrupt/fail a proposal before publication only on disposable fixtures; the synthetic test suite covers pointer safety.
8. Export the portfolio under private state. Inspect every configured project, including partial/pending/error/disabled entries. No endpoint, absolute source path, author email, or credential should appear. Redaction is heuristic: inspect confidential narrative content too.

No email sending, email connection, resume/PDF generation, public publishing, or job application is part of acceptance. Linux portability is verified by CI only after that workflow actually runs; a local Windows pass is not a Linux result.

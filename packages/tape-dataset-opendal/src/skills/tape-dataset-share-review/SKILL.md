---
name: tape-dataset-share-review
description: Export, filter, scan, and review shareable tape datasets produced by `tape-dataset-opendal`. Use when Codex needs to prepare Bub-generated tape data for public sharing, Hugging Face upload, external research handoff, or any review-sensitive export that must run CEL filtering, TruffleHog secret scanning on the staged artifact, and a final conservative LLM audit.
---

# Tape Dataset Share Review

Prepare `tape-dataset-opendal` exports for public or semi-public sharing.

Assume the tapes already exist in a real Bub tape store. This skill starts at export and release review, not at tape generation.

Assume the goal is not merely to export data, but to produce a reviewable dataset artifact that can survive secret scanning and a final policy review.

## Workflow

1. Export to a local staging directory first.
2. Apply CEL filters during export when the user already knows exclusion criteria.
3. Inspect the staged files and make any requested edits before scanning.
4. Run TruffleHog on the staged artifact, not on the original tape store.
5. If TruffleHog reports any findings, treat the export as blocked until the content is filtered or edited and rescanned.
6. For outputs that survive TruffleHog, perform a conservative LLM review using project context plus the staged dataset.
7. Only recommend publishing when deterministic checks, TruffleHog, and LLM review all pass.

## Export Policy

- Prefer `bub tape-export --scheme fs` to create a local staging copy first, even if the final target is S3, GCS, or another OpenDAL backend.
- Use remote object storage only after the staged export has passed scanning and review.
- Keep `manifest.json`, `tapes.jsonl`, `entries.jsonl`, and `segments.jsonl` together during review.
- Review `raw/` files only as needed for evidence or debugging because they are the most verbose surface.
- If the user also needs a reproducible verification path, prefer documenting a real `bub run` -> `bub tape-export` chain rather than a synthetic fixture that appends entries directly.

If full CLI details are needed, read `../../../README.md`.

## CEL Filtering

Use export filters to exclude content before scanning whenever the exclusion rule is crisp and repeatable.

Examples:

```bash
bub tape-export \
  --scheme fs \
  --config root=/tmp/tape-export \
  --root candidate \
  --filter 'kind != "tool_result"' \
  --filter '!(text.contains("private-project") || text.contains("counterparty"))'
```

Use `--filter-file` when the filter set is large or will be revised during review.

## TruffleHog Scan

Run the bundled wrapper script from this skill directory:

```bash
python ${SKILL_DIR}/scripts/trufflehog_scan.py /tmp/tape-export/candidate \
  --report /tmp/tape-export/candidate.trufflehog.json
```

The script:

- prefers a local `trufflehog` binary when available
- falls back to `podman` or `docker` with the official TruffleHog container
- emits a normalized JSON report

Blocking rule:

- Any `verified`, `unverified`, or `unknown` finding blocks publication.
- Do not hand-wave unverified findings away. Treat them as blocked unless the user explicitly wants manual adjudication.

Read `references/release-gates.md` for the release-gate rationale and normalized report shape.

## LLM Review

After TruffleHog returns zero findings, perform a final audit over the staged export plus project context files such as `README.md` and `AGENTS.md`.

Return strict JSON using this shape:

```json
{
  "about_project": "yes | no | mixed",
  "shareable": "yes | no | manual_review",
  "missed_sensitive_data": "yes | no | maybe",
  "flagged_parts": [{ "reason": "string", "evidence": "string" }],
  "summary": "string"
}
```

Review criteria:

- `about_project=yes` only if the exported content is clearly about the intended OSS project.
- `shareable=yes` only if the dataset is public-safe after filtering and edits.
- `shareable=manual_review` whenever scope, ownership, or sensitivity is uncertain.
- `missed_sensitive_data=yes` if any likely credential, token, PII, private business data, or unrelated private work appears to have survived.
- `missed_sensitive_data=maybe` when suspicion exists but evidence is incomplete.
- Quote short real excerpts in `flagged_parts`; do not speculate without evidence.

## Audit Heuristics

- Treat surviving credentials, bearer tokens, cookies, OAuth tokens, or secret-like strings as blocking.
- Treat unrelated private projects, counterparties, finance/legal matters, personal accounts, and non-OSS operations as blocking or `manual_review`.
- Do not flag public OSS metadata by itself, such as commit-author emails in public history, project-local paths, or ordinary repository names.
- Be stricter on `raw/` than on summarized files because raw entries preserve more verbatim content.
- If the user asks for public release, bias toward exclusion over retention.
- If the staged export comes from real Bub runtime tapes, do not assume human-friendly tape names will survive export; hashed tape names can still be valid if the content and counts are correct.

## Deliverables

When using this skill, leave behind:

- the staged export directory
- the TruffleHog JSON report
- the final LLM review JSON

If the dataset passes, state clearly that it passed:

1. CEL filtering / manual edits
2. TruffleHog scan
3. LLM review

If it fails, state which gate failed and what must change before rerunning.

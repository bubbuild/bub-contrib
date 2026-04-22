# Release Gates

This reference defines the release gates for `tape-dataset-opendal` exports that are intended for public sharing.

The goal is to preserve a conservative publication workflow:

1. export to a staged artifact, optionally shaping it with CEL filters and manual edits
2. scan the staged artifact with TruffleHog
3. run a final LLM review over the surviving dataset
4. publish only if every gate passes

## Why scan the staged artifact

Always scan the staged export, not the source tape store.

Reasons:

- filters may remove risky content before scanning
- manual edits may introduce or remove content after export
- the publication decision should be based on the exact artifact that would be uploaded

If the tapes were generated through real Bub sessions, this rule still holds. The release gate is attached to the exported artifact, not to the runtime that produced it.

## TruffleHog policy

Treat all TruffleHog finding states as blocking by default:

- `verified`
- `unverified`
- `unknown`

This is a release-gate policy, not a detector-confidence policy.

The normalized report written by `scripts/trufflehog_scan.py` has this shape:

```json
{
  "target": "string",
  "command": ["string"],
  "findings": [
    {
      "detector": "string",
      "decoder": "string | null",
      "status": "verified | unverified | unknown",
      "file": "string | null",
      "line": 123,
      "verification_from_cache": false,
      "raw_sha256": "sha256:...",
      "masked": "prefix***suffix"
    }
  ],
  "summary": {
    "findings": 0,
    "verified": 0,
    "unverified": 0,
    "unknown": 0,
    "top_detectors": ["string"]
  },
  "blocking": false
}
```

## Review targets

Review these dataset surfaces in this order:

1. `manifest.json`
2. `tapes.jsonl`
3. `entries.jsonl`
4. `segments.jsonl`
5. `raw/*.jsonl` only when evidence or debugging requires it

Default review surface:

- use `entries.jsonl` for exact content review
- use `segments.jsonl` for task-level or trajectory-level review
- escalate into `raw/` when summaries look suspicious

## LLM review schema

Use this JSON schema for the final review result:

```json
{
  "about_project": "yes | no | mixed",
  "shareable": "yes | no | manual_review",
  "missed_sensitive_data": "yes | no | maybe",
  "flagged_parts": [{ "reason": "string", "evidence": "string" }],
  "summary": "string"
}
```

Interpretation:

- `about_project=yes` only when the export is clearly about the intended OSS project
- `shareable=yes` only when the dataset looks safe for public release
- `manual_review` when ownership, scope, or sensitivity remains uncertain
- `missed_sensitive_data=yes` when likely secrets, credentials, PII, or confidential non-OSS content survived

## Common blocking examples

- surviving credentials, API keys, bearer tokens, cookies, or secret-like material
- unrelated private projects or internal company systems
- finance, legal, customer, or counterparty material
- personal accounts, inboxes, calendars, cloud consoles, or private infrastructure

## Common non-blocking examples

- public OSS commit metadata
- public contributor names or emails in clearly public git history
- paths clearly inside the current OSS workspace
- normal repository names, branches, and issue references

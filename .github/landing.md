# Landing automation

This is a maintainer setup and operation guide. The workflows use the public
[Landing Action](https://github.com/bubbuild/landing/blob/9014baa64f82893dc194a3f1e31391737f68c426/action.yml)
at `9014baa64f82893dc194a3f1e31391737f68c426`, adapted from Landing and
[Republic](https://github.com/bubbuild/republic/tree/7b9fdea6ff537b40ba43b7c01af0bc8eb79dab8f/.github/workflows).
Local `uses: ./` is Landing's self-host integration, not a consumer API.

## Configure

Set repository variable `LANDING_MODEL` and secret `LANDING_API_KEY` for your
provider. Without `LANDING_MODEL`, Landing jobs skip and Tests still runs.
Optional variables are `LANDING_API_BASE`, `LANDING_COMPLETION_ARGS` (JSON), and
`LANDING_TRUST` (`repository`, the default, or `owner`).

The Ubuntu runner supplies gh; setup-uv prepares Python 3.12 for project checks.
The Action installs its own isolated runtime. No repository Python dependencies
are installed with model or publication credentials before event admission.
Candidate code and delegated shell commands still require trusted execution;
admission is authorization, not sandboxing. Automatic review excludes forks and
draft PRs; native Tests still runs for both.

By default publication uses `github.token`. Enable Actions to create PRs where
repository policy permits. Optional `LANDING_GITHUB_TOKEN` selects another
publishing identity; `LANDING_ADMISSION_TOKEN` can supply permission lookup access
(including organization Members read for owner trust). Match `LANDING_GIT_NAME`
and `LANDING_GIT_EMAIL` to that identity for commits. Separately supplied tokens
must be scoped by their owner; workflow permissions only limit `github.token`.

Scheduled/manual dependency work reads Dependabot alerts. Alert access needs a
separately provisioned token with Dependabot alerts read permission; there is no
`vulnerability-alerts` Actions permission. If configured for Dependabot-triggered
runs, provide the necessary model/publication secrets as Dependabot secrets too.

## Review and delegate

CI keeps `uv run --frozen --group test pytest` as the independent Tests job.
Advisory Landing review receives its explicit conclusion and the actual checked
revision (`github.sha`, which can be a PR merge commit), separately from the PR
head. Feedback execution failures warn and retain artifacts; findings do not
replace native acceptance. There is no documentation build: review README and
package Markdown against the changed behavior and commands.

Repository writers can post `/landing review`, `/landing fix`, `/landing triage`,
`/landing explain`, or `/landing bump` followed by a concrete task, in an issue,
PR conversation, or review thread. Landing checks the comment author's effective
permission. Duty checks out the default branch with credentials persistence off
and prepares Git identity/authentication before invoking the public Action.
Do not place a candidate checkout or candidate setup step before admission.
Explicit task failures fail the job; artifacts are retained for 30 days.
Concurrency preserves the running duty; GitHub's default single pending slot can
replace an older pending delegation. Repost a superseded command when needed.

## Dependency candidates and native CI

Monday 02:23 UTC, manual Landing duty dispatch, and forwarded Dependabot alerts
can prepare dependency updates. Preserve manifest constraints, workspace sources,
lockfile consistency, and existing pins. New updates are draft PRs on fresh
`deps/landing-*` branches; work and checks use isolated candidate checkouts.
Do not change package release versions or publish packages as dependency work.

Failed candidate CI can assist Dependabot, Renovate, or the detected Landing
publisher on `deps/landing-*` branches. Set `LANDING_BUMP_ON_FAILURE=false` to
turn this off; `LANDING_DEPENDENCY_UPDATE_ACTORS` adds one allowed login per line.
Landing verifies an unambiguous same-repository PR and matching current head.

After publishing a candidate, run independent CI on its branch:

```sh
gh workflow run ci.yml --ref deps/landing-example -f number=123 -f head=FULL_HEAD_SHA
gh run list --workflow ci.yml --branch deps/landing-example
```

Verify the run's `headSha` matches the candidate before citing its Tests result.
The `head` input identifies the review target; it does not change the checkout.
Dispatch on the candidate branch, not main. Dispatch becomes available after the
workflow is on the default branch. Never wait for Landing feedback as an acceptance
check. GitHub supports explicit dispatch with `GITHUB_TOKEN`; ordinary token pushes
do not trigger push workflows, and token-created PR runs may require approval.
See [GitHub's trigger contract](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow).

A `repository_dispatch` with type `dependabot_alert` needs an external authenticated
receiver forwarding `client_payload.alert_number`. This change installs no receiver.

## Native failure and publication evidence

Duty triages failed default-branch CI pushes/dispatches and manual Release runs,
using affected job logs and existing matching issues. It ignores successful runs
and unrelated historical failures. Read the source run's SHA and conclusion;
the duty checkout itself supplies no check evidence (`checked-revision` is empty).

Publish Request runs on `issues`, which the pinned Landing Action does not admit
as a workflow_run source. Its existing validation, release result comment, retry
label handling, and issue closure remain authoritative. Use an explicit
`/landing triage` comment with the failed run link when more investigation is
needed. Release remains the existing reusable/manual workflow: maintainers approve
with `to-be-published`, inputs are validated, publication stays on the default
branch, and the `pypi` environment controls trusted publishing. A passing test or
build does not prove that a package was published. No release-main, deployment,
or documentation-site workflow is added.

---
name: Publish request
about: Request publication of a Bub plugin package to PyPI
title: "Request for publish: "
labels: ""
assignees: ""
---

<!-- publish-request -->

<!--
Set the issue title to exactly:

Request for publish: <package> <version>

Example:

Request for publish: bub-example 1.2.3

The package must exist under packages/, declare a Bub entry point, and use a
version that has not already been published to PyPI. A maintainer will add the
`to-be-published` label after reviewing the request.
-->

## Release notes

Describe the changes included in this release and any compatibility concerns.

## Checklist

- [ ] The package is ready to publish from the default branch.
- [ ] Tests and documentation are up to date.
- [ ] The requested version follows PEP 440 and has not been published before.

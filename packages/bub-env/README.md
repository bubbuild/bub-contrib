# bub-env

Injects environment variables declared in the Bub config file into the Bub
process at startup. Useful for API keys consumed by CLI tools and agent skill
scripts (they inherit the Bub process environment), when running Bub as an
installed tool where a workspace `.env` file is not practical.

Chinese documentation: [README.zh-CN.md](./README.zh-CN.md)

## Configuration

Add an `env:` section to `~/.bub/config.yml`. Every key/value pair becomes an
environment variable:

```yaml
env:
  AAAA_API_KEY: sk-...
  BBBB_API_KEY: sk-...
```

Variables already set in the real process environment are never overridden,
matching Bub's usual "environment beats config file" precedence. Non-string
YAML values are stringified (`true`/`false` for booleans); `null` values are
skipped.

### Trace export (OTLP)

Bub decides whether to export traces before plugins load, so plain injection
comes too late for it. When `env:` injects `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`
or `OTEL_EXPORTER_OTLP_ENDPOINT`, the plugin calls Bub's
`bub.tracing.configure_otlp()` once more, so trace settings can live in the
config file. Bub needs the `trace` extra (`bub[trace]`):

```yaml
env:
  OTEL_SERVICE_NAME: bub
  OTEL_EXPORTER_OTLP_TRACES_ENDPOINT: http://phoenix.lan:6006/v1/traces
  OTEL_EXPORTER_OTLP_TRACES_HEADERS: authorization=Bearer%20<api-key>,x-project-name=<project>
```

Header values are URL-encoded per the OTel spec (a space is `%20`). An
endpoint already set in the process environment was handled by Bub at
startup, and the plugin leaves it alone.

## Install (end users)

`bub-env` is not on PyPI. With a global Bub install (`uv tool install bub`),
install the plugin into Bub's own environment:

```bash
bub install bub-env@main
```

## Install (local development)

Install the package path into the same environment that runs `bub`:

```bash
uv tool install bub --with /path/to/bub-contrib/packages/bub-env
```

or add it as a dependency of the Bub host project.

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

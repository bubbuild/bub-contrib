# bub-wechat

WeChat channel adapter for Bub message IO.

## Description

This package provides a Bub plugin for integrating WeChat as a message channel. It enables sending and receiving messages via WeChat within the Bub ecosystem.

## Features
- Send and receive messages through WeChat
- Designed for easy integration as a Bub plugin
- Follows Bub contrib package conventions

## Usage

Install via pip (from the monorepo):

```bash
uv pip install git+https://github.com/bubbuild/bub-contrib.git#subdirectory=packages/bub-wechat
```

You can also install it with Bub:

```bash
bub install bub-wechat@main
```

Login to WeChat using the provided CLI tool:

```bash
bub login wechat
```

## Development

- Requires Python 3.12+
- See the root README for workspace setup instructions

## Maintenance

Plugin contributors are encouraged to maintain this package and respond to issues or PRs. Code review standards are relaxed for contrib plugins, prioritizing practicality and safety.

## License

See [LICENSE](../../LICENSE).

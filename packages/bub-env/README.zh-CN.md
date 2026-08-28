# bub-env

在 Bub 启动时，把配置文件中声明的环境变量注入 Bub 进程。适用于 CLI 工具和
agent skill 脚本所需的 API 密钥（它们继承 Bub 进程的环境变量）——尤其是以
安装工具方式运行 Bub、没有工作区 `.env` 文件可用的场景。

English documentation: [README.md](./README.md)

## 配置

在 `~/.bub/config.yml` 中添加 `env:` 配置段，其中每个键值对都会成为一个
环境变量：

```yaml
env:
  AAAA_API_KEY: sk-...
  BBBB_API_KEY: sk-...
```

进程环境中已存在的变量不会被覆盖，与 Bub 一贯的「环境变量优先于配置文件」
的优先级保持一致。非字符串的 YAML 值会被转换为字符串（布尔值转为
`true`/`false`）；`null` 值会被跳过。

## 安装（普通用户）

`bub-env` 未发布到 PyPI。如果 Bub 是全局安装的（`uv tool install bub`），
用下面的命令把插件装进 Bub 自己的环境：

```bash
bub install bub-env@main
```

## 安装（本地开发）

将包路径安装到运行 `bub` 的同一环境中：

```bash
uv tool install bub --with /path/to/bub-contrib/packages/bub-env
```

或者将它添加为 Bub 宿主项目的依赖。

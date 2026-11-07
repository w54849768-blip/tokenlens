# TokenLens —— 容器化部署（云端/内网都可以用）

纯标准库实现，**没有任何第三方运行时依赖**，所以镜像可以很小。

    docker build -t tokenlens .
    docker run --rm -p 8787:8787 tokenlens

默认跑**示例数据**（`--demo`），打开 http://localhost:8787 就能看到完整界面。

## 接真实数据

容器里没有你主机的 `~/.claude`、`~/.dsh` 等目录，所以有三种做法：

### 方案 A：把宿主目录只读挂进来（自托管、单机云主机）

    docker run --rm -p 8787:8787 \
      -v ~/.cc-switch:/root/.cc-switch:ro \
      -v ~/.claude:/root/.claude:ro \
      -v ~/.dsh:/root/.dsh:ro \
      -e TOKENLENS_SOURCE=local \
      tokenlens python backend/server.py --source local --host 0.0.0.0

### 方案 B：自建一个「请求入库」管线（推荐给真正多用户/多机器的场景）

写一个新的数据源模块，把请求落到 Postgres/ClickHouse，再实现 7 个端点。
模板与字段约定见 [docs/CLOUD.md](docs/CLOUD.md)。

### 方案 C：本地跑，云端只放前端

TokenLens 的 API 是纯 JSON 的 REST 接口，也可以把 `web/` 静态托管到任何
静态站点，把 `/api` 反代到你自己的网关（注意别把余额接口暴露到公网）。

## 反向代理

`server.py` 默认只监听 `127.0.0.1`，这是有意的（余额接口会用到真密钥）。
要在容器/云上用 `--host 0.0.0.0` **务必**配上鉴权：

- 前面挂 Nginx / Caddy / Cloudflare Access，加 Basic Auth 或 SSO
- 不要把 `/api/balances` 直接暴露给公网
- 生产环境建议设 `TOKENLENS_READ_ONLY=1`（见 docs/CLOUD.md 的安全清单）

## 健康检查

    curl -fsS http://127.0.0.1:8787/api/meta

返回 JSON 且含 `app.name == "TokenLens"` 即视为健康。

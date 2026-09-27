# image-manifest-resolver

本地镜像引用解析与多平台选择服务。调用方给出镜像引用与目标平台，服务返回
可用于拉取的**最终单架构清单**及其**层摘要列表**，并对整条引用链做摘要校验、
对选择过程做确定性约束、对更新做原子化提交、对解析结果做版本化缓存。

- 纯内存内容存储（`ContentStore`），无外部依赖，线程安全；
- 可选 FastAPI 接口（`uvicorn main:app`）。

## 快速开始

```bash
uv sync
uv run pytest -q          # 运行全部测试
uv run fastapi dev main.py # 启动 HTTP 服务
```

作为库使用：

```python
from imr.service import ResolverService
from imr.platform import PlatformSelector, FallbackStep, TieBreak
from imr.models import Platform
from imr.store import BatchUpdate
from imr.resolver import parse_object
from imr.digest import compute_digest

selector = PlatformSelector(
    fallback=[
        FallbackStep(os="linux", architecture="amd64", name="linux-amd64"),
        FallbackStep(os="linux", architecture="arm64", name="linux-arm64"),
    ],
    tie_break=TieBreak.ERROR,          # 多候选默认报错，禁止静默任选
    allow_missing_target=True,         # 目标平台缺失时是否允许走回退
)
svc = ResolverService(selector=selector)

# 1) 原子提交：blobs 是 digest->bytes；manifests 是 digest->裸 JSON 字节
raw = manifest_json_bytes
digest = compute_digest(raw)
svc.commit(BatchUpdate(
    put_blobs={digest: raw, config_digest: config_bytes, **layer_blobs},
    put_manifests={digest: parse_object(digest, raw)},
    tag_updates={"app:v1": root_digest},   # 标签可指向清单或索引
))

# 2) 解析
result = svc.resolve("app:v1", Platform(os="linux", architecture="amd64"))
print(result.manifest_digest, result.layer_digests, result.matched_by)
```

## 引用格式

```
[registry/]name[:tag][@digest]
```

- 无标签时默认 `latest`；
- 带 `@digest` 时以摘要为准（标签可同时存在但仅作信息）；
- 非法字符、未知摘要算法、畸形摘要在解析阶段即以
  `reference_invalid` / `digest_unsupported_algorithm` 拒绝。

## 平台匹配规则（确定且可复现）

`PlatformSelector.select(index.manifests, target)` 严格按以下顺序执行，
首个产生候选的步骤决定结果：

1. **精确匹配 `exact`**：目标平台非空时，按 OCI 语义比较
   `os`、`architecture`、`variant`、os.version 与 os.features
   （特性按集合比较，与顺序无关）。
2. **回退步骤 `fallback[i]:<os>/<arch>/<variant>`**：按配置顺序逐个尝试。
   - `os` / `architecture` 支持 `"*"` 通配；
   - `variant="*"` 匹配任意（含未声明）；`variant=None` 仅匹配未声明
     variant 的候选；具体字符串要求相等。
3. **唯一候选 `sole_candidate`**：仅当**未提供目标平台**、没有回退命中、
   且索引中恰好只有一个带平台描述符时使用。目标平台明确但无匹配时
   **绝不**静默落到别的候选，而是返回 `platform_not_found`。

### 多候选与歧义

- 任一步骤产生多个候选即视为歧义：
  - `tie_break=TieBreak.ERROR`（默认）：抛 `ambiguous_candidate`，
    错误消息中包含全部候选摘要，禁止静默返回任意候选；
  - `tie_break=TieBreak.DIGEST_ASC`：取摘要字典序最小者，
    `matched_by` 附加 `+tie_break(digest_asc)`，结果与候选排列顺序无关，
    可复现。
- 不带 `platform` 字段的索引条目不参与平台选择。

### 目标平台缺失

- `allow_missing_target=False`：直接抛 `platform_missing`；
- `allow_missing_target=True`：跳过精确匹配，仅按回退顺序选择；
  无回退且非唯一候选时抛 `platform_not_found`。

直接引用**单一清单**（非索引）时按 OCI 惯例直接返回，
`matched_by="direct"`，不进行平台选择。

## 摘要链校验

提交与解析时均做内容寻址校验，失败立即中止且不缓存：

| 失败情形 | 错误码 |
| --- | --- |
| 未知/不支持的摘要算法、畸形摘要 | `digest_unsupported_algorithm` |
| 内容与声明摘要不一致（清单/索引/层/blob） | `digest_mismatch` |
| 引用的层、config、子清单缺失 | `blob_not_found` |
| 非 JSON、字段缺失、未知 mediaType、结构不符 | `manifest_invalid` |

支持的算法：`sha256`、`sha512`（可通过 `parse_digest(..., allowed=...)`
限定）。校验覆盖：blob 字节↔键摘要、manifest/index 裸字节↔摘要、
manifest→config、manifest→每个 layer、index→每个子清单。

## 原子更新与失败恢复

- 每个提交是一个 `BatchUpdate`：`put_blobs` / `put_manifests` /
  `tag_updates` 与可选的 `expected_version`（乐观并发）。
- 提交时先在**新快照**上完成全部校验（摘要、引用目标、标签目标），
  通过后才做一次版本指针交换。读取方持有不可变 `Snapshot`
  （只读映射），任何时刻只能看到某个完整版本，不会出现新旧字段混合。
- 任一校验失败都在交换前抛出，**不留部分写入**，服务继续以最近一次
  完整版本对外服务；版本号不增长。
- `expected_version` 不匹配抛 `update_conflict`。

## 允许 / 拒绝策略

`Policy` 由有序规则与默认决策构成，在解析的**最终具体目标**
（具体清单摘要 + 命中平台）上求值：

- 规则字段：`tag`、`digest`、`name`、`platform`（`os/arch[/variant]`）、
  `platform_os`、`platform_arch`；pattern 使用 `fnmatch` glob。
- **按配置顺序求值，第一条命中的规则决定**：`deny` 立即拒绝，
  `allow` 立即放行（可覆盖后续更宽的拒绝）。
- 无规则命中时由 `default`（`allow` / `deny`）决定。
- 拒绝抛 `policy_denied`，原因稳定：显式规则为
  `deny:<field>:<pattern>`，默认拒绝为 `deny:default`，
  多次调用消息完全一致。

```python
svc.policy.deny("tag", "*-secret").allow("digest", "sha256:abc...")
```

## 缓存语义

- 缓存键为内容键（根摘要 + 目标平台），每条记录标注生成它的
  store `version`；仅当版本与当前一致才命中。
- 成功提交后清空缓存；标签指向更新后绝不会返回陈旧结果。
- 同键并发未命中走 **single-flight**：仅一个线程执行重建（副作用只发生
  一次），其余线程等待同一结果；重建失败不缓存，错误传播给所有等待者，
  后续调用会重新尝试。
- 解析被拒绝或摘要校验失败时不写入缓存。

## HTTP 接口

`POST /v1/resolve`

```json
{"reference": "app:v1",
 "platform": {"os": "linux", "architecture": "amd64", "variant": null}}
```

响应含 `manifest_digest`、`index_digest`、`layer_digests`、`platform`、
`matched_by`、`store_version`。

`POST /v1/commits`：`blobs` / `manifests` 的值为 base64；
`tags` 的值为摘要或 `null`（删除标签）；可带 `expected_version`。

策略：`GET /v1/policy`、`POST /v1/policy/rules`、
`DELETE /v1/policy/rules?default=allow`。

错误统一为 `{"error": {"code", "message"}}`，HTTP 状态映射：

| code | HTTP |
| --- | --- |
| reference_invalid / digest_unsupported_algorithm / manifest_invalid / platform_missing | 400 |
| blob_not_found / platform_not_found | 404 |
| policy_denied | 403 |
| ambiguous_candidate / update_conflict | 409 |
| digest_mismatch | 422 |

## 模块结构

```
imr/
  errors.py     稳定错误码
  models.py     引用、平台、描述符、清单、索引、结果
  digest.py     摘要解析/计算/校验
  reference.py  镜像引用解析
  platform.py   确定性选择器、回退、歧义处理
  policy.py     允许/拒绝策略
  store.py      原子内容存储与不可变快照
  cache.py      版本化缓存 + single-flight
  resolver.py   清单/索引解析与端到端解析
  service.py    门面（提交 + 解析 + 缓存失效）
  api.py        FastAPI 接口
tests/          80+ 项自动化测试（选择/摘要/并发/恢复/缓存/策略/HTTP）
```

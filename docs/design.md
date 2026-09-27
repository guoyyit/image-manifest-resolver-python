# 镜像引用解析与多平台选择服务 — 设计与语义

本服务在本地解析镜像引用（tag 或 digest），返回与目标平台匹配、且摘要链完整校验的
最终清单（manifest）与层摘要（layer digests）。引用可指向单一清单，也可指向包含多个
候选清单的多架构索引（image index）。所有解析结果**确定、可复现**，失败一律以可区分
的错误类型返回，绝不静默给出任意候选。

## 1. 模块结构

| 模块 | 职责 |
| --- | --- |
| `image_resolver/models.py` | `ImageReference` / `Platform` / `Descriptor` / `Manifest` / `ImageIndex` / `ResolvedImage` |
| `image_resolver/digest.py` | 摘要算法、规范化编码、摘要解析与校验 |
| `image_resolver/platform.py` | 精确匹配、可配置回退链、确定性候选选择 |
| `image_resolver/policy.py` | 按平台 / 摘要 / 名称标签的允许与拒绝策略 |
| `image_resolver/store.py` | 原子版本化内容存储（事务暂存、提交、回退） |
| `image_resolver/cache.py` | 基于摘要的并发安全缓存 + single-flight 重建 |
| `image_resolver/resolver.py` | 解析编排、摘要链校验、发布事务、缓存失效 |
| `image_resolver/service.py` | FastAPI HTTP 接口与稳定错误码映射 |

## 2. 解析流程

1. **引用解析**：digest-pinned 引用直接使用摘要；tag 引用从最近一次完整提交的版本中把
   tag 解析成摘要。tag→digest 的解析在缓存之外进行，因此 tag 指向新清单后绝不会返回
   陈旧结果。
2. **根对象加载**：根对象是 `Manifest` 时直接使用；是 `ImageIndex` 时进入平台选择，再
   取被选中的子清单。根对象会与其摘要再校验一次。
3. **平台选择**：见第 3、4 节。
4. **摘要链校验**：索引描述符 → 子清单；清单 `config` 与每个 `layer` → blob。摘要不
   匹配、未知算法、引用目标缺失分别抛出不同错误，立即终止，不写入、不缓存。
5. **策略校验**：在每次解析（包括缓存命中）时执行，策略变更立即生效。
6. **缓存**：不可变的“校验结果”以 `(digest, 平台签名)` 为键缓存；策略结果不缓存。

返回的 `ResolvedImage` 含：`manifest_digest`、`layer_digests`、`matched_platform`、
`source`（`manifest`/`index`）与 `selection_reason`。

## 3. 平台匹配规则（确定且可复现）

### 3.1 精确匹配
候选与目标 `os`、`architecture` 相同，且满足：

- `variant` 兼容。OCI 隐含规则：架构为 `arm` 且描述符未给 variant 时按 `v1` 处理；
- 目标给出 `os_version` 时，候选必须等于该值；
- 目标的 `os_features`、`features` 必须是候选对应集合的**子集**。

### 3.2 回退（禁止静默任选）
仅当不存在精确匹配时，才使用**显式配置**的回退链：

- `os_fallback`：`{目标 os: (允许回退到的 os, ...)}`
- `arch_fallback`：`{目标 arch: (允许回退到的 arch, ...)}`

内置默认链（可用 `None` 表示采用；传**空字典**表示显式禁用该维度）：

```text
os:   linux -> (linux)       windows -> (windows)    darwin -> (darwin)
arch: amd64 -> (amd64, 386)  arm64 -> (arm64, arm)
      arm   -> (arm)         386  -> (386)           ppc64le -> (ppc64le)  s390x -> (s390x)
```

回退只在链中列出的候选里挑选；**没有任何链能满足时直接报 `NoPlatformMatchError`**，
不会返回“随便一个”。同架构回退仍要求 variant 兼容；跨架构回退（如 arm64→arm）不比较
彼此不可互换的 variant。

### 3.3 多候选的确定性排序
排序键（越小越优先）：

```text
(回退距离, 是否精确, os, architecture, variant, descriptor.digest)
```

- 精确匹配永远优先于回退；
- 回退距离按 OS 偏差权重小于架构偏差；
- 其余完全并列时，以候选描述符摘要做最终 tie-break。

因此同一索引无论候选顺序如何，结果都一致、可复现。

### 3.4 目标平台缺失
- 提供了 `default_platform` 时使用默认平台；
- 既无目标平台也无默认平台时，抛出 `NoPlatformMatchError`。
- 对**单一清单**（无平台描述符）且未给平台的情况，策略评估使用占位平台
  `unknown/unknown`（这是唯一没有真实平台信息的场景）。

## 4. 策略语义（允许 / 拒绝）

`RuleSet` 包含平台、摘要、名称模式三类规则。求值顺序固定，保证拒绝原因稳定：

1. **拒绝优先**：命中任一 `deny_*` 立即拒绝（digest → platform → name/tag 模式）。
2. **非空 allow 即白名单**：`allow_digests` / `allow_platforms` /
   `allow_name_patterns` 任一非空时，对应维度必须命中；空集合表示“不限制该维度”。
3. **默认决策**：`default_decision="allow"|"deny"`（默认 `deny`），全部未命中且为
   `deny` 时返回稳定原因 `"denied by default policy"`。

名称模式采用 `fnmatch` glob，匹配 `name`、`registry/name`、以及带 `:tag` /
`@digest` 的完整形式。策略在每次解析时执行，因此即使结果已缓存，越权解析仍被拒绝，
且原因字符串稳定（HTTP 错误码 `403 policy_denied`）。

## 5. 摘要链与错误类型

支持算法：`sha256`、`sha384`、`sha512`。内容采用规范化 JSON
（`sort_keys=True`、紧凑分隔符、UTF-8）计算摘要，保证跨进程可复现。

| 错误 | 含义 | HTTP |
| --- | --- | --- |
| `MalformedDigestError` | 摘要不是 `algorithm:hex` 形式 | 422 `malformed_digest` |
| `UnknownAlgorithmError` | 未知/不支持的摘要算法 | 422 `unknown_algorithm` |
| `DigestMismatchError` | 内容摘要与引用摘要不一致 | 422 `digest_mismatch` |
| `ReferencedObjectMissingError` | 引用的清单/blob 目标缺失 | 404 `referenced_object_missing` |
| `NoPlatformMatchError` | 平台无精确匹配且无可用回退 | 404 `no_platform_match` |
| `PolicyDeniedError` | 策略拒绝（原因稳定） | 403 `policy_denied` |
| `ConcurrentUpdateError` | 已有更新事务在进行 | 409 `concurrent_update` |
| `PartialWriteError` | 更新未提交完整（结构性问题） | 409 `partial_write` |

任何校验失败都会中止解析/发布，**不继续解析、不缓存、不留部分写入**。

## 6. 原子更新与失败恢复

`ContentStore` 以不可变的 `_Version(sequence, objects, tags, refs)` 快照保存已提交
状态：

- 写操作先在事务中暂存（`begin` / `put_object` / `put_blob` / `bind_tag`），读者在
  提交前完全看不到暂存内容；
- `commit_version()` 基于旧版本构造**全新的**映射，最后用一次引用切换发布；读者任一
  时刻只会看到某个完整版本，不会出现新旧字段混合；
- 提交前校验所有 tag 绑定的目标摘要都存在；提交失败会自动丢弃暂存态；
- 异常路径（或上下文管理器 `with store:` 抛错）执行 `rollback()`，服务继续以最近一次
  完整版本对外服务。

发布一个索引时，其全部子清单与 blob 都在**同一事务**中暂存并校验，成功才一起原子生效。

## 7. 摘要缓存：并发、single-flight 与失效

`DigestCache`：

- 所有读写受一把锁保护，支持并发访问；
- 并发同键 miss 时，loader **只执行一次**（single-flight），其余线程等待同一结果，
  不会重复执行副作用；
- loader 抛错时错误传播给所有等待者且**不缓存**，后续请求可重试；
- `invalidate(key)` 清除单键，`invalidate()` 清空全部；若失效发生在 loader 执行期间，
  会标记该键，使在途结果完成后**不被回填**。

`Resolver` 在每次成功发布后清空缓存（内容寻址条目虽不可变，但 tag 可能已指向新摘
要），并将 tag→digest 解析置于缓存之外，从根上杜绝陈旧结果。策略每次实时校验，故策
略变更也无需失效缓存即可立即生效。

## 8. HTTP 接口

- `GET /healthz`：返回 `{"status":"ok","version": <已提交版本号>}`。
- `POST /resolve`：请求体
  `{"reference": {"name","tag?","digest?","registry?"}, "platform": {"os","architecture","variant?"}}`；
  返回 `manifest_digest`、`layer_digests`、`matched_platform`、`source`、
  `selection_reason`。
- `POST /publish`：发布单一清单（含 config/layer 描述符与 blob 字节）。

错误统一返回 `{"error": <稳定错误码>, "reason": <稳定原因>}`。

启动：

```bash
uv run uvicorn main:app --reload
```

## 9. 测试

```bash
uv run pytest -q
```

覆盖：多候选选择与确定性 tie-break、回退与缺失平台、摘要不匹配/未知算法/目标缺失、策
略允许拒绝与稳定原因、并发更新原子性（读写并发、无字段混合）、更新失败回滚恢复、缓存
single-flight 与失效（含在途失效）、端到端多架构解析与标签更新后的新鲜度。

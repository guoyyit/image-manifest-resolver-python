# image-manifest-resolver

本地镜像引用解析与多平台选择服务：给定镜像引用（tag 或 digest）与目标平台，返回**摘要链
已校验**、可直接用于拉取的最终清单与层摘要列表。引用可以是单一清单，也可以是包含多个
候选清单的多架构索引。

## 特性

- 单清单 / 多架构索引解析，平台匹配**确定、可复现**（含确定性 tie-break）。
- 目标平台缺失或无精确匹配时，按**可配置回退链**选择；不可满足时报错，绝不静默任选。
- 校验 `index → manifest → config/layer` 完整摘要链；摘要不匹配、未知算法、目标缺失
  返回**可区分**的错误，且不继续解析、不缓存。
- 索引/清单更新**原子生效**：读者只会看到某个完整提交版本，无新旧字段混合。
- 按平台 / 摘要 / 名称标签配置允许与拒绝策略，越权解析被拒绝并返回稳定原因。
- 更新中途失败自动回滚，服务持续以最近一次完整状态对外服务。
- 基于摘要的缓存支持并发访问、single-flight 重建（副作用不重复执行）与失效；更新后不
  返回陈旧结果。

## 快速开始

```bash
uv sync
uv run uvicorn main:app --reload
```

```bash
# 发布一个单清单（config/layer blob 以 UTF-8 字符串提供）
curl -s -X POST localhost:8000/publish -H 'content-type: application/json' -d '{
  "reference": {"name": "app", "tag": "v1"},
  "config": {"digest": "<sha256:...>", "size": 3},
  "layers": [{"digest": "<sha256:...>", "size": 5}],
  "blobs": {"<sha256:cfg>": "cfg", "<sha256:layer>": "layer"}
}'

# 按平台解析
curl -s -X POST localhost:8000/resolve -H 'content-type: application/json' -d '{
  "reference": {"name": "app", "tag": "v1"},
  "platform": {"os": "linux", "architecture": "amd64"}
}'
```

## 作为库使用

```python
from image_resolver import (
    ContentStore, Resolver, RuleSet, Platform, ImageReference,
)
from tests.conftest import make_manifest  # 或自行构造 Manifest/blobs
```

## 测试

```bash
uv run pytest -q   # 70 tests
```

## 文档

完整的**匹配规则、回退顺序、策略语义、原子性与缓存保证、错误码**见
[`docs/design.md`](docs/design.md)。

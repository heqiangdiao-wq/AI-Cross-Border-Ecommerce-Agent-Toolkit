# Templates · 跨模块通用模板库

这里的模板**不绑定具体 Agent**，可以被多个模块共用。

| 文件 | 用途 | 主要使用者 |
|---|---|---|
| [`product_brief_template.md`](./product_brief_template.md) | **标准中间产物**——三个模块之间的通信契约 | 03 / 04 / 05 |
| [`listing_asset_pack.md`](./listing_asset_pack.md) | 上架成品的目录结构、交付清单和审核记录 | 03 / 04 / 05 / 运营 |
| [`keyword_analysis_template.md`](./keyword_analysis_template.md) | 关键词三榜分析表（SOP 1.8） | 03（上游可来自 01） |
| [`cost_pricing_template.md`](./cost_pricing_template.md) | 定价核算模型（SOP 1.9 / 3.2） | 02（Phase 2） |
| [`output_qa_checklist.md`](./output_qa_checklist.md) | 交付自检清单（可打印版） | 全部 |

---

## 与 Agent 内模板的区别

| | 顶层 `Templates/` | Agent 内 `templates/` |
|---|---|---|
| 范围 | 跨模块共用 | 单模块专用 |
| 稳定性 | 改动会影响多个模块 | 改只影响本模块 |
| 例子 | `product_brief_template` | `input_image_brief` |

> **规则**：如果一个模板只被一个 Agent 使用，放在该 Agent 目录下；如果被两个以上使用，提到这里。

---

## 中间产物契约

三个 Phase 1 模块之间靠 `product_brief_template.md` 定义的字段通信：

```text
03 Listing Creative ──► product_brief ──► 04 Product Image
                                      └──► 05 Product Video
```

**阻塞规则**：`core_selling_point` 或 `forbidden_words` 为空时，下游模块必须拒绝执行并报错，不得自行推测。

---

**返回**：[项目首页](../README.md) · [架构拆解](../docs/00_Architecture_SOP_to_Agent.md)

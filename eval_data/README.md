# 评估用数据集

## 来源

ModelScope 上三个同系列的中文电商客服对话数据集，全部 Apache License 2.0：

| 文件 | 数据集 | 条数 | 内容 |
|---|---|---|---|
| `after.jsonl` | [QingshanAI/ecom-after-sale-synthetic](https://www.modelscope.cn/datasets/QingshanAI/ecom-after-sale-synthetic) | 997 | 退换货、质量问题、维权投诉、差评、补偿方案 |
| `logistics.jsonl` | [QingshanAI/ecom-logistics-synthetic](https://www.modelscope.cn/datasets/QingshanAI/ecom-logistics-synthetic) | 987 | 物流停滞、丢件破损、快递时效、催发 |
| `service.jsonl` | [QingshanAI/ecom-customer-service-synthetic](https://www.modelscope.cn/datasets/QingshanAI/ecom-customer-service-synthetic) | 992 | 咨询、售后、产品咨询、价格、支付、促销 |

共 2976 条。

## 字段

```json
{"scene": "退款", "user": "买的东西有问题，可以退吗？", "bot": "亲，我们可以为您办理退款……", "tone": "安抚"}
```

- `scene`：业务场景
- `user`：**顾客提问**（评估用的就是这一列）
- `bot`：客服回复
- `tone`：回复语气

## 性质

**由本地大模型合成生成**，不含真实用户隐私。原始仓库 README 明确说明。

合成语料的局限：措辞可能不如真实顾客多样、个别语句有模板感。作为检索策略的对比基准够用，
但结论不宜直接外推到真实业务分布。

## 用途

r13 阶段评估四种检索策略（纯向量 / 纯 BM25 / 混合 / 混合+Rerank）。
抽样 500 条并人工标注「应命中哪条政策」，见 `docs/eval/r13-queries.csv`。

## 重新获取

```bash
for name in QingshanAI/ecom-after-sale-synthetic \
            QingshanAI/ecom-logistics-synthetic \
            QingshanAI/ecom-customer-service-synthetic; do
  curl -sL "https://www.modelscope.cn/api/v1/datasets/${name}/repo?Revision=master&FilePath=data.jsonl" \
    -o "$(basename $name | sed 's/ecom-//;s/-synthetic//').jsonl"
done
```

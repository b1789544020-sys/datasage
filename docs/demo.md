# DataSage Web UI 演示：从幻觉初稿到可回溯答案

> 本页录像来自真实本地 Web UI 操作。使用 mock provider 和 `DATASAGE_DEMO_HALLUCINATION=true` 确定性触发反幻觉链路，**不调用上游模型 API**；pandas 沙箱执行、结果验证、答案重写、持久化和证据定位均走真实应用代码。

## 视频

<video controls playsinline preload="metadata" poster="demo/web/03-hallucination-rewrite.png" src="demo/web/DataSage-demo.mp4" width="100%"></video>

**[直接打开 / 下载 MP4](demo/web/DataSage-demo.mp4)** · [项目源码](../README.md) · [终端版演示 GIF](demo/anti-hallucination.gif)

录像流程：加载 `sample_sales.csv` → 输入自然语言问题 → 查看真实沙箱结果 → 对比红色幻觉初稿与绿色最终答案 → 点击数字 `300` 打开证据抽屉。

## 这次运行实际发生了什么

示例数据共 **6 行、5 列**。问题为：

> 各地区销售额汇总，总销售额是多少？哪个地区最高？

应用生成并执行 pandas 代码，得到：

| region | sales_amount |
| --- | ---: |
| 西南 | 300 |
| 华东 | 150 |
| 华南 | 80 |
| 华北 | 30 |

随后，演示开关让 mock 初稿故意加入 `8932 元` 和 `46%`。数字回溯器在执行结果中找不到这两个数字，拦截初稿并触发 **1 轮自动重写**。最终答案为：

> 共有 4 个分组。排名第一是西南（sales_amount 300）。排名第二是华东（sales_amount 150）。

最终答案的 **5/5 个数字均可回溯**，`verified=true`、`hallucination_count=0`。

## 关键截图

### 1. 加载示例数据并提问

![示例数据 schema 与问题输入](demo/web/01-data-and-question.png)

### 2. pandas 沙箱真实执行

![沙箱执行结果](demo/web/02-sandbox-result.png)

### 3. 幻觉初稿被拦截并重写

![幻觉初稿与最终答案对比](demo/web/03-hallucination-rewrite.png)

### 4. 数字证据下钻

点击最终答案中的 `300`，证据抽屉定位到结果表第 1 行 `sales_amount` 列并高亮单元格。

![数字证据抽屉](demo/web/04-numeric-evidence.png)

## 演示边界

mock 模式用于离线、可重复地展示产品交互和可信分析链路；幻觉初稿是**刻意注入的演示输入**。这里的 `5/5` 仅描述本次内置样本最终答案的数字回溯结果，**不代表真实模型准确率、通用反幻觉能力或生产环境表现**。

"""LLM 客户端：httpx 直连 OpenAI 兼容的 /chat/completions（不使用官方 SDK）。

由 DATASAGE_LLM_PROVIDER 控制三种 provider：
- mock      本地桩，不发起任何网络请求，返回"看起来像真的"预设回复，离线开发用；
- openai    OpenAI 兼容协议（DeepSeek / 通义千问 / Moonshot 均兼容，仅 base_url 不同）；
- anthropic 按项目约定同样走 OpenAI 兼容端点（通过兼容网关 / 反向代理接入）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger("app.llm")

# ------------------------------- 常量 -------------------------------

SUPPORTED_PROVIDERS = ("mock", "openai", "anthropic")

MAX_RETRIES = 3              # 总尝试次数（首次 + 2 次重试）
BACKOFF_BASE_SECONDS = 1.0   # 指数退避：1s、2s、4s
REQUEST_TIMEOUT = httpx.Timeout(60.0, connect=10.0)

# 这些 HTTP 状态码值得重试（限流 / 网关抖动 / 临时性错误）
RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504}

# 模型输出裹 markdown 代码块时的围栏：```json ... ```
_FENCE_START_RE = re.compile(r"^\s*```[a-zA-Z0-9_-]*\s*\n?")
_FENCE_END_RE = re.compile(r"\n?\s*```\s*$")


class LLMClient:
    """统一的对话补全客户端。

    Args:
        provider: mock / openai / anthropic。
        api_key: Bearer 令牌（mock 可为空）。
        base_url: 服务根地址，如 https://api.deepseek.com/v1（不带尾部斜杠）。
        model: 模型名，如 deepseek-chat。
    """

    def __init__(
        self,
        provider: str,
        api_key: str,
        base_url: str,
        model: str,
        *,
        _transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        provider_norm = (provider or "mock").strip().lower()
        if provider_norm not in SUPPORTED_PROVIDERS:
            raise ValueError(
                f"不支持的 LLM provider: {provider!r}，"
                f"可选 {SUPPORTED_PROVIDERS}"
            )
        self.provider = provider_norm
        self.api_key = api_key or ""
        self.base_url = (base_url or "").rstrip("/")
        self.model = model
        self._transport = _transport  # 仅测试时注入（如 httpx.MockTransport）

    # ----------------------------- 对外主接口 -----------------------------

    async def chat(
        self, messages: list[dict[str, str]], temperature: float = 0.0
    ) -> str:
        """发送多轮对话消息，返回模型生成的纯文本内容。

        - mock：本地生成预设回复，不联网；
        - 真实 provider：调用 OpenAI 兼容 /chat/completions，
          最多尝试 3 次，指数退避（1s/2s/4s），并打印每次调用的 token 消耗。
        """
        if self.provider == "mock":
            return self._mock_chat(messages, temperature)
        return await self._chat_remote(messages, temperature)

    async def chat_json(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        """调用 chat 并把文本解析为 JSON 对象（temperature 固定 0.0）。

        解析失败时自动补救一次：把模型上一次的非法输出原样回传，
        并明确要求"只输出合法 JSON"。再次失败则抛 ValueError。
        """
        raw = await self.chat(messages, temperature=0.0)
        try:
            return self._parse_json(raw)
        except ValueError:
            logger.warning("模型输出无法解析为 JSON，触发 1 次修复重试（要求只输出合法 JSON）")
            repair_messages = [
                *messages,
                {"role": "assistant", "content": raw},
                {
                    "role": "user",
                    "content": (
                        "你上一次的回复不是合法 JSON（可能包含 Markdown 代码块、"
                        "解释文字或尾注释）。请重新输出，并且只输出一个严格合法的 "
                        "JSON 对象：使用双引号、不要换行注释、不要 ``` 代码块、"
                        "不要任何 JSON 之外的文字。"
                    ),
                },
            ]
            repaired = await self.chat(repair_messages, temperature=0.0)
            try:
                return self._parse_json(repaired)
            except ValueError as exc:
                snippet = (raw or "").strip().replace("\n", " ")[:200]
                raise ValueError(
                    f"模型重试后仍未返回合法 JSON：{exc}；首次输出前 200 字：{snippet!r}"
                ) from exc


    # ----------------------------- mock provider -----------------------------

    @staticmethod
    def _last_user_text(messages: list[dict[str, str]]) -> str:
        for msg in reversed(messages):
            if msg.get("role") == "user":
                return str(msg.get("content", ""))
        return str(messages[-1].get("content", "")) if messages else ""

    def _mock_chat(
        self, messages: list[dict[str, str]], temperature: float
    ) -> str:
        """离线桩：针对 codegen 场景返回一个结构完整、可解析、可执行的 JSON。

        内容会引用用户问题，并生成"按维度列聚合指标列"的通用 pandas 代码，
        保证包含 load_dataframe() 与 RESULT，行为尽量贴近真实模型。
        另外识别两类"写总结"场景（app.analysis.answer / verifier 重生成），
        只引用所给真实结果里的数字，保证 mock 全链路离线可跑且可通过数字回溯。
        """
        last_user_text = self._last_user_text(messages)
        system_text = "\n".join(
            str(m.get("content", ""))
            for m in messages
            if m.get("role") == "system"
        )
        if "「最终答案撰写」" in system_text:
            # app.analysis.answer：user 消息里带【真实执行结果 JSON】
            if settings.demo_hallucination:
                # 反幻觉演示模式：初稿刻意写入"回溯器已自检、数据中必无证据"
                # 的编造数字；重生成轮走下面的「重写总结」分支返回真实答案。
                hallucinated = self._mock_hallucinated_draft(last_user_text)
                if hallucinated is not None:
                    logger.info(
                        "mock 演示模式：答案初稿包含刻意编造的不可回溯数字，"
                        "等待数字回溯器拦截并触发自动重生成"
                    )
                    return hallucinated
            return self._mock_final_answer(last_user_text)
        if "重写总结" in system_text or "真实数据如下" in last_user_text:
            # verifier.verify_and_regenerate：user 消息里带纯文本结果摘要
            return self._mock_regeneration_answer(last_user_text)
        # codegen 的 user 消息是"schema + 【用户问题】... + 输出要求"，
        # 模拟真实模型只引用问题本身；抽不到则退化为整条消息的前 80 字
        match = re.search(r"【用户问题】\s*\n(.+?)(?:\n\s*\n|$)", last_user_text, re.DOTALL)
        question = match.group(1).strip() if match else last_user_text[:80]
        # 多轮追问：user 消息中带 codegen 注入的【多轮对话上下文】块
        followup = "【多轮对话上下文】" in last_user_text
        if "合法" in last_user_text and "JSON" in last_user_text:
            # chat_json 修复重试场景：返回一个最小合法 JSON
            payload = json.dumps(
                {
                    "plan": "识别维度列与指标列，分组聚合并排序。",
                    "code": (
                        "import pandas as pd\n"
                        "df = load_dataframe()\n"
                        "RESULT = df.head(10)"
                    ),
                    "result_format": "table",
                },
                ensure_ascii=False,
            )
        elif followup:
            # 追问场景：沿用上一轮维度 / 指标口径，并在问题文本里定位被点名的
            # 维度取值（如"那华东呢"里的"华东"），命中则先筛选再聚合，
            # 模拟真实模型对上下文指代的消解；json.dumps 输出可直接当 Python 字面量
            question_literal = json.dumps(question, ensure_ascii=True)
            plan = (
                f"这是针对上一轮地区分析的追问「{question[:80]}」：沿用相同的维度 / "
                "指标口径，先在问题文本中定位被点名的维度取值（如某个地区），"
                "筛选该取值后分组求和、降序排列，避免重复上一轮的全量分析。"
            )
            code = (
                "import pandas as pd\n"
                "import numpy as np\n"
                "\n"
                "df = load_dataframe()\n"
                f"question_text = {question_literal}\n"
                "\n"
                "# 与首轮一致地识别维度列与指标列\n"
                "dimension_candidates = [\n"
                "    c for c in df.columns\n"
                "    if any(k in str(c).lower()\n"
                "           for k in ('地区', '区域', '省份', '城市', 'region', 'city', 'area'))\n"
                "]\n"
                "metric_candidates = [\n"
                "    c for c in df.columns\n"
                "    if any(k in str(c).lower()\n"
                "           for k in ('销售额', '金额', '销量', 'amount', 'sales', 'revenue'))\n"
                "]\n"
                "dimension = dimension_candidates[0] if dimension_candidates else df.columns[0]\n"
                "metric = metric_candidates[0] if metric_candidates else df.columns[-1]\n"
                "\n"
                "# 追问消解：在文本型列里找出问题中点名的取值（如华东），命中则筛选\n"
                "sub = df\n"
                "for col in df.select_dtypes(include=['object', 'str']).columns:\n"
                "    values = [str(v) for v in df[col].dropna().astype(str).unique()]\n"
                "    hit = [v for v in values if v and v in question_text]\n"
                "    if hit:\n"
                "        sub = df[df[col].astype(str).isin(hit)]\n"
                "        break\n"
                "\n"
                "RESULT = (\n"
                "    sub.dropna(subset=[dimension, metric])\n"
                "    .groupby(dimension, as_index=False)[metric]\n"
                "    .sum()\n"
                "    .sort_values(metric, ascending=False)\n"
                "    .reset_index(drop=True)\n"
                ")\n"
            )
            payload = json.dumps(
                {"plan": plan, "code": code, "result_format": "table"},
                ensure_ascii=False,
            )
        else:
            plan = (
                f"针对问题「{question[:80]}」，计划："
                "1) 用 load_dataframe() 载入数据并确认列；"
                "2) 自动识别地区等分类维度列与销售额等数值指标列；"
                "3) 按维度分组对指标求和、降序排列；"
                "4) 将汇总表赋值给 RESULT。"
            )
            code = (
                "import pandas as pd\n"
                "import numpy as np\n"
                "\n"
                "df = load_dataframe()\n"
                "\n"
                "# 根据 schema 里的列名自动识别维度列与指标列\n"
                "dimension_candidates = [\n"
                "    c for c in df.columns\n"
                "    if any(k in str(c).lower()\n"
                "           for k in ('地区', '区域', '省份', '城市', 'region', 'city', 'area'))\n"
                "]\n"
                "metric_candidates = [\n"
                "    c for c in df.columns\n"
                "    if any(k in str(c).lower()\n"
                "           for k in ('销售额', '金额', '销量', 'amount', 'sales', 'revenue'))\n"
                "]\n"
                "\n"
                "if dimension_candidates and metric_candidates:\n"
                "    dimension, metric = dimension_candidates[0], metric_candidates[0]\n"
                "    RESULT = (\n"
                "        df.dropna(subset=[dimension, metric])\n"
                "        .groupby(dimension, as_index=False)[metric]\n"
                "        .sum()\n"
                "        .sort_values(metric, ascending=False)\n"
                "        .reset_index(drop=True)\n"
                "    )\n"
                "else:\n"
                "    RESULT = df.head(100)\n"
            )
            payload = json.dumps(
                {"plan": plan, "code": code, "result_format": "table"},
                ensure_ascii=False,
            )

        # mock 也打印"token 消耗"，方便离线时核对日志链路
        prompt_chars = sum(len(str(m.get("content", ""))) for m in messages)
        logger.info(
            "LLM 调用完成 provider=mock model=%s 状态=成功 "
            "prompt_tokens≈%d completion_tokens≈%d total_tokens≈%d (字符估算)",
            self.model,
            prompt_chars,
            len(payload),
            prompt_chars + len(payload),
        )
        return payload

    # ---------------------- mock：最终答案 / 重写总结 -----------------------

    # 序数词与表行数做匹配，所以引用"第一/第二"是安全的
    _MOCK_CN_RANKS = ("第一", "第二", "第三")
    _MOCK_DIGITS_IN_LABEL_RE = re.compile(r"[0-9０-９]+(?:\.[0-9.]+)?")

    def _mock_compose_answer(self, result_data: Any) -> str:
        """只用真实结果里的数字拼一段可通过数字回溯的中文总结。"""
        if not isinstance(result_data, dict):
            return "本次分析没有产生可引用的结果。"
        rtype = result_data.get("type")

        is_table = rtype == "table" or (
            isinstance(result_data.get("data"), list)
            and isinstance(result_data.get("columns"), list)
        )
        if is_table:
            columns = [str(c) for c in result_data.get("columns", [])]
            rows = [
                row
                for row in result_data.get("data", [])
                if isinstance(row, (list, tuple))
            ]
            if not rows:
                return "分析结果为空，没有可引用的数据行。"
            total = int(result_data.get("total_rows") or len(rows))
            # 计数正则要求数字与量词紧贴，"共有4个分组"不能写成"共有 4 个分组"
            parts = [f"共有{total}个分组。"]
            for idx, row in enumerate(rows[:2]):
                rank = (
                    self._MOCK_CN_RANKS[idx]
                    if idx < len(self._MOCK_CN_RANKS)
                    else f"第{idx + 1}"
                )
                label = row[0] if row else ""
                metrics = []
                for col_idx, value in enumerate(row):
                    if value is None or col_idx >= len(columns):
                        continue
                    # 列名里的数字不是结果主张，剔掉避免无法回溯
                    col_name = self._MOCK_DIGITS_IN_LABEL_RE.sub(
                        "", columns[col_idx]
                    ).strip()
                    if col_idx == 0:
                        continue  # 维度值单独放在"是……"里
                    metrics.append(
                        f"{col_name} {value}".strip() if col_name else str(value)
                    )
                # 维度标签含数字（如月份）时退回"列名 + 值"的朴素写法，避免引入
                # 无法回溯的数字
                if isinstance(label, str) and not re.search(r"[0-9]", label):
                    parts.append(
                        f"排名{rank}是{label}（" + "，".join(metrics) + "）。"
                    )
                else:
                    cells = ([str(label)] if label != "" else []) + metrics
                    parts.append(f"排名{rank}：" + "，".join(cells) + "。")
            return "".join(parts)

        if rtype == "single_value":
            return f"计算结果是 {result_data.get('value')}。"

        # record（executor 对 dict 结果不加 type 包装）：只引用标量叶子
        if rtype is None:
            leaves: list[str] = []

            def walk(obj: Any, path: str) -> None:
                if isinstance(obj, dict):
                    for key, val in obj.items():
                        walk(val, f"{path}.{key}" if path else str(key))
                elif isinstance(obj, (list, tuple)):
                    return  # 列表叶子不进 mock 总结，避免臆造口径
                elif obj is not None and not isinstance(obj, bool):
                    label = self._MOCK_DIGITS_IN_LABEL_RE.sub("", path)
                    leaves.append(f"{label} 为 {obj}")

            walk(result_data, "")
            if leaves:
                return "关键结果：" + "，".join(leaves[:6]) + "。"
        return "本次分析没有产生可引用的数值结果。"

    # 演示模式候选池：刻意编造、与常见业务数据不重合的数字
    _DEMO_FAKE_TOTALS = (8932, 7268, 6415, 9573, 5387, 11860, 13240)
    _DEMO_FAKE_PERCENTS = (46, 35, 73, 58, 67, 91, 39, 82, 127, 163, 219)

    def _mock_hallucinated_draft(self, user_text: str) -> str | None:
        """演示模式：生成一段"听起来合理、但含有编造数字"的答案初稿。

        所有候选数字都会先用真正的 NumericBacktracker 自检：只有在真实
        结果里确实找不到证据的数字才会被写进初稿，保证演示时幻觉 100%
        被抓出，也保证不会把真实可回溯的数字误标成幻觉。任何一步自检
        失败就放弃注入（返回 None，回退为真实答案），绝不破坏主链路。
        """
        marker = "【真实执行结果 JSON】"
        idx = user_text.find(marker)
        if idx == -1:
            return None
        sub = user_text[idx + len(marker):]
        start = sub.find("{")
        if start == -1:
            return None
        try:
            result_data, _ = json.JSONDecoder().raw_decode(sub, start)
        except json.JSONDecodeError:
            return None
        if not isinstance(result_data, dict):
            return None

        # 局部导入：verifier 不反向依赖 llm 包，无循环引用
        from app.analysis.verifier import NumericBacktracker

        bt = NumericBacktracker()

        def untraceable(raw: str) -> bool:
            nums = bt.extract_numbers(raw)
            if not nums:
                return False
            return all(v["found"] is False for v in bt.backtrack(nums, result_data))

        # 取一个真实的文字标签（如销售额最高的地区名），让初稿"半真半假"
        top_label = ""
        if result_data.get("type") == "table" and isinstance(
            result_data.get("data"), list
        ):
            for row in result_data["data"]:
                if isinstance(row, list) and row and isinstance(row[0], str):
                    top_label = row[0]
                    break

        clauses: list[str] = []
        fake_total = next(
            (v for v in self._DEMO_FAKE_TOTALS if untraceable(str(v))), None
        )
        if fake_total is not None:
            clauses.append(f"全年总销售额约为 {fake_total} 元")
        fake_pct = next(
            (p for p in self._DEMO_FAKE_PERCENTS if untraceable(f"{p}%")), None
        )
        if fake_pct is not None:
            clauses.append(f"同比增长 {fake_pct}%")
        if not clauses:
            return None
        tail = (
            f"，其中{top_label}销售额最高。"
            if top_label
            else "，整体表现超出预期。"
        )
        draft = "经统计，" + "，".join(clauses) + tail

        # 最终自检：初稿必须至少含一个无法回溯的数字，否则不注入
        report = bt.verify_answer(draft, result_data)
        if report["hallucination_count"] < 1:
            return None
        return draft

    def _mock_final_answer(self, user_text: str) -> str:
        """答案撰写场景：从【真实执行结果 JSON】片段还原结果并拼总结。"""
        marker = "【真实执行结果 JSON】"
        idx = user_text.find(marker)
        if idx != -1:
            sub = user_text[idx + len(marker):]
            start = sub.find("{")
            if start != -1:
                try:
                    result_data, _ = json.JSONDecoder().raw_decode(sub, start)
                    return self._mock_compose_answer(result_data)
                except json.JSONDecodeError:
                    pass
        return "分析已完成，但结果中没有可直接引用的数值。"

    @staticmethod
    def _mock_loose_scalar(text: str) -> Any:
        """重生成 prompt 里的文本单元格还原：能转数字就转，否则保留字符串。"""
        if text == "":
            return None
        try:
            num = float(text)
        except ValueError:
            return text
        return int(num) if num.is_integer() else num

    def _mock_regeneration_answer(self, user_text: str) -> str:
        """重写场景：解析 verifier 喂的纯文本表格 / `value = ...` 后按真实数字重写。"""
        anchor = "真实数据如下（前若干行）："
        body = user_text.split(anchor, 1)[1] if anchor in user_text else user_text
        lines = [line.strip() for line in body.splitlines() if line.strip()]
        if lines and lines[0].startswith("value ="):
            return f"计算结果是 {lines[0][len('value ='):].strip()}。"
        if len(lines) >= 2 and "|" in lines[0]:
            columns = [c.strip() for c in lines[0].split("|")]
            data_rows: list[list[Any]] = []
            for line in lines[1:]:
                if line.startswith(("（", "(")) or "|" not in line:
                    break
                data_rows.append(
                    [self._mock_loose_scalar(c.strip()) for c in line.split("|")]
                )
            total_match = re.search(r"共\s*(\d+)\s*行", body)
            total = int(total_match.group(1)) if total_match else len(data_rows)
            return self._mock_compose_answer(
                {
                    "type": "table",
                    "columns": columns,
                    "data": data_rows,
                    "total_rows": total,
                }
            )
        return "重新核对后，结果中没有可引用的数字，本结论不包含数值主张。"

    # ----------------------------- 真实 provider -----------------------------

    async def _chat_remote(
        self, messages: list[dict[str, str]], temperature: float
    ) -> str:
        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
        }

        last_error: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            started_at = time.monotonic()
            retriable = False
            detail = ""
            try:
                async with httpx.AsyncClient(
                    timeout=REQUEST_TIMEOUT, transport=self._transport
                ) as client:
                    response = await client.post(url, headers=headers, json=body)

                if response.status_code in RETRYABLE_STATUS:
                    raise httpx.HTTPStatusError(
                        f"LLM 服务返回可重试状态码 {response.status_code}: "
                        f"{response.text[:200]}",
                        request=response.request,
                        response=response,
                    )
                response.raise_for_status()  # 其余 4xx 直接失败，不重试

                data = response.json()
                content = data["choices"][0]["message"]["content"]
                usage = data.get("usage") or {}
                logger.info(
                    "LLM 调用完成 provider=%s model=%s 状态=成功 尝试=%d/%d "
                    "耗时=%.2fs prompt_tokens=%s completion_tokens=%s total_tokens=%s",
                    self.provider, self.model, attempt, MAX_RETRIES,
                    time.monotonic() - started_at,
                    usage.get("prompt_tokens", "?"),
                    usage.get("completion_tokens", "?"),
                    usage.get("total_tokens", "?"),
                )
                return str(content)

            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error, retriable, detail = exc, True, f"{type(exc).__name__}: {exc}"
            except httpx.HTTPStatusError as exc:
                last_error = exc
                retriable = exc.response.status_code in RETRYABLE_STATUS
                detail = f"HTTP {exc.response.status_code}"

            if attempt >= MAX_RETRIES or not retriable:
                logger.error(
                    "LLM 调用失败 provider=%s model=%s 尝试=%d/%d 错误=%s",
                    self.provider, self.model, attempt, MAX_RETRIES, detail,
                )
                # 注意：此处已在 except 块之外，不能用裸 raise（无活动异常）
                raise last_error

            wait_seconds = BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
            logger.warning(
                "LLM 调用失败 provider=%s model=%s 尝试=%d/%d 错误=%s；%.0fs 后指数退避重试",
                self.provider, self.model, attempt, MAX_RETRIES,
                detail, wait_seconds,
            )
            await asyncio.sleep(wait_seconds)

        # 理论不可达（循环内要么 return 要么 raise）
        raise RuntimeError("LLM 调用异常退出") from last_error

    # ----------------------------- JSON 解析 -----------------------------

    @staticmethod
    def _parse_json(raw: str) -> dict[str, Any]:
        """宽容解析模型输出的 JSON：去围栏、截取最外层花括号。"""
        if raw is None or not str(raw).strip():
            raise ValueError("模型输出为空")
        text = str(raw).strip()
        text = _FENCE_START_RE.sub("", text)
        text = _FENCE_END_RE.sub("", text).strip()

        candidates = [text]
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            candidates.append(text[start : end + 1])

        parsed: Any = None
        last_exc: Exception | None = None
        for candidate in candidates:
            try:
                parsed = json.loads(candidate)
                break
            except json.JSONDecodeError as exc:
                last_exc = exc
        else:
            raise ValueError(f"无法解析为 JSON：{last_exc}")

        if not isinstance(parsed, dict):
            raise ValueError(f"JSON 根节点应为对象，实际为 {type(parsed).__name__}")
        return parsed


def get_llm_client() -> LLMClient:
    """根据全局 settings 构造 LLM 客户端。"""
    return LLMClient(
        provider=settings.llm_provider,
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
        model=settings.llm_model,
    )


# ============================== 自测入口 ==============================

if __name__ == "__main__":
    import asyncio

    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s | %(message)s"
    )

    async def main() -> None:
        # ---- 场景 1：mock provider，chat + chat_json 全离线 ----
        mock_client = LLMClient("mock", "", "", "mock-1")
        text = await mock_client.chat(
            [{"role": "user", "content": "各地区销售额总和"}]
        )
        print("===== mock chat 原始输出 =====")
        print(text)
        obj = await mock_client.chat_json(
            [{"role": "user", "content": "各地区销售额总和"}]
        )
        assert set(obj) == {"plan", "code", "result_format"}
        assert "load_dataframe()" in obj["code"] and "RESULT" in obj["code"]
        print("\n[1/2] mock chat / chat_json 通过 OK")

        # ---- 场景 2：MockTransport 模拟 503×2 后成功，验证重试+退避+token 日志 ----
        attempts = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            attempts["n"] += 1
            if attempts["n"] < 3:
                return httpx.Response(503, json={"error": "service busy"})
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": '{"ok": true}'}}],
                    "usage": {
                        "prompt_tokens": 11,
                        "completion_tokens": 7,
                        "total_tokens": 18,
                    },
                },
            )

        remote = LLMClient(
            "openai",
            "sk-test",
            "https://llm.example.com/v1",
            "test-model",
            _transport=httpx.MockTransport(handler),
        )
        started = time.monotonic()
        reply = await remote.chat([{"role": "user", "content": "ping"}])
        elapsed = time.monotonic() - started
        assert reply == '{"ok": true}', reply
        assert attempts["n"] == 3, attempts
        assert elapsed >= 2.9, f"指数退避耗时不足: {elapsed:.2f}s"  # 1s + 2s
        print(f"[2/2] 503×2→200 重试通过 OK（尝试 {attempts['n']} 次，耗时 {elapsed:.1f}s）")

        print("\n全部自测通过")

    asyncio.run(main())



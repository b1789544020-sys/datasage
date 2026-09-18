#!/usr/bin/env python3
"""DataSage 反幻觉端到端演示 + 演示动图录制。

做了什么（全程离线，mock provider，不需要任何 API Key）：
  1. 上传 data/demo_sales.csv（事实极其简单的小数据集）；
  2. 自然语言提问，走完整链路：LLM 生成 pandas -> 沙箱执行 -> 三层验证；
  3. 演示模式（DATASAGE_DEMO_HALLUCINATION=true）下，mock LLM 的第一版答案
     刻意写入数据中不存在的数字（候选数字经真实回溯器自检，保证必被抓）；
  4. NumericBacktracker 数字回溯抓出幻觉 -> 自动带证据重生成 ->
     最终答案每个数字都能回溯到沙箱真实结果；
  5. 终端打印红蓝对照过程，并用 Pillow 录制成 GIF：docs/demo/anti-hallucination.gif。

用法（在 backend/ 目录下）：
    python scripts/demo_anti_hallucination.py
    python scripts/demo_anti_hallucination.py --no-gif
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

# 必须在 import app.* 之前设置：pydantic-settings 在导入时读取环境变量
os.environ.setdefault("DATASAGE_DEMO_HALLUCINATION", "true")

BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_ROOT.parent
sys.path.insert(0, str(BACKEND_ROOT))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

from app.analysis.verifier import NumericBacktracker  # noqa: E402
from app.database import get_session  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Base  # noqa: E402
from app.services import datasource_service  # noqa: E402

DEMO_CSV = BACKEND_ROOT / "data" / "demo_sales.csv"
QUESTION = "各地区销售额汇总，总销售额是多少？哪个地区最高？"
OUT_DIR = REPO_ROOT / "docs" / "demo"
GIF_PATH = OUT_DIR / "anti-hallucination.gif"
TRANSCRIPT_PATH = OUT_DIR / "transcript.txt"

ANSI = {
    "text": "\033[38;5;252m", "dim": "\033[38;5;244m",
    "blue": "\033[38;5;75m", "green": "\033[38;5;76m",
    "red": "\033[38;5;203m", "amber": "\033[38;5;179m",
    "purple": "\033[38;5;141m", "white": "\033[97m",
}
RESET = "\033[0m"
RGB = {
    "text": (201, 209, 219), "dim": (125, 133, 146),
    "blue": (88, 166, 255), "green": (63, 185, 80),
    "red": (248, 81, 73), "amber": (210, 153, 34),
    "purple": (190, 140, 255), "white": (240, 246, 252),
}

# 一行 = 若干 (文本, 颜色)；一个 scene = 若干行（逐行动画出现）
Line = list[tuple[str, str]]
Scene = list[Line]


def line(text: str, color: str = "text") -> Line:
    return [(text, color)]


async def _init_db(engine: Any) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


# ----------------------- 真实跑一遍全链路 -----------------------

def run_demo() -> dict[str, Any]:
    """用隔离的临时 SQLite / 上传目录跑通 上传 -> 提问 完整链路。"""
    tmp_dir = Path(tempfile.mkdtemp(prefix="datasage_demo_"))
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_dir / 'demo.db').as_posix()}",
        poolclass=NullPool,
    )
    asyncio.run(_init_db(engine))
    factory = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)

    async def override_get_session() -> Any:
        async with factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session
    upload_dir = tmp_dir / "uploads"
    upload_dir.mkdir()
    old_upload_dir = datasource_service.UPLOAD_DIR
    datasource_service.UPLOAD_DIR = upload_dir
    try:
        client = TestClient(app)  # 不触发 lifespan，避免碰开发库
        with open(DEMO_CSV, "rb") as fh:
            up = client.post(
                "/api/v1/datasources",
                files={"file": (DEMO_CSV.name, fh, "text/csv")},
            )
        if up.status_code != 201:
            raise SystemExit(f"上传失败：{up.status_code} {up.text}")
        datasource = up.json()

        resp = client.post(
            "/api/v1/query",
            json={"datasource_id": datasource["id"], "question": QUESTION},
        )
        if resp.status_code != 200:
            raise SystemExit(f"提问失败：{resp.status_code} {resp.text}")
        body = resp.json()
    finally:
        app.dependency_overrides.clear()
        datasource_service.UPLOAD_DIR = old_upload_dir
        asyncio.run(engine.dispose())

    answer_meta = next(
        step["meta"] for step in body["steps"] if step["step_type"] == "answer"
    )
    initial = answer_meta.get("initial_answer") or ""
    # meta.hallucinations 是"最终仍残留"的幻觉（修复成功时为空）；
    # 被自动修复移除的数字记录在 regeneration_history[].removed_hallucinations
    removed_raws: list[str] = []
    for item in answer_meta.get("regeneration_history") or []:
        removed_raws.extend(item.get("removed_hallucinations") or [])
    rounds = int(answer_meta.get("regeneration_rounds") or 0)

    # 用真实回溯器给初稿打数字级判定（红色高亮用）
    initial_verdicts = NumericBacktracker().verify_answer(
        initial, body["result_data"]
    )["verified_numbers"]

    facts = {
        "datasource": datasource,
        "question": QUESTION,
        "plan": body["plan"],
        "result": body["result_data"],
        "initial": initial,
        "initial_verdicts": initial_verdicts,
        "hallucination_raws": removed_raws,
        "rounds": rounds,
        "answer": body["answer"],
        "verdicts": body["answer_numbers"],
        "verified": body["verified"],
        "hallucination_count": body["hallucination_count"],
    }

    # ---- 硬性断言：演示场景必须"先翻车、后修复" ----
    if not initial or not facts["hallucination_raws"]:
        raise SystemExit("演示失败：第一版答案没有产生幻觉数字，请确认演示开关已打开")
    if rounds < 1:
        raise SystemExit("演示失败：检出幻觉后未触发自动重生成")
    if not facts["verified"] or facts["hallucination_count"] != 0:
        raise SystemExit("演示失败：最终答案仍存在无法回溯的数字")
    if not facts["verdicts"] or any(not v["found"] for v in facts["verdicts"]):
        raise SystemExit("演示失败：最终答案存在未通过数字回溯的数字")
    return facts


# ----------------------- 分镜构造 -----------------------

def _char_w(ch: str) -> int:
    return 2 if ord(ch) > 0x2E80 else 1


def wrap_segments(segments: Line, limit: int = 100) -> list[Line]:
    """按显示宽度折行（CJK 算 2 列），保留颜色；句读处优先断行。

    自动识别行首空格作为续行的悬挂缩进；全流程只在此处折行一次。
    """
    # 续行悬挂缩进 = 本行首个非空字符前的空格数
    hang = 0
    for text, _ in segments:
        stripped = text.lstrip(" ")
        if stripped == "":
            hang += len(text)
            continue
        hang += len(text) - len(stripped)
        break

    wrapped: list[list[tuple[str, str]]] = []
    current: list[tuple[str, str]] = []
    width = 0

    def flush() -> None:
        nonlocal current, width
        wrapped.append(current)
        current, width = [], 0
        if hang:
            current.append((" " * hang, "dim"))
            width += hang

    for text, color in segments:
        for ch in text:
            if ch == "\n":
                flush()
                continue
            # 句末标点后优先断行，避免把 region/sales_amount 这类标识符拦腰截断
            if ch in "。！？；" and width >= 52:
                current.append((ch, color))
                width += _char_w(ch)
                flush()
                continue
            if ch in "，、" and width >= 88:
                current.append((ch, color))
                width += _char_w(ch)
                flush()
                continue
            # 闭合标点不允许出现在行首：宁可超出 1~2 列也把它留在上一行
            if (
                width + _char_w(ch) > limit
                and width > hang
                and ch not in "。，、；：！？）"
            ):
                flush()
            current.append((ch, color))
            width += _char_w(ch)
    if current and "".join(t for t, _ in current).strip():
        wrapped.append(current)
    merged: list[Line] = []
    for raw_line in wrapped:
        segs: Line = []
        for ch, color in raw_line:
            if segs and segs[-1][1] == color:
                segs[-1] = (segs[-1][0] + ch, color)
            else:
                segs.append((ch, color))
        merged.append(segs)
    return merged or [[]]  # 空行保留为空行，不要吞掉


def annotate_answer(text: str, verdicts: list[dict[str, Any]]) -> Line:
    """按 verdict 的 position/raw/found 给数字上色（红=幻觉，绿=可溯源）。

    返回未折行的一整条 Line，由最终渲染统一折行。
    """
    segments: Line = []
    cursor = 0
    for v in sorted(verdicts, key=lambda item: item["position"]):
        start, end = v["position"], v["position"] + len(v["raw"])
        if text[start:end] != v["raw"]:
            continue
        if start > cursor:
            segments.append((text[cursor:start], "text"))
        segments.append((v["raw"], "green" if v["found"] else "red"))
        cursor = end
    if cursor < len(text):
        segments.append((text[cursor:], "text"))
    return segments


def build_scenes(f: dict[str, Any]) -> list[Scene]:
    ds = f["datasource"]
    schema_line = "  schema: " + " | ".join(
        f"{c['name']}: {c['dtype']}" for c in ds["columns"]
    )
    scenes: list[Scene] = [
        [
            [("$ ", "green"), ("python scripts/demo_anti_hallucination.py", "white")],
            line("DataSage 反幻觉端到端演示 · 全程离线（mock provider，无需 API Key）", "dim"),
            [],
        ],
        [
            [("[1/5] ", "blue"), ("上传数据 ", "text"), (ds["name"], "amber"),
             (f"（{ds['row_count']} 行 × {len(ds['columns'])} 列）", "text")],
            line(schema_line, "dim"),
            [],
        ],
        [
            [("[2/5] ", "blue"), ("自然语言提问", "text")],
            line("  " + f["question"], "white"),
            line("  LLM 生成 pandas 代码 -> 受限沙箱执行 -> 三层结果验证（全部通过）", "dim"),
            [],
        ],
    ]

    table_scene: Scene = [
        [("[3/5] ", "blue"),
         ("沙箱真实执行结果（数字回溯唯一的证据来源，模型拿不到全量数据）", "text")],
        [("        " + "          ".join(str(c) for c in f["result"]["columns"]), "blue")],
    ]
    for row in f["result"]["data"]:
        table_scene.append([
            ("        " + str(row[0]) + "              ", "text"),
            (str(row[1]), "amber"),
        ])
    table_scene.append([])
    scenes.append(table_scene)

    raws = "、".join(f["hallucination_raws"])
    detect: Scene = [
        [("[4/5] ", "blue"),
         ("LLM 写出第一版答案（演示模式：模型在数字上'自由发挥'了一次）", "text")],
    ]
    detect.append(
        [("      ", "dim")]
        + annotate_answer(f["initial"], f["initial_verdicts"])
    )
    detect.append([
        ("      [!] ", "red"), ("数字回溯拦截：", "red"), (raws, "red"),
        (" 在真实结果中找不到任何证据（单元格 / 行列数 / 求和 / 均值 / "
         "百分比与倍数候选全部比对过，均不匹配），判定为幻觉，自动触发重生成。", "red"),
    ])
    detect.append([])
    scenes.append(detect)

    ok_count = sum(1 for v in f["verdicts"] if v["found"])
    repair: Scene = [
        [("[5/5] ", "blue"),
         (f"系统自动把幻觉证据喂回 LLM 重新生成（第 {f['rounds']} 轮）", "text")],
        line("      修复 prompt：以下数字无法溯源……只能引用真实数据，"
             "不要编造任何数据里没有的数字。", "dim"),
        [],
        [("[OK] ", "green"), ("最终答案（每个数字都能回溯到沙箱结果）", "green")],
    ]
    repair.append(
        [("      ", "dim")] + annotate_answer(f["answer"], f["verdicts"])
    )
    repair.append([
        ("      [OK] ", "green"),
        (f"{ok_count}/{len(f['verdicts'])} 个数字全部找到证据 · verified=True · "
         f"hallucination_count=0", "green"),
    ])
    repair.append([])
    repair.append(line(
        "浏览器中：红色高亮=幻觉数字（'答案生成'步骤保留被丢弃的初稿），"
        "绿色下划线=可溯源数字，点击可看行列证据。", "dim"))
    scenes.append(repair)
    return scenes


def print_transcript(scenes: list[Scene]) -> str:
    plain_parts: list[str] = []
    for ln in [ln for scene in scenes for ln in scene]:
        for wrapped in wrap_segments(ln):
            plain_parts.append("".join(text for text, _ in wrapped))
            sys.stdout.write(
                "".join(f"{ANSI[c]}{t}{RESET}" for t, c in wrapped) + "\n"
            )
    return "\n".join(plain_parts)


# ----------------------- GIF 渲染（Pillow，可选依赖） -----------------------

def _load_fonts() -> tuple[Any, Any]:
    from PIL import ImageFont

    candidates = [
        ("C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/msyhbd.ttc"),
        ("C:/Windows/Fonts/simhei.ttf", "C:/Windows/Fonts/simhei.ttf"),
        ("/System/Library/Fonts/PingFang.ttc", "/System/Library/Fonts/PingFang.ttc"),
        ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
         "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    ]
    regular_path = bold_path = None
    for reg, bold in candidates:
        if Path(reg).exists():
            regular_path, bold_path = reg, bold
            break
    if regular_path is None:
        return ImageFont.load_default(), ImageFont.load_default()
    regular = ImageFont.truetype(regular_path, 18)
    bold = (
        ImageFont.truetype(bold_path, 18)
        if bold_path and Path(bold_path).exists()
        else regular
    )
    return regular, bold


def render_gif(scenes: list[Scene], path: Path) -> None:
    """把分镜逐行动画录制为循环 GIF（终端风格深色窗口）。"""
    from PIL import Image, ImageDraw

    font, bold = _load_fonts()

    all_lines: list[Line] = [
        wrapped
        for ln in [ln for scene in scenes for ln in scene]
        for wrapped in wrap_segments(ln)
    ]

    # 镜头表：每个 scene 内每 2 行一帧（100ms），scene 末尾停顿（650ms）
    shots: list[tuple[int, int]] = []
    shown = 0
    for scene in scenes:
        scene_lines = sum(len(wrap_segments(ln)) for ln in scene)
        target = shown + scene_lines
        while shown < target:
            shown = min(target, shown + 2)
            shots.append((shown, 90))
        shots.append((shown, 650))

    width, pad, title_h, line_h = 1000, 26, 42, 27
    height = title_h + 16 + len(all_lines) * line_h + 22

    def draw_frame(n_lines: int) -> Any:
        img = Image.new("RGB", (width, height), (13, 17, 23))
        d = ImageDraw.Draw(img)
        d.rectangle([0, 0, width, title_h], fill=(22, 27, 34))
        for i, color in enumerate((((255, 95, 86), (255, 189, 46), (39, 201, 63)))):
            d.ellipse([18 + i * 22, 14, 30 + i * 22, 26], fill=color)
        title = "datasage - anti-hallucination demo (mock, offline)"
        d.text((width // 2 - int(d.textlength(title, font=bold)) // 2, 12),
               title, font=bold, fill=(139, 148, 158))

        y = title_h + 14
        for ln in all_lines[:n_lines]:
            x = pad
            for text, color in ln:
                d.text((x, y), text, font=font, fill=RGB[color])
                x += int(d.textlength(text, font=font))
            y += line_h
        return img

    frames = [draw_frame(n).convert("P", palette=Image.ADAPTIVE, colors=128)
              for n, _ in shots]
    durations = [ms for _, ms in shots]
    path.parent.mkdir(parents=True, exist_ok=True)
    frames[0].save(
        path,
        save_all=True,
        append_images=frames[1:],
        duration=durations,
        loop=0,
        optimize=False,
        disposal=2,
    )


# ----------------------- 入口 -----------------------

def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except Exception:
        pass

    make_gif = "--no-gif" not in sys.argv
    facts = run_demo()
    scenes = build_scenes(facts)
    print()
    transcript = print_transcript(scenes)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    TRANSCRIPT_PATH.write_text(transcript + "\n", encoding="utf-8")

    print()
    if make_gif:
        try:
            render_gif(scenes, GIF_PATH)
        except ModuleNotFoundError:
            print("未安装 Pillow，跳过 GIF 录制。安装后重跑即可：")
            print("  pip install -r requirements-dev.txt")
        else:
            size_kb = GIF_PATH.stat().st_size / 1024
            print(f"{ANSI['green']}演示动图已生成：{GIF_PATH}（{size_kb:.0f} KB）{RESET}")
    print(f"{ANSI['dim']}文字版记录：{TRANSCRIPT_PATH}{RESET}")
    print(
        f"{ANSI['green']}反幻觉演示成功：初稿 {len(facts['hallucination_raws'])} 处幻觉 -> "
        f"自动重生成 {facts['rounds']} 轮 -> 最终答案 "
        f"{sum(1 for v in facts['verdicts'] if v['found'])}/{len(facts['verdicts'])} "
        f"个数字全部可溯源。{RESET}"
    )


if __name__ == "__main__":
    main()

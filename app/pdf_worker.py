"""Run one BabelDOC PDF translation in a child process and stream JSON progress lines."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import multiprocessing as mp
import shutil
import sys
from pathlib import Path


def emit(kind: str, **payload) -> None:
    sys.stdout.write(json.dumps({"type": kind, **payload}, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def quiet_logs() -> None:
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    for name in list(logging.root.manager.loggerDict):
        if name.startswith(("httpx", "openai", "pdfminer", "peewee", "httpcore")):
            logging.getLogger(name).setLevel(logging.ERROR)


async def run(args: argparse.Namespace) -> int:
    import babeldoc.format.pdf.high_level as high_level
    from babeldoc.docvision.doclayout import DocLayoutModel
    from babeldoc.format.pdf.translation_config import TranslationConfig, WatermarkOutputMode
    from babeldoc.glossary import Glossary
    from babeldoc.translator.translator import OpenAITranslator, set_translate_rate_limiter

    high_level.init()
    translator = OpenAITranslator(
        lang_in=args.lang_in,
        lang_out=args.lang_out,
        model=args.model,
        base_url=args.base_url,
        api_key=args.api_key,
        ignore_cache=False,
    )
    set_translate_rate_limiter(args.qps)
    glossaries = []
    if args.glossary and Path(args.glossary).is_file():
        glossaries.append(Glossary.from_csv(Path(args.glossary), args.lang_out))
    emit("stage", stage="加载版面分析模型", progress=2)
    layout_model = DocLayoutModel.load_onnx()
    output_dir = Path(args.output_dir)
    work_dir = Path(args.work_dir)
    config = TranslationConfig(
        translator=translator,
        input_file=args.input,
        lang_in=args.lang_in,
        lang_out=args.lang_out,
        doc_layout_model=layout_model,
        pages=args.pages or None,
        output_dir=output_dir,
        working_dir=work_dir,
        qps=args.qps,
        no_dual=False,
        no_mono=False,
        use_rich_pbar=False,
        report_interval=1.0,
        watermark_output_mode=WatermarkOutputMode.NoWatermark,
        glossaries=glossaries,
        auto_extract_glossary=not args.no_term_extraction,
        save_auto_extracted_glossary=False,
        custom_system_prompt=args.system_prompt or None,
        skip_scanned_detection=False,
        auto_enable_ocr_workaround=True,
    )
    getattr(layout_model, "init_font_mapper", lambda _config: None)(config)
    result = None
    async for event in high_level.async_translate(config):
        kind = event.get("type")
        if kind in ("progress_start", "progress_update", "progress_end"):
            emit(
                "progress",
                stage=event.get("stage"),
                current=event.get("stage_current"),
                total=event.get("stage_total"),
                progress=event.get("overall_progress"),
            )
        elif kind == "error":
            emit("error", error=str(event.get("error")))
            return 2
        elif kind == "finish":
            result = event.get("translate_result")
            break
    if result is None:
        emit("error", error="BabelDOC 未返回翻译结果")
        return 2
    mono = result.no_watermark_mono_pdf_path or result.mono_pdf_path
    dual = result.no_watermark_dual_pdf_path or result.dual_pdf_path
    outputs = {}
    if mono and Path(mono).is_file():
        shutil.move(str(mono), output_dir / "translated.pdf")
        outputs["translated"] = "translated.pdf"
    if dual and Path(dual).is_file():
        shutil.move(str(dual), output_dir / "dual.pdf")
        outputs["dual"] = "dual.pdf"
    emit(
        "finish",
        outputs=outputs,
        seconds=round(float(getattr(result, "total_seconds", 0) or 0), 1),
        usage={
            "prompt_tokens": translator.prompt_token_count.value,
            "completion_tokens": translator.completion_token_count.value,
            "total_tokens": translator.token_count.value,
        },
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--work-dir", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--api-key", default="magpie")
    parser.add_argument("--lang-in", default="en")
    parser.add_argument("--lang-out", default="zh-CN")
    parser.add_argument("--qps", type=int, default=8)
    parser.add_argument("--pages", default="")
    parser.add_argument("--glossary", default="")
    parser.add_argument("--system-prompt", default="")
    parser.add_argument("--no-term-extraction", action="store_true")
    args = parser.parse_args()
    quiet_logs()
    try:
        return asyncio.run(run(args))
    except Exception as exc:
        emit("error", error=f"{exc.__class__.__name__}: {exc}")
        return 2


if __name__ == "__main__":
    mp.set_start_method("spawn", force=True)
    sys.exit(main())

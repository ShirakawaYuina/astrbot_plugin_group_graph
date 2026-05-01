from __future__ import annotations

import json
import re
from typing import Any

from astrbot.api import logger


class LLMResponseError(RuntimeError):
    """用于标识可预期的 LLM 响应异常，避免把原始长文本直接打进日志。"""


def _collapse_whitespace(text: str) -> str:
    """将多余空白折叠成单个空格，方便在日志中输出紧凑摘要。"""

    return re.sub(r"\s+", " ", text).strip()


def _trim_message(text: str, max_length: int = 180) -> str:
    """截断长文本，避免 HTML 错页或超长响应污染插件日志。"""

    collapsed_text = _collapse_whitespace(text)
    if len(collapsed_text) <= max_length:
        return collapsed_text
    return f"{collapsed_text[: max_length - 3]}..."


def _looks_like_html(text: str) -> bool:
    """判断返回内容是否明显是 HTML 页面，而不是模型 JSON 输出。"""

    lowered_text = text.lower()
    html_markers = ("<!doctype html", "<html", "</html>", "<head", "<body")
    return any(marker in lowered_text for marker in html_markers)


def summarize_llm_payload(text: str) -> str:
    """把原始响应压缩成适合写日志的短摘要。"""

    collapsed_text = _collapse_whitespace(text)
    if not collapsed_text:
        return "空响应"

    if _looks_like_html(collapsed_text):
        status_code_match = re.search(r"\b([45]\d{2})\b", collapsed_text)
        title_match = re.search(
            r"<title>(.*?)</title>", text, re.IGNORECASE | re.DOTALL
        )
        timeout_match = re.search(
            r"(Gateway Time-out|Gateway Timeout|Bad Gateway|Service Unavailable|源站服务器连接超时)",
            collapsed_text,
            re.IGNORECASE,
        )
        summary_parts = ["上游返回HTML错误页"]
        if status_code_match:
            summary_parts.append(f"状态码={status_code_match.group(1)}")
        if title_match:
            summary_parts.append(
                f"标题={_trim_message(title_match.group(1), max_length=40)}"
            )
        if timeout_match:
            summary_parts.append(f"摘要={timeout_match.group(1)}")
        return "，".join(summary_parts)

    return _trim_message(collapsed_text)


def summarize_llm_exception(exc: Exception) -> str:
    """对异常文本做统一压缩，避免把整页 HTML 或超长 SDK 对象输出到日志。"""

    return summarize_llm_payload(str(exc))


def _extract_text_from_response(response: Any) -> str:
    """从 provider 响应中提取 completion_text，并把空内容转换成可读错误。"""

    completion_text = getattr(response, "completion_text", None)
    if not isinstance(completion_text, str) or not completion_text.strip():
        response_id = getattr(response, "id", None) or getattr(
            response, "response_id", None
        )
        finish_reason = ""
        choices = getattr(response, "choices", None)
        if choices and isinstance(choices, list):
            first_choice = choices[0]
            finish_reason = getattr(first_choice, "finish_reason", "") or ""

        message_parts = ["LLM未返回可用文本"]
        if response_id:
            message_parts.append(f"response_id={response_id}")
        if finish_reason:
            message_parts.append(f"finish_reason={finish_reason}")
        raise LLMResponseError("，".join(message_parts))

    return completion_text.strip()


def _unwrap_code_block(text: str) -> str:
    """兼容模型把 JSON 包在 Markdown 代码块中的情况。"""

    if "```json" in text:
        return text.split("```json", 1)[1].split("```", 1)[0].strip()
    if "```" in text:
        return text.split("```", 1)[1].split("```", 1)[0].strip()
    return text


def parse_json_text_from_response(response: Any) -> Any:
    """把 provider 响应解析成 JSON；遇到异常页或非法 JSON 时抛出紧凑异常。"""

    result_text = _unwrap_code_block(_extract_text_from_response(response))
    if _looks_like_html(result_text):
        raise LLMResponseError(summarize_llm_payload(result_text))

    try:
        return json.loads(result_text)
    except json.JSONDecodeError as exc:
        raise LLMResponseError(
            f"LLM返回内容不是合法JSON: {summarize_llm_payload(result_text)}"
        ) from exc


def should_retry_llm_error(exc: Exception) -> bool:
    """只对明显的瞬时故障做一次轻量重试，避免放大慢请求。"""

    lowered_message = summarize_llm_exception(exc).lower()
    retry_keywords = (
        "no usable output",
        "未返回可用文本",
        "timeout",
        "time-out",
        "timed out",
        "gateway",
        "状态码=502",
        "状态码=503",
        "状态码=504",
        "service unavailable",
        "bad gateway",
        "temporarily unavailable",
        "connection reset",
    )
    return any(keyword in lowered_message for keyword in retry_keywords)


async def request_json_from_provider(
    *,
    provider: Any,
    prompt: str,
    task_name: str,
    max_attempts: int = 2,
) -> Any:
    """统一处理 LLM JSON 请求、错误压缩和一次瞬时故障重试。"""

    last_error: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            response = await provider.text_chat(
                contexts=[{"role": "user", "content": prompt}]
            )
            return parse_json_text_from_response(response)
        except Exception as exc:  # noqa: BLE001 - 这里需要收敛各种 provider/SDK 异常
            normalized_error = exc
            if not isinstance(exc, LLMResponseError):
                normalized_error = LLMResponseError(summarize_llm_exception(exc))
            last_error = normalized_error

            if attempt < max_attempts and should_retry_llm_error(normalized_error):
                logger.warning(
                    f"{task_name}第{attempt}次调用失败，准备重试: "
                    f"{summarize_llm_exception(normalized_error)}"
                )
                continue
            break

    raise last_error or LLMResponseError("LLM请求失败")

"""Обёртка над Claude API: вызовы, цикл работы с инструментами, учёт расходов."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import anthropic

from .config import Config
from .db import DB

if TYPE_CHECKING:
    from .tools import ToolBox

log = logging.getLogger(__name__)

# Примерные цены за 1 млн токенов: (вход, выход, чтение из кеша). Запись в кеш ≈ 1.25 × вход.
PRICES: dict[str, tuple[float, float, float]] = {
    "claude-fable-5-1": (10.0, 50.0, 0.25),
    "claude-opus-5-5": (4.0, 20.0, 0.20),
    "claude-opus-5": (5.0, 25.0, 0.50),
    "claude-opus-4-8": (5.0, 25.0, 0.50),
    "claude-sonnet-5-5": (2.0, 10.0, 0.20),
    "claude-sonnet-5": (2.0, 10.0, 0.20),
    "claude-sonnet-4-6": (3.0, 15.0, 0.30),
    "claude-haiku-4-5": (1.0, 5.0, 0.10),
}

# Модели, для которых включаем серверный запасной вариант при отказе (fallbacks="default")
FALLBACK_MODELS = {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class LLMError(RuntimeError):
    """Понятная пользователю ошибка обращения к Claude."""


def price_for(model: str) -> tuple[float, float, float]:
    return PRICES.get(model, (4.0, 20.0, 0.4))


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    pin, pout, _ = price_for(model)
    return (input_tokens * pin + output_tokens * pout) / 1_000_000


def usage_cost(model: str, usage) -> float:
    pin, pout, pread = price_for(model)
    inp = getattr(usage, "input_tokens", 0) or 0
    out = getattr(usage, "output_tokens", 0) or 0
    cr = getattr(usage, "cache_read_input_tokens", 0) or 0
    cw = getattr(usage, "cache_creation_input_tokens", 0) or 0
    return (inp * pin + out * pout + cr * pread + cw * pin * 1.25) / 1_000_000


def response_text(resp) -> str:
    return "\n".join(b.text for b in resp.content if b.type == "text").strip()


class LLM:
    def __init__(self, cfg: Config, db: DB):
        self.cfg = cfg
        self.db = db
        # ключ берётся из переменной окружения ANTHROPIC_API_KEY (файл .env)
        self.client = anthropic.AsyncAnthropic(max_retries=4)

    # ------------------------------------------------------------------ низкоуровневый вызов
    def _model_params(self, model: str, effort: str | None, output_format: dict | None) -> dict:
        params: dict[str, Any] = {}
        output_config: dict[str, Any] = {}
        if not model.startswith("claude-haiku"):
            params["thinking"] = {"type": "adaptive"}
            if effort:
                output_config["effort"] = effort
        if output_format:
            output_config["format"] = output_format
        if output_config:
            params["output_config"] = output_config
        if self.cfg.fallbacks and model in FALLBACK_MODELS:
            params["betas"] = [FALLBACK_BETA]
            params["fallbacks"] = "default"
        return params

    async def call(
        self,
        *,
        purpose: str,
        system: str | list[dict],
        messages: list[dict],
        model: str | None = None,
        tools: list[dict] | None = None,
        tool_choice: dict | None = None,
        effort: str | None = None,
        max_tokens: int = 16000,
        output_format: dict | None = None,
        stream: bool = False,
    ):
        model = model or self.cfg.model
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": messages,
            **self._model_params(model, effort, output_format),
        }
        if tools:
            kwargs["tools"] = tools
            # авто-кеширование хвоста переписки: шаги цикла с инструментами становятся дешевле
            kwargs["cache_control"] = {"type": "ephemeral"}
        if tool_choice:
            kwargs["tool_choice"] = tool_choice
        try:
            if stream:
                async with self.client.beta.messages.stream(**kwargs) as s:
                    resp = await s.get_final_message()
            else:
                resp = await self.client.beta.messages.create(**kwargs)
        except anthropic.AuthenticationError as e:
            raise LLMError("Claude: неверный ключ. Проверь ANTHROPIC_API_KEY в файле .env") from e
        except anthropic.PermissionDeniedError as e:
            raise LLMError(f"Claude: нет доступа ({e.message})") from e
        except anthropic.NotFoundError as e:
            raise LLMError(f"Claude: модель «{model}» не найдена — проверь claude.model в config.yaml") from e
        except anthropic.RateLimitError as e:
            raise LLMError("Claude: превышен лимит запросов, попробуй через минуту") from e
        except anthropic.BadRequestError as e:
            raise LLMError(f"Claude: ошибка запроса — {e.message}") from e
        except anthropic.APIStatusError as e:
            raise LLMError(f"Claude: ошибка сервера {e.status_code}, попробуй позже") from e
        except anthropic.APIConnectionError as e:
            raise LLMError("Claude: нет связи с сервером — проверь интернет") from e
        except TypeError as e:
            if "authentication" in str(e).lower():
                raise LLMError("Claude: не задан ключ. Впиши ANTHROPIC_API_KEY в файл .env") from e
            raise

        try:
            served_by = getattr(resp, "model", None) or model
            self.db.add_usage(served_by, purpose, resp.usage, usage_cost(served_by, resp.usage))
        except Exception:  # учёт расходов не должен ломать работу
            log.exception("Не удалось записать расход токенов")
        return resp

    # ------------------------------------------------------------------ простые запросы
    async def ask_text(self, *, purpose: str, system: str, prompt: str, effort: str | None = None,
                       model: str | None = None, max_tokens: int = 32000) -> str:
        resp = await self.call(
            purpose=purpose, system=system, model=model, effort=effort, max_tokens=max_tokens, stream=True,
            messages=[{"role": "user", "content": prompt}],
        )
        if resp.stop_reason == "refusal":
            raise LLMError("Модель отказалась отвечать на этот запрос")
        return response_text(resp)

    async def ask_json(self, *, purpose: str, system: str, prompt: str, schema: dict,
                       effort: str | None = None, model: str | None = None, max_tokens: int = 32000) -> dict:
        resp = await self.call(
            purpose=purpose, system=system, model=model, effort=effort, max_tokens=max_tokens, stream=True,
            output_format={"type": "json_schema", "schema": schema},
            messages=[{"role": "user", "content": prompt}],
        )
        if resp.stop_reason == "refusal":
            raise LLMError("Модель отказалась отвечать на этот запрос")
        if resp.stop_reason == "max_tokens":
            raise LLMError("Ответ не поместился в лимит токенов — уменьши learn_chunk_chars в config.yaml")
        return json.loads(response_text(resp))

    # ------------------------------------------------------------------ агент с инструментами
    async def run_agent(
        self,
        *,
        purpose: str,
        system: list[dict],
        messages: list[dict],
        toolbox: ToolBox,
        effort: str | None,
        model: str | None = None,
        on_tool: Callable[[str, dict], None] | None = None,
    ) -> str:
        """Цикл: Claude думает → вызывает инструменты → получает результаты → … → финальный ответ.

        История внутри одного запуска только дописывается (ничего не редактируем) —
        так работают кеш и сохранённые размышления модели.
        """
        msgs = list(messages)
        max_steps = self.cfg.max_tool_steps
        for step in range(max_steps + 1):
            last_step = step == max_steps
            resp = await self.call(
                purpose=purpose, system=system, messages=msgs, model=model, effort=effort,
                tools=toolbox.schemas, tool_choice={"type": "none"} if last_step else None,
            )
            if resp.stop_reason == "refusal":
                return "⚠️ Модель отказалась отвечать на этот запрос. Попробуй переформулировать."
            msgs.append({"role": "assistant", "content": resp.content})

            if resp.stop_reason == "pause_turn":
                continue

            tool_uses = [b for b in resp.content if b.type == "tool_use"]
            # при обрыве по лимиту токенов вызов инструмента может быть неполным — не выполняем его
            if resp.stop_reason == "tool_use" and tool_uses and not last_step:
                for b in tool_uses:
                    log.info("🔧 %s %s", b.name, json.dumps(b.input, ensure_ascii=False)[:300])
                    if on_tool:
                        on_tool(b.name, b.input)
                results = await asyncio.gather(*(toolbox.execute(b.name, b.input, b.id) for b in tool_uses))
                content: list[dict] = list(results)
                if step + 1 == max_steps:
                    content.append({
                        "type": "text",
                        "text": "Лимит шагов исчерпан. Дай итоговый ответ по уже собранной информации, "
                                "без новых вызовов инструментов.",
                    })
                msgs.append({"role": "user", "content": content})
                continue

            text = response_text(resp)
            if resp.stop_reason == "max_tokens":
                if not text:
                    return "⚠️ Ответ не поместился в лимит длины. Попробуй разбить вопрос на части."
                text += "\n\n…(ответ обрезан по длине)"
            return text or "(пустой ответ)"
        return "⚠️ Не удалось завершить задачу за отведённое число шагов."

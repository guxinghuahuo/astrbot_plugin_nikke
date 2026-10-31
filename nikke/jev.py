"""TypeSafe Choice precheck; only a confident normal result may bypass the LLM."""

import json
import math
from dataclasses import dataclass

import aiohttp

from .config import Settings
from .prompts import JEV_CRITERIA_JSON, JEV_INSTRUCTIONS

CRITERIA = json.loads(JEV_CRITERIA_JSON)
INSTRUCTIONS = JEV_INSTRUCTIONS

ENDPOINT = "https://api.typesafe.ai/v1/systemone"
POLICY_VERSION = "nikke-jev-v1"
CACHE_SECONDS = 86400
MAX_RESPONSE_BYTES = 65536


class JevError(Exception):
    """Safe diagnostic without credentials or response content."""

    def __init__(self, message: str, *, status: int | None = None):
        super().__init__(message)
        self.status = status


def probability(value) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and 0 <= value <= 1
    )


@dataclass(frozen=True)
class JevResult:
    choice: str
    confidence: float
    model: str

    def __post_init__(self):
        if self.choice not in CRITERIA or not probability(self.confidence):
            raise JevError("Jev 分类或置信度无效")
        if not isinstance(self.model, str) or not self.model.strip() or len(self.model) > 200:
            raise JevError("Jev 模型标识无效")


def parse_result(data) -> JevResult:
    try:
        answer = data["answers"]["dynamic_type"]
        result = JevResult(answer["choice"], answer["confidence"], data["model"])
        distribution = answer["probabilities"]
        if (
            answer["type"] != "choice"
            or not isinstance(distribution, dict)
            or set(distribution) != set(CRITERIA)
        ):
            raise JevError("Jev 返回的分类结构无效")
        if not all(probability(p) for p in distribution.values()) or not math.isclose(
            sum(distribution.values()), 1, abs_tol=0.001
        ):
            raise JevError("Jev 返回的概率分布无效")
        if distribution[result.choice] < max(distribution.values()):
            raise JevError("Jev 返回的分类与概率分布不一致")
        return result
    except (TypeError, KeyError, ValueError) as exc:
        raise JevError("Jev 返回格式无效") from exc


async def evaluate(settings: Settings, state: dict) -> JevResult:
    body = {
        "model": settings.jev_model,
        "state": state,
        "questions": {
            "dynamic_type": {
                "type": "choice",
                "instructions": settings.jev_instructions,
                "criteria": json.loads(settings.jev_criteria),
            }
        },
    }
    # Do not forward credentials through redirects, proxy environment or cookies.
    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=settings.jev_timeout),
        cookie_jar=aiohttp.DummyCookieJar(),
        trust_env=False,
    ) as session:
        async with session.post(
            ENDPOINT,
            json=body,
            headers={"Authorization": f"Bearer {settings.jev_token}"},
            allow_redirects=False,
        ) as response:
            if response.status != 200:
                raise JevError(f"Jev HTTP {response.status}", status=response.status)
            data = bytearray()
            async for chunk in response.content.iter_chunked(8192):
                data.extend(chunk)
                if len(data) > MAX_RESPONSE_BYTES:
                    raise JevError("Jev 响应超过大小上限")
    try:
        return parse_result(json.loads(data))
    except (ValueError, UnicodeError) as exc:
        raise JevError("Jev 响应不是有效 JSON") from exc

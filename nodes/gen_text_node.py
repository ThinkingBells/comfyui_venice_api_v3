from __future__ import annotations

import json
import logging
from typing import Any, Dict

import requests
from comfy_api.latest import io

from ..e2ee import decrypt_chunk, is_encrypted_chunk, setup_e2ee_request
from ..globals import API_ENDPOINTS, REQUEST_TIMEOUT, VENICEAI_BASE_URL
from ..nodes.catalog_utils import text_model_choices
from ..nodes.utils import encode_tensor_for_vision
from ..venice_config import config as venice_config

logger = logging.getLogger(__name__)


class GenerateText(io.ComfyNode):
    @classmethod
    def define_schema(cls) -> io.Schema:
        models = list(text_model_choices()) or ["llama-3.3-70b"]
        default_model = "llama-3.3-70b" if "llama-3.3-70b" in models else models[0]

        return io.Schema(
            node_id="GenerateText_VENICE",
            display_name="Generate Text (Venice)",
            category="venice.ai",
            inputs=[
                io.Combo.Input(
                    "model",
                    options=models,
                    default=default_model,
                    tooltip="The text model to use for generation.",
                ),
                io.String.Input(
                    "system_prompt",
                    default="",
                    multiline=True,
                    tooltip="Optional system prompt to guide the model's behavior.",
                ),
                io.String.Input(
                    "prompt",
                    default="",
                    multiline=True,
                    tooltip="The user message / prompt to send to the model.",
                ),
                io.Float.Input(
                    "temperature",
                    default=0.5,
                    min=0.0,
                    max=2.0,
                    step=0.05,
                    tooltip="Sampling temperature. Higher = more creative, lower = more deterministic.",
                ),
                io.Float.Input(
                    "top_p",
                    default=0.9,
                    min=0.0,
                    max=1.0,
                    step=0.01,
                    tooltip="Nucleus sampling probability.",
                ),
                io.Int.Input(
                    "max_completion_tokens",
                    default=3000,
                    min=1,
                    max=131072,
                    tooltip=(
                        "Maximum tokens to generate (includes reasoning tokens). "
                        "Keep low for reasoning models to avoid long waits."
                    ),
                ),
                io.Combo.Input(
                    "reasoning_effort",
                    options=["none", "low", "medium", "high"],
                    default="none",
                    tooltip=(
                        "Reasoning effort for models that support it (e.g. GLM, QwQ). "
                        "'none' disables reasoning and skips this parameter entirely."
                    ),
                ),
                io.Int.Input(
                    "seed",
                    default=1,
                    min=1,
                    tooltip="Seed for reproducibility (must be >= 1). Venice rejects seed = 0.",
                ),
                io.Boolean.Input(
                    "strip_thinking",
                    default=False,
                    tooltip=(
                        "Strip thinking/reasoning blocks from the response "
                        "(venice_parameters.strip_thinking_response). "
                        "Use with reasoning models to get clean output."
                    ),
                ),
                io.Boolean.Input(
                    "include_venice_system_prompt",
                    default=True,
                    tooltip="Allow Venice to inject its own system prompt alongside yours.",
                ),
                io.Boolean.Input(
                    "enable_vision",
                    default=False,
                    tooltip=(
                        "Enable vision input. Requires an image connected to vision_image "
                        "and a vision-capable model."
                    ),
                ),
                io.Boolean.Input(
                    "use_e2ee",
                    default=False,
                    tooltip=(
                        "Enable End-to-End Encryption (E2EE). "
                        "Required for models whose names start with 'e2ee-'. "
                        "Also auto-enabled when an e2ee- model is selected."
                    ),
                ),
                io.Image.Input(
                    "vision_image",
                    optional=True,
                    tooltip=(
                        "Optional image for vision-capable models. "
                        "Enable 'enable_vision' to send it."
                    ),
                ),
            ],
            outputs=[io.String.Output(id="response", display_name="response")],
        )

    @classmethod
    def execute(
        cls,
        model: str,
        system_prompt: str,
        prompt: str,
        temperature: float,
        top_p: float,
        max_completion_tokens: int,
        reasoning_effort: str,
        seed: int,
        strip_thinking: bool,
        include_venice_system_prompt: bool,
        enable_vision: bool,
        use_e2ee: bool,
        vision_image=None,
    ) -> io.NodeOutput:
        api_key = venice_config.apikey.strip()
        if not api_key:
            return io.NodeOutput("[Error] VeniceAI API key is missing. Set it in the VeniceAI settings.")

        url = VENICEAI_BASE_URL + API_ENDPOINTS["text_generate"]

        user_content: list = []
        if enable_vision and vision_image is not None:
            tensor = vision_image[0] if isinstance(vision_image, (list, tuple)) else vision_image
            if tensor is not None:
                encoded = encode_tensor_for_vision(tensor)
                user_content.extend([
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": encoded}},
                ])
        if not user_content:
            user_content.append({"type": "text", "text": prompt})

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]

        seed_value = max(1, int(seed))

        payload: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "top_p": top_p,
            "max_completion_tokens": max_completion_tokens,
            "seed": seed_value,
            "venice_parameters": {
                "strip_thinking_response": strip_thinking,
                "include_venice_system_prompt": include_venice_system_prompt,
            },
        }
        if reasoning_effort != "none":
            payload["reasoning_effort"] = reasoning_effort

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        needs_e2ee = use_e2ee or model.startswith("e2ee-")
        client_sk = None

        if needs_e2ee:
            logger.info("E2EE: fetching attestation for model '%s'", model)
            try:
                enc_messages, e2ee_headers, client_sk = setup_e2ee_request(
                    VENICEAI_BASE_URL, model, api_key, messages, timeout=30
                )
            except Exception as exc:
                return io.NodeOutput(f"[E2EE Error] {exc}")
            payload["messages"] = enc_messages
            payload["stream"] = True
            headers.update(e2ee_headers)

        try:
            response = requests.post(
                url,
                json=payload,
                headers=headers,
                timeout=REQUEST_TIMEOUT,
                stream=needs_e2ee,
            )
        except requests.exceptions.Timeout:
            return io.NodeOutput(
                f"[Error] Request timed out after {REQUEST_TIMEOUT}s. "
                "Reasoning models can take several minutes; try reducing max_completion_tokens."
            )
        except requests.exceptions.RequestException as exc:
            return io.NodeOutput(f"[Error] API request failed: {exc}")

        if response.status_code != 200:
            return io.NodeOutput(f"[Error] HTTP {response.status_code}: {response.text[:500]}")

        if needs_e2ee:
            content = cls._consume_e2ee_stream(response, client_sk)
        else:
            try:
                json_response = response.json()
                message = json_response["choices"][0]["message"]
                content = message.get("content") or message.get("reasoning_content") or ""
            except (KeyError, IndexError, TypeError) as exc:
                return io.NodeOutput(f"[Error] Unexpected API response: {exc}")

        return io.NodeOutput(content)

    @classmethod
    def _consume_e2ee_stream(cls, response: requests.Response, client_sk) -> str:
        parts = []
        for line in response.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data: "):
                continue
            data_str = line[len("data: "):]
            if data_str.strip() == "[DONE]":
                break
            try:
                chunk = json.loads(data_str)
            except json.JSONDecodeError:
                continue
            choices = chunk.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta", {})
            raw = delta.get("content") or ""
            if raw:
                text = (decrypt_chunk(raw, client_sk) if is_encrypted_chunk(raw) else raw) or ""
                if text:
                    parts.append(text)
        return "".join(parts)

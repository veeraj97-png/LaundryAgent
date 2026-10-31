"""One bounded Nebius round trip; imports do not contact APIs or initialize storage."""
import argparse
from dataclasses import dataclass, field
import json
import logging
import os
from pathlib import Path
import re
from urllib.parse import urlsplit

from dotenv import dotenv_values
from openai import APIConnectionError, APIError, APITimeoutError, OpenAI

from shop import calculate_quote


DEFAULT_BASE_URL = "https://api.tokenfactory.nebius.com/v1/"
ENV_FILE = Path(__file__).with_name(".env")
# This exact ID is used in Nebius's function-calling documentation. It is
# a candidate, not a promise of account access, current availability or price.
DOCUMENTED_TOOL_MODELS = ("meta-llama/Meta-Llama-3.1-8B-Instruct-fast",)
USER_REQUEST = "What would it cost to wash and iron five garments?"
EXPECTED_ARGUMENTS = {"service_code": "wash_and_iron", "quantity": 5}
MAX_TOKENS = 256
MAX_MODELS_DISPLAYED = 20
QUOTE_TOOL = {
    "type": "function",
    "function": {
        "name": "calculate_quote",
        "description": "Calculate a laundry price in Indian rupees without creating a booking.",
        "parameters": {
            "type": "object",
            "properties": {
                "service_code": {"type": "string", "enum": ["wash_and_fold", "wash_and_iron"]},
                "quantity": {"type": "integer", "minimum": 1, "maximum": 1000},
            },
            "required": ["service_code", "quantity"],
            "additionalProperties": False,
        },
    },
}


class ConfigurationError(ValueError):
    pass


class IncompleteCheck(ValueError):
    pass


@dataclass(frozen=True)
class Config:
    api_key: str = field(repr=False)
    base_url: str
    model: str | None


def load_config(env_file=ENV_FILE, environ=None):
    """Read only this project's .env; exported variables take precedence."""
    environ = os.environ if environ is None else environ
    try:
        local = dotenv_values(env_file, interpolate=False) if env_file.is_file() else {}
    except (OSError, UnicodeError):
        raise ConfigurationError("Cannot read the project .env file. Check it locally.") from None
    def value(name, default=""):
        return (environ.get(name, local.get(name, default)) or "").strip()
    api_key = value("NEBIUS_API_KEY")
    if not api_key:
        raise ConfigurationError("NEBIUS_API_KEY is missing. Copy .env.example to .env and enter your key locally.")
    if any(character.isspace() for character in api_key):
        raise ConfigurationError("NEBIUS_API_KEY must be a single token. Correct it locally.")
    base_url = value("NEBIUS_BASE_URL", DEFAULT_BASE_URL)
    try:
        url = urlsplit(base_url)
        valid_url = (url.scheme == "https" and url.hostname is not None
                     and url.hostname.endswith(".nebius.com")
                     and url.username is None and url.password is None
                     and not url.query and not url.fragment and url.port in (None, 443)
                     and url.path.rstrip("/") == "/v1")
    except ValueError:
        valid_url = False
    if not valid_url:
        raise ConfigurationError("NEBIUS_BASE_URL must be a Nebius HTTPS /v1/ URL without embedded credentials or query parameters.")
    model = value("NEBIUS_MODEL") or None
    if model is not None and (len(model) > 200 or any(character.isspace() for character in model)):
        raise ConfigurationError("NEBIUS_MODEL must be one exact model ID copied locally from discovery.")
    return Config(api_key, base_url.rstrip("/") + "/", model)


def safe_text(text, api_key):
    """Never echo a key, even if a server includes it in returned text."""
    redacted = str(text).replace(api_key, "[REDACTED]") if api_key else str(text)
    return "".join(character for character in redacted if character.isprintable() or character == "\n")[:2000]


def error_message(error):
    """Classify failures without printing provider bodies, headers or raw errors."""
    if isinstance(error, APITimeoutError):
        return "CONNECTION TIMEOUT: the request exceeded its timeout; no automatic retry was made."
    if isinstance(error, APIConnectionError):
        return "CONNECTION ERROR: check network access, TLS and NEBIUS_BASE_URL locally."
    status = getattr(error, "status_code", None)
    if status == 401:
        return "AUTHENTICATION ERROR (401): check NEBIUS_API_KEY locally."
    if status in (403, 404):
        return "MODEL/PROJECT ACCESS ERROR (403/404): check access and the exact model ID in your Nebius account."
    if status == 429:
        return "RATE-LIMIT/QUOTA ERROR (429): check limits and billing; no automatic retry was made."
    if status in (400, 422):
        return "REQUEST/MODEL CAPABILITY ERROR (400/422): the selected model may not accept this tool-calling request."
    return "API ERROR: the check failed. Provider details were suppressed to protect configuration."


def discover_models(client):
    # Access .data only: do not auto-paginate into extra requests.
    page = client.models.list(extra_query={"verbose": "true"})
    models = []
    for model in page.data:
        data = model.model_dump()
        if isinstance(data.get("id"), str) and data["id"]:
            models.append(data)
    return sorted(models, key=lambda model: model["id"])


def show_model_choices(models, emit):
    ids = {model["id"] for model in models}
    emit("Documented tool-calling example IDs (not live-verified):")
    for model_id in DOCUMENTED_TOOL_MODELS:
        availability = "returned by discovery" if model_id in ids else "availability NOT confirmed"
        emit(f"  {model_id} — {availability}")
    if models:
        emit("Returned model IDs (listing alone does NOT prove tool support):")
        for model in models[:MAX_MODELS_DISPLAYED]:
            emit("  " + model["id"][:200])
        if len(models) > MAX_MODELS_DISPLAYED:
            emit("  Display limited to 20 IDs; use the Nebius console for the rest.")
    emit("Review access, tool support and pricing, then set NEBIUS_MODEL to your chosen exact ID in .env.")
    emit("No model is selected automatically. Run python3 nebius_check.py to test the selected model.")


def _unique_json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise IncompleteCheck("Tool arguments contain duplicate keys.")
        result[key] = value
    return result


def dispatch_tool_call(call):
    """Allow only the quote function, and only the stated demo request."""
    if not isinstance(call, dict) or call.get("type") != "function":
        raise IncompleteCheck("Expected one function tool call.")
    if not isinstance(call.get("id"), str) or not call["id"] or len(call["id"]) > 256:
        raise IncompleteCheck("The tool call has no valid call ID.")
    function = call.get("function")
    if not isinstance(function, dict) or function.get("name") != "calculate_quote":
        raise IncompleteCheck("Only calculate_quote is allowed.")
    raw_arguments = function.get("arguments")
    if not isinstance(raw_arguments, str) or len(raw_arguments) > 2048:
        raise IncompleteCheck("Tool arguments must be a bounded JSON string.")
    try:
        arguments = json.loads(raw_arguments, object_pairs_hook=_unique_json_object)
    except (json.JSONDecodeError, RecursionError):
        raise IncompleteCheck("Tool arguments are not valid JSON.") from None
    if not isinstance(arguments, dict) or set(arguments) != {"service_code", "quantity"}:
        raise IncompleteCheck("Tool arguments must contain exactly service_code and quantity.")
    if (arguments["service_code"] not in ("wash_and_fold", "wash_and_iron")
            or type(arguments["quantity"]) is not int or not 1 <= arguments["quantity"] <= 1000):
        raise IncompleteCheck("Tool arguments do not match the allowed services and integer quantity range.")
    if arguments != EXPECTED_ARGUMENTS:
        raise IncompleteCheck("The tool call does not match wash-and-iron for five garments.")
    return arguments, calculate_quote(**arguments)


def _message(response):
    if len(response.choices) != 1 or response.choices[0].finish_reason in ("length", "content_filter"):
        raise IncompleteCheck("Expected one complete, untruncated model response.")
    return response.choices[0].message


def run_tool_round_trip(client, model, emit=print):
    messages = [
        {"role": "system", "content": (
            "Use calculate_quote exactly once for the user's request: service_code wash_and_iron, quantity 5. "
            "After receiving the result, answer in one short plain-text sentence with the total as digits prefixed by Rs. "
            "Do not invent prices or request more tools."
        )},
        {"role": "user", "content": USER_REQUEST},
    ]
    response = client.chat.completions.create(
        model=model, messages=messages, tools=[QUOTE_TOOL],
        tool_choice={"type": "function", "function": {"name": "calculate_quote"}},
        max_tokens=MAX_TOKENS, n=1, store=False,
    )
    message = _message(response)
    if not message.tool_calls or len(message.tool_calls) != 1:
        raise IncompleteCheck("Expected exactly one tool call; missing or multiple calls are not a pass.")
    call = message.tool_calls[0].model_dump()
    arguments, result = dispatch_tool_call(call)
    emit("Tool: calculate_quote")
    emit("Arguments: " + json.dumps(arguments, sort_keys=True))
    emit("Actual Python result: " + json.dumps(result, sort_keys=True))
    messages.extend([
        {"role": "assistant", "tool_calls": [{
            "id": call["id"], "type": "function", "function": {
                "name": "calculate_quote", "arguments": json.dumps(arguments),
            },
        }]},
        {"role": "tool", "tool_call_id": call["id"], "name": "calculate_quote", "content": json.dumps(result)},
    ])
    # Exactly one follow-up request; the model is not allowed another tool turn.
    final_response = client.chat.completions.create(
        model=model, messages=messages, tools=[QUOTE_TOOL], tool_choice="none",
        max_tokens=MAX_TOKENS, n=1, store=False,
    )
    final = _message(final_response)
    if final.tool_calls or not isinstance(final.content, str) or not final.content.strip():
        raise IncompleteCheck("Expected a final text answer without further tool calls.")
    if len(final.content) > 2000:
        raise IncompleteCheck("Final answer exceeded the display limit.")
    total = result["total_price"]
    if not re.search(rf"(?:Rs\.?|INR|₹)\s*{total}(?:\.00)?(?!\d|\.\d)", final.content, re.IGNORECASE):
        raise IncompleteCheck("Final answer did not state the actual tool total in the requested currency format.")
    emit("Customer response: " + final.content.strip())
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description="Check Nebius with one read-only quote tool round trip.")
    parser.add_argument("--list-models", action="store_true", help="Discover models without generating a completion.")
    arguments = parser.parse_args(argv)
    # Suppress SDK/transport debug output, including when OPENAI_LOG is set.
    for logger in ("openai", "httpx", "httpx2", "httpcore", "httpcore2"):
        logging.getLogger(logger).setLevel(logging.CRITICAL)
    try:
        config = load_config()
    except ConfigurationError as error:
        print("LIVE TEST NOT RUN: " + str(error))
        show_model_choices([], print)
        return 2
    emit = lambda text: print(safe_text(text, config.api_key))
    try:
        with OpenAI(api_key=config.api_key, base_url=config.base_url, timeout=20.0, max_retries=0) as client:
            models = discover_models(client)
            emit("Model discovery completed (one GET /models request).")
            if arguments.list_models or config.model is None:
                show_model_choices(models, emit)
                emit("LIVE TOOL TEST NOT RUN: discovery does not verify a tool round trip.")
                return 0 if arguments.list_models else 2
            if config.model not in {model["id"] for model in models}:
                emit("MODEL ACCESS CHECK FAILED: configured ID was not returned by discovery. No completion requested.")
                return 1
            emit("Testing the configured model's tool support; model listing alone is insufficient.")
            run_tool_round_trip(client, config.model, emit)
        emit("LIVE TOOL ROUND TRIP PASS: one validated Python call and one final model answer.")
        return 0
    except IncompleteCheck as error:
        emit("CHECK FAILED/INCOMPLETE: " + str(error))
    except APIError as error:
        emit(error_message(error))
    except Exception:
        emit("CHECK FAILED: unexpected local or response error; raw details suppressed to protect configuration.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

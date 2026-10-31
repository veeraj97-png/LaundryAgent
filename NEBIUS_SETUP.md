# One read-only Nebius check

This step asks: “What would it cost to wash and iron five garments?” It
offers only `shop.calculate_quote`, validates the returned call, executes
that existing Python function, and sends its JSON result back for a short
answer. It does not open, initialize or modify `laundry.db`.

## Environment and local configuration

This guide describes the earlier one-off connectivity milestone; it remains
available alongside the current graph and Streamlit application. Use
[README.md](README.md) for a fresh checkout. Python 3.12 is the verified runtime.
The current requirements file pins five direct dependencies: `openai`,
`python-dotenv`, `langgraph`, `langgraph-checkpoint-sqlite`, and `streamlit`.
The one-off script itself uses the first two. Install only into a virtual
environment, never globally.

From a terminal:

```bash
cd LaundryAgent
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp -n .env.example .env
chmod 600 .env
nano .env
```

In your local editor, enter your own `NEBIUS_API_KEY`. Keep
`NEBIUS_BASE_URL=https://api.tokenfactory.nebius.com/v1/`. Leave
`NEBIUS_MODEL` empty initially. Do not paste the key into chat or commands
that could enter shell history. `.env` and `.venv` are gitignored.

The script reads only `.env` beside `nebius_check.py`; it does not search
parent directories or sibling projects. Exported environment variables
take precedence, including an explicitly empty variable. Values are not
expanded with `${...}`. Configuration is read only when the script runs.

## Discover, choose, then test

```bash
python nebius_check.py --list-models
```

This performs one authenticated model-list request and no inference.
Nebius's function-calling documentation uses
`meta-llama/Meta-Llama-3.1-8B-Instruct-fast` as its example. The script reports
whether discovery returns that ID. This is a documented candidate, not a
guarantee of current availability, your access, price or successful tool use.
Other returned IDs are explicitly marked as not tool-verified.

Check your account's model documentation and pricing. In `nano .env`, set
`NEBIUS_MODEL` to one exact accessible model ID that supports function
calling. No model is selected automatically, and the script never tries a
more expensive fallback.

```bash
python nebius_check.py
```

With a selected model, the limit is three API requests: model discovery,
one request for the quote tool call, and one request containing the tool
result. Each request has a 20-second timeout and automatic retries are
disabled. Each completion allows at most 256 output tokens. Display is
bounded to 20 model IDs and 2,000 characters per output item. There is no
tool loop.

The schema allows the two existing service codes and integer quantities
from 1 to 1,000. Python additionally requires this demonstration's exact
arguments: `wash_and_iron` and `5`. Unknown tools, extra/missing arguments,
booleans, malformed JSON, duplicate keys, missing/multiple calls, truncated
responses, further tool calls or an incorrect displayed total fail the check.

Exit codes: `0` means a completed live round trip, or successful discovery
when explicitly using `--list-models`; `2` means configuration is incomplete;
`1` means the check failed. Discovery success alone is never called a tool pass.
Errors distinguish authentication, model/project access, quota/rate limits,
connection/timeout and unsupported request/capability problems. Raw provider
errors, response bodies, headers and credentials are not printed.

## Offline verification

```bash
python -m unittest -v test_nebius_check
```

These tests use fake configuration, temporary files and simulated API
responses. They execute the real quote function, whose result is ₹250
(five garments at ₹50 each). They verify dispatch validation, the two-message
tool protocol, request limits, safe error messages and silent imports without
SQLite access. Passing offline tests does **not** demonstrate Nebius access.

An API client sends HTTP requests and parses responses. A model ID selects
the remote model; environment variables configure the client without
hard-coding a secret. The tool schema describes an allowed request, while
the Python function contains the actual pricing logic. A model emits a
tool-call instruction, not executed Python. Our program validates and runs
it, then sends the result back so the model can explain the real price.

## Official references checked

- [Quickstart: supported client, endpoint and authentication](https://docs.tokenfactory.nebius.com/quickstart)
- [API introduction: bearer authentication](https://docs.tokenfactory.nebius.com/api-reference/introduction)
- [Model discovery, including verbose metadata](https://docs.tokenfactory.nebius.com/api-reference/examples/list-of-models)
- [Tool schema, explicit tool choice, assistant tool calls and tool results](https://docs.tokenfactory.nebius.com/ai-models-inference/function-calling)
- [Chat completion parameters, including output limits](https://docs.tokenfactory.nebius.com/api-reference/inference/create-chat-completion)

Historical note: the first setup did not have configuration, so its live check
was not run then. Later session screenshots show a successful live quote tool
round trip returning Rs 250. This is an example, not a model accuracy benchmark.
No live call was repeated for submission packaging. See
[evaluation evidence](docs/EVALUATION.md) for current offline results and the
separate live examples. A fresh checkout has no key or selected model; configure
those locally before deliberately running the live commands above.

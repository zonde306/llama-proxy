import os
import json
import urllib
import itertools
import urllib.parse
import collections
import contextlib
import asyncio
import typing
import fastapi
import fastapi.middleware
import fastapi.middleware.cors
from fastapi.responses import PlainTextResponse, StreamingResponse
import curl_cffi.requests as requests
import tenacity

FORWARD_API = os.environ.get("FORWARD_API", "").split(",")
if FORWARD_API == [""]:
    FORWARD_API = []

FORWARD_API = itertools.cycle(
    FORWARD_API
    or [
        # "https://sakura.1percentsync.games/",
        "https://sakura-share.one/",
        # "https://neovax.neocloud.tw/",
        # "https://tls.0v0.io/proxy?url=https://sakura-share.one/",
        # "https://sakura.pidanshourouzhou.top/",
    ]
)

FORWARD_PROXY = os.environ.get("FORWARD_PROXY", "").split(",")
if FORWARD_PROXY == [""]:
    FORWARD_PROXY = []

FORWARD_PROXY = itertools.cycle(
    FORWARD_PROXY
    or [
        # "socks5://127.0.0.1:1080",
    ]
    or [None]
)

DEFAULT_HEADERS = {
    "Referer": "https://books.fishhawk.top/",
    "Authorization": "Bearer no-key",
}

app = fastapi.FastAPI()

app.add_middleware(
    fastapi.middleware.cors.CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

MAX_CONNECTIONS = os.environ.get("MAX_CONNECTIONS", 6)
CONNECTION_COUNTER = collections.defaultdict(int)


def get_endpoint_and_proxy():
    return next(FORWARD_API), next(FORWARD_PROXY)


def urljoin(base: str, *paths: str):
    assert len(paths) > 0, "paths must not be empty"

    if "?" not in base:
        return urllib.parse.urljoin(base, "/".join(paths))
    if base.endswith("/") and paths[0].startswith("/"):
        base = base[:-1]
    return base + "/".join(paths)


class WaitForConnection:
    def __init__(self, endpoint: str) -> None:
        self.endpoint = endpoint

    async def __aenter__(self):
        global CONNECTION_COUNTER
        while CONNECTION_COUNTER[self.endpoint] >= MAX_CONNECTIONS:
            await asyncio.sleep(1)
        CONNECTION_COUNTER[self.endpoint] += 1
        assert (
            CONNECTION_COUNTER[self.endpoint] <= MAX_CONNECTIONS
        ), "bad connection counter"

    async def __aexit__(self, exc_type, exc, tb):
        global CONNECTION_COUNTER
        CONNECTION_COUNTER[self.endpoint] -= 1
        assert CONNECTION_COUNTER[self.endpoint] >= 0, "bad connection counter"


async def fetch_completion(data: dict, path: str, timeout: float = 120):
    endpoint, proxy = get_endpoint_and_proxy()
    async with WaitForConnection(f"{proxy}#{endpoint}"):
        endpoint = urljoin(endpoint, path)
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        headers = DEFAULT_HEADERS.copy()
        headers.update({"Content-Type": "application/json; charset=utf-8"})

        async for attempt in tenacity.AsyncRetrying(
            wait=tenacity.wait_random(1, 5),
            stop=tenacity.stop_after_attempt(5),
            retry=tenacity.retry_if_exception_type(
                (
                    requests.errors.CurlError,
                    json.decoder.JSONDecodeError,
                    AssertionError,
                )
            ),
        ):
            with attempt:
                async with requests.AsyncSession() as s:
                    r = await s.post(
                        endpoint,
                        data=body,
                        impersonate="chrome",
                        proxy=proxy,
                        timeout=timeout,
                        headers=headers,
                    )
                    assert (
                        r.status_code == 200
                    ), f"bad request: {r.status_code}: {r.text} from {endpoint}"
                    return r.json()

    return None


async def fetch_completion_stream(data: dict, path: str, timeout: float = 120):
    endpoint, proxy = get_endpoint_and_proxy()
    async with WaitForConnection(f"{proxy}#{endpoint}"):
        endpoint = urljoin(endpoint, path)
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        headers = DEFAULT_HEADERS.copy()
        headers.update({"Content-Type": "application/json; charset=utf-8"})

        # stream cannot retry
        async with requests.AsyncSession() as s:
            r = await s.post(
                endpoint,
                data=body,
                impersonate="chrome",
                proxy=proxy,
                timeout=timeout,
                headers=headers,
            )
            assert (
                r.status_code == 200
            ), f"bad request: {r.status_code}: {r.text} from {endpoint}"

            # cannot use yield from
            async for chunk in r.aiter_lines():
                yield chunk


@app.get("/health")
async def health():
    global CONNECTION_COUNTER
    endpoint, proxy = get_endpoint_and_proxy()
    with contextlib.suppress((requests.errors.CurlError, json.decoder.JSONDecodeError, KeyError)):
        async with requests.AsyncSession() as s:
            r = await s.get(
                urljoin(endpoint, "/health"),
                impersonate="chrome",
                proxy=proxy,
                timeout=60,
                headers=DEFAULT_HEADERS,
            )
            result: dict = r.json()
            if result["status"].lower() == "ok":
                processing = (
                    sum(CONNECTION_COUNTER.values())
                    if CONNECTION_COUNTER
                    else result["slots_processing"]
                )
                available = (
                    min(
                        len(CONNECTION_COUNTER) * MAX_CONNECTIONS,
                        result["slots_idle"],
                    )
                    if CONNECTION_COUNTER
                    else result["slots_idle"]
                )
                result["slots_idle"] = max(available, 0)
                result["slots_processing"] = processing
                if result["slots_idle"] <= 0:
                    result["status"] = "no slot available"

            return result

    return {"status": "ok"}


async def keep_alive(coro : typing.Awaitable, timeout : float = 10):
    task = asyncio.create_task(coro)
    with not task.done():
        done, _ = await asyncio.wait({ task }, timeout=timeout)
        if not done:
            yield "\n"
    
    result = task.result()
    if not isinstance(result, str):
        yield json.dumps(result, separators=(",", ":"))
    else:
        yield result

@app.get("/")
async def index():
    status: dict = await health()
    return {
        "status": status.get("status", "error"),
    }


@app.post("/v1/chat/completions")
async def chat_completion_post(request: fastapi.Request):
    data: dict = json.loads(await request.body())
    if data.get("stream", False):
        return StreamingResponse(fetch_completion_stream(data, "/v1/chat/completions"))
    return StreamingResponse(keep_alive(fetch_completion(data, "/v1/chat/completions")))


@app.post("/completion")
async def completion_post(request: fastapi.Request):
    data: dict = json.loads(await request.body())
    if data.get("stream", False):
        return StreamingResponse(fetch_completion_stream(data, "/completion"))
    return StreamingResponse(keep_alive(fetch_completion(data, "/completion")))


@app.get("/v1/models")
async def models():
    if not os.environ.get("LLAMA_FAKE_MODELS"):
        endpoint, proxy = get_endpoint_and_proxy()
        endpoint = urljoin(endpoint, "/v1/models")
        with contextlib.suppress(
            (requests.errors.CurlError, AssertionError, json.decoder.JSONDecodeError)
        ):
            async with requests.AsyncSession() as s:
                r = await s.get(
                    endpoint,
                    impersonate="chrome",
                    proxy=proxy,
                    timeout=9,
                    headers=DEFAULT_HEADERS,
                )
                assert (
                    r.status_code == 200
                ), f"bad request: {r.status_code}: {r.text} from {endpoint}"
                return r.json()

    return {
        "object": "list",
        "data": [
            {
                "id": "sakura-14b-qwen2.5-v1.0-iq4xs.gguf",
                "object": "model",
                "created": 1732761523,
                "owned_by": "llamacpp",
                "meta": {
                    "vocab_type": 2,
                    "n_vocab": 152064,
                    "n_ctx_train": 131072,
                    "n_embd": 5120,
                    "n_params": 14770033664,
                    "size": 8180228096,
                },
            }
        ],
    }


@app.get("/slots")
async def slots():
    endpoint, proxy = get_endpoint_and_proxy()
    endpoint = urljoin(endpoint, "/slots")
    try:
        async with requests.AsyncSession() as s:
            r = await s.get(
                endpoint,
                impersonate="chrome",
                proxy=proxy,
                timeout=9,
                headers=DEFAULT_HEADERS,
            )
            assert (
                r.status_code == 200
            ), f"bad request: {r.status_code}: {r.text} from {endpoint}"
            return r.json()
    except (requests.errors.CurlError, AssertionError, json.decoder.JSONDecodeError):
        # for unknown status
        return [
            {
                "n_ctx": 1536,
                "n_predict": -1,
                "model": "sakura-14b-qwen2.5-v1.0-iq4xs",
                "seed": 4294967295,
                "seed_cur": 0,
                "temperature": 0.800000011920929,
                "dynatemp_range": 0.0,
                "dynatemp_exponent": 1.0,
                "top_k": 40,
                "top_p": 0.949999988079071,
                "min_p": 0.05000000074505806,
                "tfs_z": 1.0,
                "typical_p": 1.0,
                "repeat_last_n": 64,
                "repeat_penalty": 1.0,
                "presence_penalty": 0.0,
                "frequency_penalty": 0.0,
                "mirostat": 0,
                "mirostat_tau": 5.0,
                "mirostat_eta": 0.10000000149011612,
                "penalize_nl": False,
                "stop": [],
                "max_tokens": -1,
                "n_keep": 0,
                "n_discard": 0,
                "ignore_eos": False,
                "stream": True,
                "n_probs": 0,
                "min_keep": 0,
                "grammar": "",
                "samplers": [
                    "top_k",
                    "tfs_z",
                    "typ_p",
                    "top_p",
                    "min_p",
                    "temperature",
                ],
                "id": 0,
                "id_task": -1,
                "state": 0,
                "prompt": None,
                "next_token": {
                    "has_next_token": True,
                    "n_remain": -1,
                    "n_decoded": 0,
                    "stopped_eos": False,
                    "stopped_word": False,
                    "stopped_limit": False,
                    "stopping_word": "",
                },
            }
        ]


@app.get("/metrics")
async def metrics():
    endpoint, proxy = get_endpoint_and_proxy()
    endpoint = urljoin(endpoint, "/metrics")
    try:
        async with requests.AsyncSession() as s:
            r = await s.get(
                endpoint,
                impersonate="chrome",
                proxy=proxy,
                timeout=9,
                headers=DEFAULT_HEADERS,
            )
            assert (
                r.status_code == 200
            ), f"bad request: {r.status_code}: {r.text} from {endpoint}"
            return PlainTextResponse(r.text)
    except (requests.errors.CurlError, AssertionError, json.decoder.JSONDecodeError):
        # for unknown status
        return PlainTextResponse(
            """\
# HELP llamacpp:prompt_tokens_total Number of prompt tokens processed.
# TYPE llamacpp:prompt_tokens_total counter
llamacpp:prompt_tokens_total 0
# HELP llamacpp:prompt_seconds_total Prompt process time
# TYPE llamacpp:prompt_seconds_total counter
llamacpp:prompt_seconds_total 0
# HELP llamacpp:tokens_predicted_total Number of generation tokens processed.
# TYPE llamacpp:tokens_predicted_total counter
llamacpp:tokens_predicted_total 0
# HELP llamacpp:tokens_predicted_seconds_total Predict process time
# TYPE llamacpp:tokens_predicted_seconds_total counter
llamacpp:tokens_predicted_seconds_total 0
# HELP llamacpp:n_decode_total Total number of llama_decode() calls
# TYPE llamacpp:n_decode_total counter
llamacpp:n_decode_total 0
# HELP llamacpp:n_busy_slots_per_decode Average number of busy slots per llama_decode() call
# TYPE llamacpp:n_busy_slots_per_decode counter
llamacpp:n_busy_slots_per_decode -nan(ind)
# HELP llamacpp:prompt_tokens_seconds Average prompt throughput in tokens/s.
# TYPE llamacpp:prompt_tokens_seconds gauge
llamacpp:prompt_tokens_seconds 0
# HELP llamacpp:predicted_tokens_seconds Average generation throughput in tokens/s.
# TYPE llamacpp:predicted_tokens_seconds gauge
llamacpp:predicted_tokens_seconds 0
# HELP llamacpp:kv_cache_usage_ratio KV-cache usage. 1 means 100 percent usage.
# TYPE llamacpp:kv_cache_usage_ratio gauge
llamacpp:kv_cache_usage_ratio 0
# HELP llamacpp:kv_cache_tokens KV-cache tokens.
# TYPE llamacpp:kv_cache_tokens gauge
llamacpp:kv_cache_tokens 0
# HELP llamacpp:requests_processing Number of request processing.
# TYPE llamacpp:requests_processing gauge
llamacpp:requests_processing 0
# HELP llamacpp:requests_deferred Number of request deferred.
# TYPE llamacpp:requests_deferred gauge
llamacpp:requests_deferred 0
"""
        )

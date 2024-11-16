import itertools
import json
import urllib
import urllib.parse
import collections
import asyncio
import fastapi
import fastapi.middleware
import fastapi.middleware.cors
import curl_cffi.requests as requests
import tenacity

FORWARD_API = itertools.cycle(
    [
        # "https://sakura.1percentsync.games/",
        "https://sakura-share.one/",
        # "https://neovax.neocloud.tw/",
        # "https://tls.0v0.io/proxy?url=https://sakura-share.one/",
        # "https://sakura.pidanshourouzhou.top/",
    ]
)

FORWARD_PROXY = itertools.cycle(
    [
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

MAX_CONNECTIONS = 6
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
        assert CONNECTION_COUNTER[self.endpoint] <= MAX_CONNECTIONS, "bad connection counter"

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


@app.get("/health")
async def health():
    global CONNECTION_COUNTER
    endpoint, proxy = get_endpoint_and_proxy()
    endpoint = urljoin(endpoint, "/health")
    async with requests.AsyncSession() as s:
        r = await s.get(
            endpoint,
            impersonate="chrome",
            proxy=proxy,
            timeout=60,
            headers=DEFAULT_HEADERS,
        )
        result : dict = r.json()
        if result.get("status", "").lower() == "ok":
            processing = sum(CONNECTION_COUNTER.values()) if CONNECTION_COUNTER else result["slots_processing"]
            available = min(len(CONNECTION_COUNTER) * MAX_CONNECTIONS, result["slots_idle"]) if CONNECTION_COUNTER else result["slots_idle"]
            result["slots_idle"] = max(available, 0)
            result["slots_processing"] = processing
            if result["slots_idle"] <= 0:
                result["status"] = "no slot available"

        return result


@app.get("/")
async def index():
    status: dict = await health()
    return {
        "status": status.get("status", "error"),
    }


@app.post("/v1/chat/completions")
async def chat_completion_post(body: dict):
    return await fetch_completion(body, "/v1/chat/completions")


@app.post("/completion")
async def completion_post(body: dict):
    return await fetch_completion(body, "/completion")


@app.get("/v1/models")
async def models():
    endpoint, proxy = get_endpoint_and_proxy()
    endpoint = urljoin(endpoint, "/v1/models")
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
        # for unknown models
        return {
            "object": "list",
            "data": [
                {
                    "id": "sakura-14b-qwen2beta-v0.9.2-iq4xs",
                    "object": "model",
                    "created": 1728783938,
                    "owned_by": "llamacpp",
                    "meta": {
                        "vocab_type": 2,
                        "n_vocab": 152064,
                        "n_ctx_train": 32768,
                        "n_embd": 5120,
                        "n_params": 14167290880,
                        "size": 7908392960,
                    },
                }
            ],
        }

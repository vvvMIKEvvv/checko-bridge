from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel
import os
import requests
from datetime import datetime


CHECKO_BASE = "https://api.checko.ru/v2"
DEEPSEEK_BASE = "https://api.deepseek.com"
TIMEOUT = 60


# MCP is mounted into the existing FastAPI application below at /mcp/.
# Stateless HTTP is appropriate for Render and for independent tool calls.
mcp = FastMCP(
    "Checko Bridge",
    instructions=(
        "Use these tools to retrieve Checko company data by INN "
        "or to send a prompt to DeepSeek."
    ),
    streamable_http_path="/",
    stateless_http=True,
)


# =========================================================
# CHECKO
# =========================================================

def get_checko_key():
    key = os.getenv("CHECKO_API_KEY", "").strip()
    if not key:
        raise HTTPException(
            status_code=500,
            detail="Не задан CHECKO_API_KEY"
        )
    return key


def validate_inn(inn: str):
    inn = "".join(c for c in inn if c.isdigit())

    if len(inn) not in (10, 12):
        raise HTTPException(
            status_code=400,
            detail="ИНН должен содержать 10 или 12 цифр"
        )

    return inn


def call_checko(method, inn, extra=None):

    params = {
        "key": get_checko_key(),
        "inn": inn
    }

    if extra:
        params.update(extra)

    try:
        r = requests.get(
            f"{CHECKO_BASE}/{method}",
            params=params,
            timeout=TIMEOUT
        )

    except requests.RequestException as e:
        raise HTTPException(
            status_code=502,
            detail=f"Ошибка связи с Checko: {e}"
        )

    try:
        data = r.json()

    except ValueError:
        data = {
            "raw_text": r.text[:3000]
        }

    if not r.ok:
        raise HTTPException(
            status_code=502,
            detail={
                "checko_status": r.status_code,
                "checko_response": data
            }
        )

    return data


# =========================================================
# DEEPSEEK
# =========================================================

def get_deepseek_key():

    key = os.getenv(
        "DEEPSEEK_API_KEY",
        ""
    ).strip()

    if not key:
        raise HTTPException(
            status_code=500,
            detail="Не задан DEEPSEEK_API_KEY"
        )

    return key


class DeepSeekRequest(BaseModel):
    prompt: str


def call_deepseek(prompt: str):

    headers = {
        "Authorization":
            f"Bearer {get_deepseek_key()}",
        "Content-Type":
            "application/json"
    }

    payload = {
        "model": "deepseek-chat",
        "messages": [
            {
                "role": "user",
                "content": prompt
            }
        ],
        "stream": False
    }

    try:
        r = requests.post(
            f"{DEEPSEEK_BASE}/chat/completions",
            headers=headers,
            json=payload,
            timeout=TIMEOUT
        )

    except requests.RequestException as e:
        raise HTTPException(
            status_code=502,
            detail=f"Ошибка связи с DeepSeek: {e}"
        )

    try:
        data = r.json()

    except ValueError:
        data = {
            "raw_text": r.text[:3000]
        }

    if not r.ok:
        raise HTTPException(
            status_code=502,
            detail={
                "deepseek_status":
                    r.status_code,
                "deepseek_response":
                    data
            }
        )

    return data


# =========================================================
# MCP TOOLS
# =========================================================

@mcp.tool()
def get_company(inn: str) -> dict:
    """Get the Checko company card for a 10- or 12-digit Russian INN."""
    return call_checko("company", validate_inn(inn))


@mcp.tool()
def get_finances(inn: str) -> dict:
    """Get extended Checko financial data for a 10- or 12-digit Russian INN."""
    return call_checko(
        "finances",
        validate_inn(inn),
        {"extended": "true"},
    )


@mcp.tool()
def get_legal_cases(inn: str) -> dict:
    """Get Checko arbitration and legal-case data for a Russian INN."""
    return call_checko("legal-cases", validate_inn(inn))


@mcp.tool()
def get_enforcements(inn: str) -> dict:
    """Get Checko enforcement-proceeding data for a Russian INN."""
    return call_checko("enforcements", validate_inn(inn))


@mcp.tool()
def get_fedresurs(inn: str) -> dict:
    """Get Checko Fedresurs messages and events for a Russian INN."""
    return call_checko("fedresurs", validate_inn(inn))


@mcp.tool()
def get_bankruptcy_messages(inn: str) -> dict:
    """Get Checko bankruptcy-message data for a Russian INN."""
    return call_checko("bankruptcy-messages", validate_inn(inn))


@mcp.tool()
def get_company_bundle(inn: str) -> dict:
    """Get all available Checko data for a Russian INN in one response."""
    inn = validate_inn(inn)
    methods = [
        ("company", {}),
        ("finances", {"extended": "true"}),
        ("legal-cases", {}),
        ("enforcements", {}),
        ("fedresurs", {}),
        ("bankruptcy-messages", {}),
    ]

    out = {
        "inn": inn,
        "generated_at": datetime.now().astimezone().isoformat(),
        "source": "Checko API v2",
        "results": {},
        "errors": {},
    }

    for method, extra in methods:
        try:
            out["results"][method] = call_checko(method, inn, extra)
        except HTTPException as error:
            out["errors"][method] = error.detail

    return out


@mcp.tool()
def ask_deepseek(prompt: str) -> dict:
    """Send a prompt to DeepSeek and return its answer and usage information."""
    if not prompt.strip():
        raise ValueError("Пустой запрос")

    data = call_deepseek(prompt)
    try:
        answer = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return data

    return {
        "model": data.get("model"),
        "answer": answer,
        "usage": data.get("usage", {}),
    }


# The MCP session manager must be started with the parent FastAPI app.
mcp_asgi_app = mcp.streamable_http_app()


@asynccontextmanager
async def lifespan(_: FastAPI):
    async with mcp.session_manager.run():
        yield


app = FastAPI(
    title="Checko + DeepSeek Bridge",
    version="2.0.0",
    lifespan=lifespan,
)
app.mount("/mcp", mcp_asgi_app)


# =========================================================
# SERVICE STATUS
# =========================================================

@app.get("/")
def root():

    return {
        "service":
            "Checko + DeepSeek Bridge",
        "status":
            "ok",
        "version":
            "2.0.0",
        "checko_example":
            "/bundle/7736249977",
        "deepseek_endpoint":
            "/deepseek",
        "api_keys_exposed":
            False
    }


# =========================================================
# CHECKO ENDPOINTS
# =========================================================

@app.get("/company/{inn}")
def company(inn: str):

    inn = validate_inn(inn)

    return call_checko(
        "company",
        inn
    )


@app.get("/finances/{inn}")
def finances(inn: str):

    inn = validate_inn(inn)

    return call_checko(
        "finances",
        inn,
        {
            "extended": "true"
        }
    )


@app.get("/legal-cases/{inn}")
def legal_cases(inn: str):

    inn = validate_inn(inn)

    return call_checko(
        "legal-cases",
        inn
    )


@app.get("/enforcements/{inn}")
def enforcements(inn: str):

    inn = validate_inn(inn)

    return call_checko(
        "enforcements",
        inn
    )


@app.get("/fedresurs/{inn}")
def fedresurs(inn: str):

    inn = validate_inn(inn)

    return call_checko(
        "fedresurs",
        inn
    )


@app.get("/bankruptcy-messages/{inn}")
def bankruptcy(inn: str):

    inn = validate_inn(inn)

    return call_checko(
        "bankruptcy-messages",
        inn
    )


# =========================================================
# CHECKO BUNDLE
# =========================================================

@app.get("/bundle/{inn}")
def bundle(inn: str):

    inn = validate_inn(inn)

    methods = [
        (
            "company",
            {}
        ),
        (
            "finances",
            {
                "extended": "true"
            }
        ),
        (
            "legal-cases",
            {}
        ),
        (
            "enforcements",
            {}
        ),
        (
            "fedresurs",
            {}
        ),
        (
            "bankruptcy-messages",
            {}
        )
    ]

    out = {
        "inn": inn,
        "generated_at":
            datetime.now()
            .astimezone()
            .isoformat(),
        "source":
            "Checko API v2",
        "results": {},
        "errors": {}
    }

    for method, extra in methods:

        try:

            result = call_checko(
                method,
                inn,
                extra
            )

            out["results"][method] = result

        except HTTPException as e:

            out["errors"][method] = e.detail

    return JSONResponse(
        content=out
    )


# =========================================================
# DEEPSEEK ENDPOINT
# =========================================================

@app.post("/deepseek")
def deepseek(request: DeepSeekRequest):

    if not request.prompt.strip():

        raise HTTPException(
            status_code=400,
            detail="Пустой запрос"
        )

    data = call_deepseek(
        request.prompt
    )

    try:

        answer = (
            data["choices"][0]
            ["message"]
            ["content"]
        )

    except (
        KeyError,
        IndexError,
        TypeError
    ):

        return JSONResponse(
            content=data
        )

    return {
        "model":
            data.get("model"),
        "answer":
            answer,
        "usage":
            data.get(
                "usage",
                {}
            )
    }

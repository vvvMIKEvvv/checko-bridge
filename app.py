from contextlib import asynccontextmanager
import hmac
import json
import time
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel
import jwt
import os
import requests
from datetime import datetime
from urllib.parse import urlsplit


CHECKO_BASE = "https://api.checko.ru/v2"
DEEPSEEK_BASE = "https://api.deepseek.com"
TIMEOUT = 60

OIDC_ISSUER = os.getenv("OIDC_ISSUER", "").rstrip("/")
MCP_OWNER_SUB = os.getenv("MCP_OWNER_SUB", "").strip()
MCP_RESOURCE_URL = os.getenv("MCP_RESOURCE_URL", "").strip()
ZITADEL_INTROSPECTION_KEY_JSON = os.getenv(
    "ZITADEL_INTROSPECTION_KEY_JSON", ""
).strip()
MCP_OAUTH_SCOPES = ("openid",)


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
    json_response=True,
)


def oauth_settings_error() -> str | None:
    missing = [
        name
        for name, value in (
            ("OIDC_ISSUER", OIDC_ISSUER),
            ("MCP_OWNER_SUB", MCP_OWNER_SUB),
            ("MCP_RESOURCE_URL", MCP_RESOURCE_URL),
            ("ZITADEL_INTROSPECTION_KEY_JSON", ZITADEL_INTROSPECTION_KEY_JSON),
        )
        if not value
    ]
    if missing:
        return "Не заданы OAuth-переменные Render: " + ", ".join(missing)
    return None


def zitadel_introspection_key() -> dict:
    """Read the JSON key of the separate ZITADEL API application."""
    try:
        key = json.loads(ZITADEL_INTROSPECTION_KEY_JSON)
    except json.JSONDecodeError as error:
        raise ValueError("ZITADEL_INTROSPECTION_KEY_JSON содержит некорректный JSON") from error

    required = ("keyId", "key", "clientId")
    if not all(isinstance(key.get(name), str) and key[name] for name in required):
        raise ValueError(
            "ZITADEL_INTROSPECTION_KEY_JSON должен содержать keyId, key и clientId"
        )
    return key


def mcp_resource_metadata_url() -> str:
    parsed = urlsplit(MCP_RESOURCE_URL)
    return (
        f"{parsed.scheme}://{parsed.netloc}"
        "/.well-known/oauth-protected-resource"
    )


async def oauth_unauthorized(scope, receive, send, detail: str) -> None:
    response = JSONResponse(
        status_code=401,
        content={"detail": detail},
        headers={
            "WWW-Authenticate": (
                'Bearer resource_metadata="'
                + mcp_resource_metadata_url()
                + '", scope="'
                + " ".join(MCP_OAUTH_SCOPES)
                + '"'
            )
        },
    )
    await response(scope, receive, send)


def verify_mcp_access_token(token: str) -> None:
    """Validate either an opaque or a JWT ZITADEL access token by introspection."""
    if error := oauth_settings_error():
        raise RuntimeError(error)

    key = zitadel_introspection_key()
    now = int(time.time())
    assertion = jwt.encode(
        {
            "iss": key["clientId"],
            "sub": key["clientId"],
            # ZITADEL requires the issuer URL here, not the introspection URL.
            "aud": OIDC_ISSUER,
            "iat": now,
            "exp": now + 60,
            "jti": str(uuid4()),
        },
        key["key"].replace("\\n", "\n"),
        algorithm="RS256",
        headers={"kid": key["keyId"]},
    )

    try:
        response = requests.post(
            f"{OIDC_ISSUER}/oauth/v2/introspect",
            data={
                "client_assertion_type": (
                    "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
                ),
                "client_assertion": assertion,
                "token": token,
                "token_type_hint": "access_token",
            },
            timeout=TIMEOUT,
        )
        response.raise_for_status()
        claims = response.json()
    except (requests.RequestException, ValueError) as error:
        raise ValueError("Не удалось проверить OAuth token через ZITADEL") from error

    if claims.get("active") is not True:
        raise ValueError("OAuth token неактивен")

    if claims.get("iss") != OIDC_ISSUER:
        raise ValueError("OAuth token выдан другим issuer")

    subject = claims.get("sub")
    if not isinstance(subject, str) or not hmac.compare_digest(subject, MCP_OWNER_SUB):
        raise ValueError("Токен выдан не владельцу MCP")


class MCPBearerAuthMiddleware:
    """Require a valid ZITADEL OAuth access token for every MCP request."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers", []))
        authorization = headers.get(b"authorization", b"").decode("latin-1")
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token:
            await oauth_unauthorized(scope, receive, send, "Требуется OAuth Bearer token")
            return

        try:
            verify_mcp_access_token(token)
        except (jwt.PyJWTError, ValueError, RuntimeError):
            await oauth_unauthorized(scope, receive, send, "Недействительный OAuth token")
            return

        await self.app(scope, receive, send)


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

@mcp.tool(
    meta={
        "securitySchemes": [
            {"type": "oauth2", "scopes": ["openid"]}
        ]
    },
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
def get_company(inn: str) -> dict:
    """Get the Checko company card for a 10- or 12-digit Russian INN."""
    return call_checko("company", validate_inn(inn))


@mcp.tool(
    meta={
        "securitySchemes": [
            {"type": "oauth2", "scopes": ["openid"]}
        ]
    },
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
def get_finances(inn: str) -> dict:
    """Get extended Checko financial data for a 10- or 12-digit Russian INN."""
    return call_checko(
        "finances",
        validate_inn(inn),
        {"extended": "true"},
    )


@mcp.tool(
    meta={
        "securitySchemes": [
            {"type": "oauth2", "scopes": ["openid"]}
        ]
    },
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
def get_legal_cases(inn: str) -> dict:
    """Get Checko arbitration and legal-case data for a Russian INN."""
    return call_checko("legal-cases", validate_inn(inn))


@mcp.tool(
    meta={
        "securitySchemes": [
            {"type": "oauth2", "scopes": ["openid"]}
        ]
    },
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
def get_enforcements(inn: str) -> dict:
    """Get Checko enforcement-proceeding data for a Russian INN."""
    return call_checko("enforcements", validate_inn(inn))


@mcp.tool(
    meta={
        "securitySchemes": [
            {"type": "oauth2", "scopes": ["openid"]}
        ]
    },
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
def get_fedresurs(inn: str) -> dict:
    """Get Checko Fedresurs messages and events for a Russian INN."""
    return call_checko("fedresurs", validate_inn(inn))


@mcp.tool(
    meta={
        "securitySchemes": [
            {"type": "oauth2", "scopes": ["openid"]}
        ]
    },
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
def get_bankruptcy_messages(inn: str) -> dict:
    """Get Checko bankruptcy-message data for a Russian INN."""
    return call_checko("bankruptcy-messages", validate_inn(inn))


@mcp.tool(
    meta={
        "securitySchemes": [
            {"type": "oauth2", "scopes": ["openid"]}
        ]
    },
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
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


@mcp.tool(
    meta={
        "securitySchemes": [
            {"type": "oauth2", "scopes": ["openid"]}
        ]
    },
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
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
# DCR and the authorization-code / PKCE flow are implemented by ZITADEL and
# ChatGPT. This service is the protected resource server and introspects the
# access token, because ZITADEL DCR access tokens are not guaranteed to be JWTs.
mcp_asgi_app = MCPBearerAuthMiddleware(mcp.streamable_http_app())


@asynccontextmanager
async def lifespan(_: FastAPI):
    if error := oauth_settings_error():
        raise RuntimeError(error)
    async with mcp.session_manager.run():
        yield


app = FastAPI(
    title="Checko + DeepSeek Bridge",
    version="2.0.0",
    lifespan=lifespan,
)
app.mount("/mcp", mcp_asgi_app)


# =========================================================
# MCP OAUTH DISCOVERY
# =========================================================

@app.get("/.well-known/oauth-protected-resource", include_in_schema=False)
def oauth_protected_resource_metadata():
    """Advertise the ZITADEL authorization server for the protected MCP."""
    if error := oauth_settings_error():
        raise HTTPException(status_code=500, detail=error)

    return {
        "resource": MCP_RESOURCE_URL,
        "authorization_servers": [OIDC_ISSUER],
        "scopes_supported": list(MCP_OAUTH_SCOPES),
    }


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

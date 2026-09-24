from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
import os
import requests
from datetime import datetime


app = FastAPI(
    title="Checko + DeepSeek Bridge",
    version="2.0.0"
)

CHECKO_BASE = "https://api.checko.ru/v2"
DEEPSEEK_BASE = "https://api.deepseek.com"
TIMEOUT = 60


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

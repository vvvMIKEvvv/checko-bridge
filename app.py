from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
import os, requests
from datetime import datetime

app = FastAPI(title="Checko Bridge", version="1.0.0")
BASE = "https://api.checko.ru/v2"
TIMEOUT = 45

def get_key():
    key = os.getenv("CHECKO_API_KEY", "").strip()
    if not key:
        raise HTTPException(500, "Не задан CHECKO_API_KEY")
    return key

def validate_inn(inn: str):
    inn = "".join(c for c in inn if c.isdigit())
    if len(inn) not in (10, 12):
        raise HTTPException(400, "ИНН должен содержать 10 или 12 цифр")
    return inn

def call(method, inn, extra=None):
    params = {"key": get_key(), "inn": inn}
    if extra:
        params.update(extra)
    try:
        r = requests.get(f"{BASE}/{method}", params=params, timeout=TIMEOUT)
    except requests.RequestException as e:
        raise HTTPException(502, f"Ошибка связи с Checko: {e}")
    try:
        data = r.json()
    except ValueError:
        data = {"raw_text": r.text[:3000]}
    if not r.ok:
        raise HTTPException(502, {"checko_status": r.status_code, "checko_response": data})
    return data

@app.get("/")
def root():
    return {
        "service": "Checko Bridge",
        "status": "ok",
        "bundle_example": "/bundle/7736249977",
        "api_key_exposed": False
    }

@app.get("/company/{inn}")
def company(inn: str):
    return call("company", validate_inn(inn))

@app.get("/finances/{inn}")
def finances(inn: str):
    return call("finances", validate_inn(inn), {"extended": "true"})

@app.get("/legal-cases/{inn}")
def legal_cases(inn: str):
    return call("legal-cases", validate_inn(inn))

@app.get("/enforcements/{inn}")
def enforcements(inn: str):
    return call("enforcements", validate_inn(inn))

@app.get("/fedresurs/{inn}")
def fedresurs(inn: str):
    return call("fedresurs", validate_inn(inn))

@app.get("/bankruptcy-messages/{inn}")
def bankruptcy(inn: str):
    return call("bankruptcy-messages", validate_inn(inn))

@app.get("/bundle/{inn}")
def bundle(inn: str):
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
        "errors": {}
    }
    for method, extra in methods:
        try:
            out["results"][method] = call(method, inn, extra)
        except HTTPException as e:
            out["errors"][method] = e.detail
    return JSONResponse(out)

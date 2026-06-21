"""Standalone test service — run with: uv run python test_service.py
OpenAPI spec available at: http://localhost:8001/openapi.json

Auth test credentials:
  bearer token : test-bearer-secret
  api key      : test-api-key
  basic        : admin / test-pass
  oauth2       : client_id=test-client, client_secret=test-secret
"""

import time
import uuid

import uvicorn
from fastapi import FastAPI, Form, HTTPException, Security
from fastapi.security import APIKeyHeader, APIKeyQuery, HTTPBasic, HTTPBasicCredentials, HTTPBearer
from pydantic import BaseModel

app = FastAPI(title="Test Service", version="1.0")

# ── Secrets ────────────────────────────────────────────────────────────────

BEARER_SECRET = "test-bearer-secret"
API_KEY_SECRET = "test-api-key"
BASIC_USER, BASIC_PASS = "admin", "test-pass"
OAUTH_CLIENT_ID, OAUTH_CLIENT_SECRET = "test-client", "test-secret"
OAUTH_TOKEN = "oauth2-issued-token"
OAUTH_REFRESH = "test-refresh-token"

_bearer = HTTPBearer()
_apikey_header = APIKeyHeader(name="X-API-Key")
_apikey_query = APIKeyQuery(name="api_key")
_basic = HTTPBasic()


def _check_bearer(creds=Security(_bearer)):
    if creds.credentials != BEARER_SECRET:
        raise HTTPException(401, "Invalid bearer token")


def _check_apikey_header(key: str = Security(_apikey_header)):
    if key != API_KEY_SECRET:
        raise HTTPException(401, "Invalid API key")


def _check_apikey_query(key: str = Security(_apikey_query)):
    if key != API_KEY_SECRET:
        raise HTTPException(401, "Invalid API key")


def _check_basic(creds: HTTPBasicCredentials = Security(_basic)):
    if creds.username != BASIC_USER or creds.password != BASIC_PASS:
        raise HTTPException(401, "Invalid credentials")


# ── Weather (bearer) ────────────────────────────────────────────────────────

WEATHER = {
    "london": {"temperature_c": 12, "condition": "rainy", "humidity_pct": 82, "wind_kmh": 25},
    "tokyo": {"temperature_c": 28, "condition": "sunny", "humidity_pct": 60, "wind_kmh": 10},
    "new york": {"temperature_c": 18, "condition": "cloudy", "humidity_pct": 55, "wind_kmh": 18},
    "sydney": {"temperature_c": 22, "condition": "sunny", "humidity_pct": 50, "wind_kmh": 15},
    "reykjavik": {"temperature_c": -2, "condition": "snowy", "humidity_pct": 90, "wind_kmh": 40},
    "chennai": {"temperature_c": 35, "condition": "sunny", "humidity_pct": 75, "wind_kmh": 12},
    "paris": {"temperature_c": 15, "condition": "cloudy", "humidity_pct": 65, "wind_kmh": 14},
    "mumbai": {"temperature_c": 32, "condition": "rainy", "humidity_pct": 85, "wind_kmh": 20},
}


@app.get("/weather", summary="Get weather for a city")
def get_weather(city: str, _=Security(_bearer)):
    if _.credentials != BEARER_SECRET:
        raise HTTPException(401, "Invalid bearer token")
    return WEATHER.get(
        city.lower(),
        {"temperature_c": 20, "condition": "sunny", "humidity_pct": 50, "wind_kmh": 10},
    )


# ── UV Index (apiKey header) ────────────────────────────────────────────────

UV = {
    "london": {"uv_index": 2.1, "risk_level": "low", "minutes_to_burn": 60},
    "tokyo": {"uv_index": 7.5, "risk_level": "high", "minutes_to_burn": 20},
    "new york": {"uv_index": 5.0, "risk_level": "moderate", "minutes_to_burn": 35},
    "sydney": {"uv_index": 11.0, "risk_level": "extreme", "minutes_to_burn": 10},
    "chennai": {"uv_index": 9.0, "risk_level": "very_high", "minutes_to_burn": 15},
    "mumbai": {"uv_index": 8.5, "risk_level": "very_high", "minutes_to_burn": 18},
}


@app.get("/uv", summary="Get UV index for a city")
def get_uv(city: str, key: str = Security(_apikey_header)):
    if key != API_KEY_SECRET:
        raise HTTPException(401, "Invalid API key")
    return UV.get(city.lower(), {"uv_index": 4.0, "risk_level": "moderate", "minutes_to_burn": 40})


# ── Clothing (no auth) ──────────────────────────────────────────────────────


class ClothingRequest(BaseModel):
    temperature_c: int
    condition: str
    wind_kmh: int


@app.post("/clothing", summary="Recommend clothing for given weather conditions")
def get_clothing(req: ClothingRequest):
    accessories = []
    if req.condition == "rainy":
        accessories.append("umbrella")
    if req.condition == "snowy":
        accessories += ["gloves", "beanie"]
    if req.wind_kmh > 30:
        accessories.append("scarf")
    if req.temperature_c > 20 and req.condition == "sunny":
        accessories.append("sunglasses")

    if req.temperature_c < 0:
        return {
            "top": "thermal undershirt + heavy sweater",
            "bottom": "thermal leggings + insulated trousers",
            "outerwear": "heavy winter coat",
            "accessories": accessories,
            "comfort_score": 5,
        }
    elif req.temperature_c < 10:
        return {
            "top": "long-sleeve shirt + jumper",
            "bottom": "jeans",
            "outerwear": "winter coat",
            "accessories": accessories,
            "comfort_score": 6,
        }
    elif req.temperature_c < 18:
        return {
            "top": "t-shirt + light sweater",
            "bottom": "jeans",
            "outerwear": "light jacket",
            "accessories": accessories,
            "comfort_score": 8,
        }
    elif req.temperature_c < 25:
        return {
            "top": "t-shirt",
            "bottom": "chinos or jeans",
            "outerwear": None,
            "accessories": accessories,
            "comfort_score": 9,
        }
    else:
        return {
            "top": "breathable t-shirt",
            "bottom": "shorts",
            "outerwear": None,
            "accessories": accessories,
            "comfort_score": 7,
        }


# ── Currency (no auth) ──────────────────────────────────────────────────────

RATES = {"USD": 1.0, "EUR": 1.08, "GBP": 1.27, "JPY": 0.0067, "AUD": 0.65, "INR": 0.012}


@app.get("/currency/convert", summary="Convert an amount between currencies")
def convert_currency(amount: float, from_currency: str, to_currency: str):
    src, dst = from_currency.upper(), to_currency.upper()
    rate = RATES.get(src, 1.0) / RATES.get(dst, 1.0)
    return {
        "converted_amount": round(amount * rate, 2),
        "rate": round(rate, 4),
        "from_currency": src,
        "to_currency": dst,
    }


# ── City info (apiKey query) ────────────────────────────────────────────────

CITIES = {
    "london": {
        "country": "UK",
        "population_millions": 9.0,
        "timezone": "Europe/London",
        "language": "English",
        "currency": "GBP",
    },
    "tokyo": {
        "country": "Japan",
        "population_millions": 13.9,
        "timezone": "Asia/Tokyo",
        "language": "Japanese",
        "currency": "JPY",
    },
    "new york": {
        "country": "USA",
        "population_millions": 8.3,
        "timezone": "America/New_York",
        "language": "English",
        "currency": "USD",
    },
    "sydney": {
        "country": "Australia",
        "population_millions": 5.3,
        "timezone": "Australia/Sydney",
        "language": "English",
        "currency": "AUD",
    },
    "paris": {
        "country": "France",
        "population_millions": 2.1,
        "timezone": "Europe/Paris",
        "language": "French",
        "currency": "EUR",
    },
    "mumbai": {
        "country": "India",
        "population_millions": 12.4,
        "timezone": "Asia/Kolkata",
        "language": "Hindi/Marathi",
        "currency": "INR",
    },
    "chennai": {
        "country": "India",
        "population_millions": 7.1,
        "timezone": "Asia/Kolkata",
        "language": "Tamil",
        "currency": "INR",
    },
}


@app.get("/city", summary="Get info about a city")
def get_city_info(city: str, key: str = Security(_apikey_query)):
    if key != API_KEY_SECRET:
        raise HTTPException(401, "Invalid API key")
    return CITIES.get(
        city.lower(),
        {
            "country": "Unknown",
            "population_millions": 1.0,
            "timezone": "UTC",
            "language": "Unknown",
            "currency": "USD",
        },
    )


# ── Flight (basic auth) ─────────────────────────────────────────────────────

FLIGHTS = {
    ("london", "new york"): {"estimated_cost_usd": 450, "duration_hours": 7.5, "direct": True},
    ("new york", "london"): {"estimated_cost_usd": 420, "duration_hours": 7.0, "direct": True},
    ("london", "tokyo"): {"estimated_cost_usd": 800, "duration_hours": 12.0, "direct": True},
    ("tokyo", "sydney"): {"estimated_cost_usd": 550, "duration_hours": 9.5, "direct": True},
    ("new york", "paris"): {"estimated_cost_usd": 380, "duration_hours": 7.2, "direct": True},
    ("paris", "mumbai"): {"estimated_cost_usd": 620, "duration_hours": 9.0, "direct": True},
    ("mumbai", "chennai"): {"estimated_cost_usd": 80, "duration_hours": 1.5, "direct": True},
    ("chennai", "london"): {"estimated_cost_usd": 700, "duration_hours": 10.5, "direct": True},
}


@app.get("/flight", summary="Estimate flight cost between two cities")
def estimate_flight(
    origin: str,
    destination: str,
    passengers: int = 1,
    creds: HTTPBasicCredentials = Security(_basic),
):
    if creds.username != BASIC_USER or creds.password != BASIC_PASS:
        raise HTTPException(401, "Invalid credentials")
    base = FLIGHTS.get(
        (origin.lower(), destination.lower()),
        {"estimated_cost_usd": 500, "duration_hours": 8.0, "direct": False},
    )
    return {
        "estimated_cost_usd": round(base["estimated_cost_usd"] * passengers, 2),
        "duration_hours": base["duration_hours"],
        "direct": base["direct"],
    }


# ── OAuth2 token + user profile ─────────────────────────────────────────────


@app.get("/.well-known/openid-configuration", include_in_schema=False)
def oidc_discovery():
    return {
        "issuer": "http://localhost:8001",
        "token_endpoint": "http://localhost:8001/oauth/token",
        "response_types_supported": ["code"],
    }


@app.post("/oauth/token", include_in_schema=False)
def oauth_token(
    grant_type: str = Form(...),
    client_id: str = Form(default=""),
    client_secret: str = Form(default=""),
    refresh_token: str = Form(default=""),
):
    if grant_type == "client_credentials":
        if client_id != OAUTH_CLIENT_ID or client_secret != OAUTH_CLIENT_SECRET:
            raise HTTPException(401, "Invalid client credentials")
    elif grant_type == "refresh_token":
        if refresh_token != OAUTH_REFRESH:
            raise HTTPException(401, "Invalid refresh token")
    else:
        raise HTTPException(400, "Unsupported grant_type")
    return {"access_token": OAUTH_TOKEN, "token_type": "bearer", "expires_in": 3600}


@app.get("/user/profile/oidc", summary="Get user profile via OIDC auth")
def user_profile_oidc(creds=Security(_bearer)):
    if creds.credentials != OAUTH_TOKEN:
        raise HTTPException(401, "Invalid token")
    return {
        "user_id": "usr_oidc_001",
        "name": "OIDC User",
        "email": "oidc@example.com",
        "auth_method": "oidc",
        "plan": "enterprise",
    }


@app.get("/user/profile", summary="Get the authenticated user's profile and travel stats")
def user_profile(creds=Security(_bearer)):
    if creds.credentials != OAUTH_TOKEN:
        raise HTTPException(401, "Invalid token")
    return {
        "user_id": "usr_001",
        "name": "Test User",
        "email": "test@example.com",
        "plan": "premium",
        "home_city": "London",
        "preferred_currency": "GBP",
        "trips_this_year": 7,
        "loyalty_points": 12400,
        "upcoming_trips": [
            {"destination": "Tokyo", "date": "2026-07-15", "passengers": 2},
        ],
    }


# inject OAuth2 securityScheme for /user/profile
_orig_openapi = app.openapi


def _patched_openapi():
    schema = _orig_openapi()
    schemes = schema.setdefault("components", {}).setdefault("securitySchemes", {})
    schemes["OAuth2"] = {
        "type": "oauth2",
        "flows": {
            "clientCredentials": {"tokenUrl": "http://localhost:8001/oauth/token", "scopes": {}}
        },
    }
    schemes["OpenIDConnect"] = {
        "type": "openIdConnect",
        "openIdConnectUrl": "http://localhost:8001/.well-known/openid-configuration",
    }
    paths = schema.get("paths", {})
    if "/user/profile" in paths:
        paths["/user/profile"]["get"]["security"] = [{"OAuth2": []}]
    if "/user/profile/oidc" in paths:
        paths["/user/profile/oidc"]["get"]["security"] = [{"OpenIDConnect": []}]
    return schema


app.openapi = _patched_openapi  # type: ignore


# ── Daily weather tracker (schedule testing) ────────────────────────────────
# Values cycle deterministically by day so a scheduled agent sees real changes.

_DAILY_CONDITIONS = ["sunny", "cloudy", "rainy", "windy", "stormy", "foggy"]
_DAILY_TEMPS = [28, 24, 18, 15, 12, 22, 26]


@app.get("/weather/daily", summary="Get today's weather reading for a city (changes daily)")
def get_daily_weather(city: str):
    from datetime import date

    day = date.today().toordinal()
    cond = _DAILY_CONDITIONS[day % len(_DAILY_CONDITIONS)]
    temp = _DAILY_TEMPS[day % len(_DAILY_TEMPS)]
    return {
        "city": city,
        "date": date.today().isoformat(),
        "temperature_c": temp,
        "condition": cond,
        "humidity_pct": 50 + (day % 40),
        "wind_kmh": 10 + (day % 30),
    }


# ── Weather forecast report (defer_turn / defer_turn_until testing) ─────────
# Simulates a slow forecast generation job (e.g. model run takes 30s).
# POST /weather/forecast/request → job_id
# GET  /weather/forecast/{job_id} → pending | done + 7-day forecast

_forecast_jobs: dict[str, dict] = {}
_FORECAST_CONDS = ["sunny", "cloudy", "rainy", "windy", "stormy", "foggy", "sunny"]


@app.post(
    "/weather/forecast/request", summary="Request a 7-day forecast report (takes time to generate)"
)
def request_forecast(city: str, duration_seconds: int = 30):
    job_id = str(uuid.uuid4())
    _forecast_jobs[job_id] = {"city": city, "started_at": time.time(), "duration": duration_seconds}
    return {
        "job_id": job_id,
        "city": city,
        "status": "pending",
        "ready_in_seconds": duration_seconds,
    }


@app.get("/weather/forecast/{job_id}", summary="Get forecast report status and result")
def get_forecast(job_id: str):
    job = _forecast_jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Forecast job not found")
    elapsed = time.time() - job["started_at"]
    if elapsed < job["duration"]:
        return {
            "job_id": job_id,
            "status": "pending",
            "remaining_seconds": round(job["duration"] - elapsed, 1),
        }
    from datetime import date, timedelta

    base = date.today()
    forecast = [
        {
            "date": (base + timedelta(days=i)).isoformat(),
            "condition": _FORECAST_CONDS[i],
            "high_c": 20 + i,
            "low_c": 12 + i,
        }
        for i in range(7)
    ]
    return {"job_id": job_id, "city": job["city"], "status": "done", "forecast": forecast}


# ── Weather alert (defer_turn_until testing) ────────────────────────────────
# Simulates a weather alert that activates at a fixed scheduled UTC time
# (next 2-minute boundary). Agent should use defer_turn_until(ready_at).

_alert_subscriptions: dict[str, dict] = {}


@app.post(
    "/weather/alert/subscribe",
    summary="Subscribe to next weather alert for a city (fires at a scheduled UTC time)",
)
def subscribe_alert(city: str):
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    # next 2-minute boundary
    minutes = (now.minute // 2 + 1) * 2
    alert_at = now.replace(minute=0, second=0, microsecond=0) + timedelta(minutes=minutes)
    sub_id = str(uuid.uuid4())
    _alert_subscriptions[sub_id] = {"city": city, "alert_at": alert_at.timestamp()}
    return {"subscription_id": sub_id, "city": city, "alert_at": alert_at.isoformat()}


@app.get(
    "/weather/alert/{subscription_id}", summary="Get weather alert (only available after alert_at)"
)
def get_alert(subscription_id: str):
    sub = _alert_subscriptions.get(subscription_id)
    if not sub:
        raise HTTPException(404, "Subscription not found")
    from datetime import UTC, datetime

    if time.time() < sub["alert_at"]:
        ready_at = datetime.fromtimestamp(sub["alert_at"], tz=UTC).isoformat()
        return {"subscription_id": subscription_id, "status": "pending", "alert_at": ready_at}
    day = int(sub["alert_at"]) % len(_DAILY_CONDITIONS)
    return {
        "subscription_id": subscription_id,
        "city": sub["city"],
        "status": "active",
        "severity": "moderate",
        "condition": _DAILY_CONDITIONS[day],
        "message": f"Weather alert for {sub['city']}: {_DAILY_CONDITIONS[day]} conditions expected.",
    }


if __name__ == "__main__":
    uvicorn.run(app, host="localhost", port=8001)

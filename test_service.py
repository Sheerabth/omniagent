import logging
import os
import sys

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

import omniagent
from omniagent import ToolInput, ToolOutput, tool

# ── Weather ────────────────────────────────────────────────────────────────


class WeatherInput(ToolInput):
    city: str


class WeatherOutput(ToolOutput):
    temperature_c: int
    condition: str  # sunny | cloudy | rainy | snowy
    humidity_pct: int
    wind_kmh: int


@tool(
    description="Get the current weather for a city. Returns temperature, condition, humidity, and wind speed."
)
async def get_weather(inp: WeatherInput) -> WeatherOutput:
    data = {
        "london": WeatherOutput(temperature_c=12, condition="rainy", humidity_pct=82, wind_kmh=25),
        "tokyo": WeatherOutput(temperature_c=28, condition="sunny", humidity_pct=60, wind_kmh=10),
        "new york": WeatherOutput(
            temperature_c=18, condition="cloudy", humidity_pct=55, wind_kmh=18
        ),
        "sydney": WeatherOutput(temperature_c=22, condition="sunny", humidity_pct=50, wind_kmh=15),
        "reykjavik": WeatherOutput(
            temperature_c=-2, condition="snowy", humidity_pct=90, wind_kmh=40
        ),
    }
    return data.get(
        inp.city.lower(),
        WeatherOutput(temperature_c=20, condition="sunny", humidity_pct=50, wind_kmh=10),
    )


# ── UV Index ───────────────────────────────────────────────────────────────


class UVInput(ToolInput):
    city: str


class UVOutput(ToolOutput):
    uv_index: float
    risk_level: str  # low | moderate | high | very_high | extreme
    minutes_to_burn: int


@tool(description="Get the current UV index and burn time for a city.")
async def get_uv_index(inp: UVInput) -> UVOutput:
    uv_map = {
        "london": UVOutput(uv_index=2.1, risk_level="low", minutes_to_burn=60),
        "tokyo": UVOutput(uv_index=7.5, risk_level="high", minutes_to_burn=20),
        "new york": UVOutput(uv_index=5.0, risk_level="moderate", minutes_to_burn=35),
        "sydney": UVOutput(uv_index=11.0, risk_level="extreme", minutes_to_burn=10),
        "reykjavik": UVOutput(uv_index=0.8, risk_level="low", minutes_to_burn=120),
    }
    return uv_map.get(
        inp.city.lower(), UVOutput(uv_index=4.0, risk_level="moderate", minutes_to_burn=40)
    )


# ── Clothing recommendation ────────────────────────────────────────────────


class ClothingInput(ToolInput):
    temperature_c: int
    condition: str
    wind_kmh: int


class ClothingOutput(ToolOutput):
    top: str
    bottom: str
    outerwear: str | None
    accessories: list[str]
    comfort_score: int  # 1-10


@tool(description="Recommend clothing based on temperature, weather condition, and wind speed.")
async def get_clothing_recommendation(inp: ClothingInput) -> ClothingOutput:
    accessories = []

    if inp.condition == "rainy":
        accessories.append("umbrella")
    if inp.condition == "snowy":
        accessories += ["gloves", "beanie"]
    if inp.wind_kmh > 30:
        accessories.append("scarf")
    if inp.temperature_c > 20 and inp.condition == "sunny":
        accessories.append("sunglasses")

    if inp.temperature_c < 0:
        return ClothingOutput(
            top="thermal undershirt + heavy sweater",
            bottom="thermal leggings + insulated trousers",
            outerwear="heavy winter coat",
            accessories=accessories,
            comfort_score=5,
        )
    elif inp.temperature_c < 10:
        return ClothingOutput(
            top="long-sleeve shirt + jumper",
            bottom="jeans",
            outerwear="winter coat",
            accessories=accessories,
            comfort_score=6,
        )
    elif inp.temperature_c < 18:
        return ClothingOutput(
            top="t-shirt + light sweater",
            bottom="jeans",
            outerwear="light jacket",
            accessories=accessories,
            comfort_score=8,
        )
    elif inp.temperature_c < 25:
        return ClothingOutput(
            top="t-shirt",
            bottom="chinos or jeans",
            outerwear=None,
            accessories=accessories,
            comfort_score=9,
        )
    else:
        return ClothingOutput(
            top="breathable t-shirt",
            bottom="shorts",
            outerwear=None,
            accessories=accessories,
            comfort_score=7,
        )


# ── Currency conversion ────────────────────────────────────────────────────


class CurrencyInput(ToolInput):
    amount: float
    from_currency: str  # e.g. USD
    to_currency: str  # e.g. EUR


class CurrencyOutput(ToolOutput):
    converted_amount: float
    rate: float
    from_currency: str
    to_currency: str


RATES_TO_USD = {"USD": 1.0, "EUR": 1.08, "GBP": 1.27, "JPY": 0.0067, "AUD": 0.65, "INR": 0.012}


@tool(description="Convert an amount between currencies. Supports USD, EUR, GBP, JPY, AUD, INR.")
async def convert_currency(inp: CurrencyInput) -> CurrencyOutput:
    src = inp.from_currency.upper()
    dst = inp.to_currency.upper()
    rate_src = RATES_TO_USD.get(src, 1.0)
    rate_dst = RATES_TO_USD.get(dst, 1.0)
    rate = rate_src / rate_dst
    converted = round(inp.amount * rate, 2)
    return CurrencyOutput(
        converted_amount=converted, rate=round(rate, 4), from_currency=src, to_currency=dst
    )


# ── City info ──────────────────────────────────────────────────────────────


class CityInfoInput(ToolInput):
    city: str


class CityInfoOutput(ToolOutput):
    country: str
    population_millions: float
    timezone: str
    language: str
    currency: str


CITY_DATA = {
    "london": CityInfoOutput(
        country="UK",
        population_millions=9.0,
        timezone="Europe/London",
        language="English",
        currency="GBP",
    ),
    "tokyo": CityInfoOutput(
        country="Japan",
        population_millions=13.9,
        timezone="Asia/Tokyo",
        language="Japanese",
        currency="JPY",
    ),
    "new york": CityInfoOutput(
        country="USA",
        population_millions=8.3,
        timezone="America/New_York",
        language="English",
        currency="USD",
    ),
    "sydney": CityInfoOutput(
        country="Australia",
        population_millions=5.3,
        timezone="Australia/Sydney",
        language="English",
        currency="AUD",
    ),
    "paris": CityInfoOutput(
        country="France",
        population_millions=2.1,
        timezone="Europe/Paris",
        language="French",
        currency="EUR",
    ),
    "mumbai": CityInfoOutput(
        country="India",
        population_millions=12.4,
        timezone="Asia/Kolkata",
        language="Hindi/Marathi",
        currency="INR",
    ),
}


@tool(
    description="Get general info about a city: country, population, timezone, language, and local currency."
)
async def get_city_info(inp: CityInfoInput) -> CityInfoOutput:
    return CITY_DATA.get(
        inp.city.lower(),
        CityInfoOutput(
            country="Unknown",
            population_millions=1.0,
            timezone="UTC",
            language="Unknown",
            currency="USD",
        ),
    )


# ── Flight cost estimate ───────────────────────────────────────────────────


class FlightInput(ToolInput):
    origin_city: str
    destination_city: str
    passengers: int


class FlightOutput(ToolOutput):
    estimated_cost_usd: float
    duration_hours: float
    direct_available: bool


FLIGHT_DATA = {
    ("london", "new york"): FlightOutput(
        estimated_cost_usd=450, duration_hours=7.5, direct_available=True
    ),
    ("new york", "london"): FlightOutput(
        estimated_cost_usd=420, duration_hours=7.0, direct_available=True
    ),
    ("london", "tokyo"): FlightOutput(
        estimated_cost_usd=800, duration_hours=12.0, direct_available=True
    ),
    ("tokyo", "sydney"): FlightOutput(
        estimated_cost_usd=550, duration_hours=9.5, direct_available=True
    ),
    ("new york", "paris"): FlightOutput(
        estimated_cost_usd=380, duration_hours=7.2, direct_available=True
    ),
    ("paris", "mumbai"): FlightOutput(
        estimated_cost_usd=620, duration_hours=9.0, direct_available=True
    ),
}


@tool(
    description="Estimate flight cost and duration between two cities for a number of passengers."
)
async def estimate_flight(inp: FlightInput) -> FlightOutput:
    key = (inp.origin_city.lower(), inp.destination_city.lower())
    base = FLIGHT_DATA.get(
        key, FlightOutput(estimated_cost_usd=500, duration_hours=8.0, direct_available=False)
    )
    return FlightOutput(
        estimated_cost_usd=round(base.estimated_cost_usd * inp.passengers, 2),
        duration_hours=base.duration_hours,
        direct_available=base.direct_available,
    )


# ── App ────────────────────────────────────────────────────────────────────

app = FastAPI()


class ExecuteRequest(BaseModel):
    tool: str
    input: dict


logger = logging.getLogger(__name__)


@app.post("/execute")
async def execute(body: ExecuteRequest):
    try:
        output = await omniagent.handle_execute(body.tool, body.input)
        return {"output": output}
    except KeyError as e:
        raise HTTPException(404, detail=f"Tool '{body.tool}' not found") from e
    except Exception as e:
        logger.exception("execute failed for tool=%s", body.tool)
        raise HTTPException(500, detail=str(e)) from e


api_key = os.environ.get("OMNIAGENT_SERVICE_KEY")
if not api_key:
    print("OMNIAGENT_SERVICE_KEY environment variable required", file=sys.stderr)
    sys.exit(1)

omniagent.init(
    service="test-service",
    control_plane="http://localhost:8080",
    api_key=api_key,
    execute_url="http://localhost:8001/execute",
)

if __name__ == "__main__":
    uvicorn.run(app, host="localhost", port=8001)

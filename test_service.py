import uvicorn
from fastapi import FastAPI

import omniagent
from omniagent import ToolInput, ToolOutput, tool


class WeatherInput(ToolInput):
    city: str


class WeatherOutput(ToolOutput):
    temperature: str
    condition: str


class ClothingInput(ToolInput):
    temperature: str
    condition: str


class ClothingOutput(ToolOutput):
    recommendation: str


class UVInput(ToolInput):
    city: str


class UVOutput(ToolOutput):
    uv_index: int
    risk: str


@tool(description="Get the current weather for a city.")
async def get_weather(inp: WeatherInput) -> WeatherOutput:
    return WeatherOutput(temperature="22°C", condition="sunny")


@tool(description="Get clothing recommendation given temperature and weather condition from get_weather.")
async def get_clothing_recommendation(inp: ClothingInput) -> ClothingOutput:
    temp_val = int("".join(c for c in inp.temperature if c.isdigit() or c == "-") or "20")
    if temp_val < 10:
        rec = "heavy coat and scarf"
    elif temp_val < 20:
        rec = "light jacket"
    else:
        rec = "t-shirt and shorts"
    if inp.condition == "rainy":
        rec += ", bring an umbrella"
    return ClothingOutput(recommendation=rec)


@tool(description="Get the current UV index for a city.")
async def get_uv_index(inp: UVInput) -> UVOutput:
    return UVOutput(uv_index=8, risk="high")


app = FastAPI()
app.include_router(omniagent.router())

omniagent.init(
    service="test-service",
    control_plane="http://localhost:8080",
    api_key="b55d38cdc6574d7c1fad3ec7ab9af887bc0a3a075f739de27a8cfced9de5ee9a",
    execute_url="http://localhost:8001/execute",
)

if __name__ == "__main__":
    uvicorn.run(app, host="localhost", port=8001)

"""
TimesFM 2.0 forecasting service.

POST /forecast  — run time-series forecast using TimesFM 2.5-200M (CPU)
GET  /health    — liveness/readiness probe (returns ok only after model loaded)
"""

import logging
import os
from contextlib import asynccontextmanager
from typing import List, Optional

import numpy as np
import pandas as pd
import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from timesfm import ForecastConfig, TimesFM_2p5_200M_torch

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("timesfm-service")

# Model loaded once at startup, reused for all requests
_model: Optional[TimesFM_2p5_200M_torch] = None

MODEL_PATH = os.environ.get("MODEL_PATH", "/app/model_cache")


def load_model() -> TimesFM_2p5_200M_torch:
    logger.info("Loading TimesFM 2.5-200M model from %s (CPU)...", MODEL_PATH)
    model = TimesFM_2p5_200M_torch.from_pretrained(MODEL_PATH, torch_compile=False)
    logger.info("Compiling model...")
    # per_core_batch_size=1: single-input inference, avoids batch-padding complexity
    # max_context=512: matches timesfm-2.5-200m training context length
    model.compile(ForecastConfig(max_horizon=128, per_core_batch_size=1, max_context=512))
    logger.info("TimesFM model ready.")
    return model


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _model
    _model = load_model()
    yield


app = FastAPI(title="TimesFM Forecasting Service", version="2.0.0", lifespan=lifespan)


class ForecastRequest(BaseModel):
    context: List[float] = Field(..., description="Historical values in chronological order")
    horizon: int = Field(12, ge=1, le=128, description="Number of future periods to forecast")
    freq: str = Field("M", description="Time frequency: H (hourly), D (daily), W (weekly), M (monthly)")
    context_dates: Optional[List[str]] = Field(None, description="ISO date strings for context values")


class ForecastResponse(BaseModel):
    forecast: List[float]
    lower_80: List[float]
    upper_80: List[float]
    forecast_dates: Optional[List[str]] = None


@app.get("/health")
def health():
    if _model is None:
        return JSONResponse(status_code=503, content={"status": "loading"})
    return {"status": "ok"}


@app.post("/forecast", response_model=ForecastResponse)
def forecast(req: ForecastRequest):
    if _model is None:
        return JSONResponse(status_code=503, content={"error": "Model not loaded yet"})

    logger.info("Forecast: context_len=%d, horizon=%d, freq=%s", len(req.context), req.horizon, req.freq)

    context_array = np.array(req.context, dtype=np.float32)

    # forecast() returns (point_forecasts, quantile_forecasts)
    # point_forecasts: ndarray shape (batch, horizon)
    # quantile_forecasts: ndarray shape (batch, horizon, num_quantiles)
    point_forecasts, quantile_forecasts = _model.forecast(
        horizon=req.horizon,
        inputs=[context_array],
    )

    pf = point_forecasts[0][:req.horizon].tolist()
    qf = quantile_forecasts[0]  # shape (horizon, num_quantiles)

    # Default quantiles are [0.1, 0.2, ..., 0.9] — index 1=0.2, index 7=0.8
    num_q = qf.shape[-1] if qf.ndim > 1 else 0
    if num_q >= 8:
        lower_80 = qf[:req.horizon, 1].tolist()
        upper_80 = qf[:req.horizon, 7].tolist()
    else:
        lower_80 = pf
        upper_80 = pf

    # Generate forecast dates if context_dates provided
    forecast_dates = None
    if req.context_dates and len(req.context_dates) > 0:
        try:
            last_date = pd.Timestamp(req.context_dates[-1])
            freq_alias = {"M": "ME", "Q": "QE", "Y": "YE", "A": "YE"}.get(
                req.freq.upper(), req.freq.upper()
            )
            date_range = pd.date_range(start=last_date, periods=req.horizon + 1, freq=freq_alias)
            forecast_dates = [d.isoformat()[:10] for d in date_range[1:]]
        except Exception:
            pass

    return ForecastResponse(
        forecast=pf,
        lower_80=lower_80,
        upper_80=upper_80,
        forecast_dates=forecast_dates,
    )


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080)

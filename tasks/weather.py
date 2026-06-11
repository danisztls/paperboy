"""Weather task: Open-Meteo forecast posted as plain text, with smart-mode climate cache."""

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from config import get_weather_cfg
from pull.weather import WeatherSource, climate_cache_fresh, fetch_climate_normals
from tasks.context import RunContext
from tasks.delivery import deliver_text
from util import utc_now_iso

log = logging.getLogger(__name__)


async def process_weather_task(task_cfg: dict, state: dict, ctx: RunContext) -> dict:
    """Fetch Open-Meteo forecast, post as plain text. Returns {name: task_state} or {}."""
    name = task_cfg["name"]
    with ctx.capture_task(name, "weather"):
        weather_cfg = dict(get_weather_cfg(task_cfg))

        fresh_climate: dict | None = None
        if weather_cfg.get("kind") == "smart":
            tz = ZoneInfo(weather_cfg["timezone"])
            now_local = datetime.now(tz)
            cache = state.get("tasks", {}).get(name, {}).get("climate")
            if climate_cache_fresh(cache, now_local):
                weather_cfg["_climate_normals"] = cache
            else:
                fresh_climate = await fetch_climate_normals(weather_cfg, ctx.session)
                weather_cfg["_climate_normals"] = fresh_climate or cache

        result = await WeatherSource().pull(weather_cfg, set(), ctx.session)
        if result is None or not result.new_items:
            return {}

        if ctx.analysis:
            ctx.record_push(len(result.new_items))
            return {}

        if not await deliver_text(ctx, task_cfg, result.new_items, name):
            return {}

        ctx.record_push(len(result.new_items))
        task_state: dict = {"last_run": utc_now_iso()}
        if fresh_climate is not None:
            task_state["climate"] = fresh_climate
        return {name: task_state}

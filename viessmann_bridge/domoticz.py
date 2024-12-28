import asyncio
import base64
from datetime import date, datetime, timedelta
from math import floor

from viessmann_bridge.action import Action, DomoticzActionConfig
from viessmann_bridge.consumption import ConsumptionContext
from viessmann_bridge.logger import logger
import aiohttp

from viessmann_bridge.utils import gas_consumption_kwh_to_m3


class Domoticz(Action):
    config: DomoticzActionConfig

    def __init__(self, config: DomoticzActionConfig) -> None:
        self.config = config

    async def init(self) -> None:
        await self._configure_gas_entries()

    async def _configure_gas_entries(self) -> None:
        """
        If we want the ability for the historical values to be editable,
        we need to set AddDBLogEntry device option to true
        (see: https://wiki.domoticz.com/Domoticz_API/JSON_URL's#Note_on_counters)

        Note that those devices have to be 'Counter' type.
        """
        for device in (
            self.config.gas_consumption_kwh_idx,
            self.config.gas_consumption_m3_idx,
        ):
            if device is None:
                continue

            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self.config.domoticz_url}/json.htm",
                    params={"type": "devices", "rid": device}
                    if self.config.use_legacy_device_endpoint
                    else {"type": "command", "param": "getdevices", "rid": device},
                ) as response:
                    if response.status == 200:
                        device_state = await response.json()
                        logger.debug(f"Device state: {device_state}")
                    else:
                        logger.error(
                            f"Failed to request Domoticz {self.config.domoticz_url} when getting device status: {response.status}"
                        )

                # Now let's update the device to set
                # AddDBLogEntry to true

                async with session.get(
                    f"{self.config.domoticz_url}/json.htm",
                    params={
                        "type": "setused",
                        "idx": device,
                        "name": device_state["result"][0]["Name"],
                        "switchtype": device_state["result"][0]["SwitchTypeVal"],
                        "description": device_state["result"][0]["Description"],
                        "addjvalue": device_state["result"][0]["AddjValue"],
                        "addjvalue2": device_state["result"][0]["AddjValue2"],
                        "used": "true",
                        "options": base64.b64encode(
                            "AddDBLogEntry:true".encode()
                        ).decode(),
                    },
                ) as response:
                    logger.debug(response.request_info.real_url)
                    if response.status == 200:
                        logger.info(
                            f"Updated device {device} with AddDBLogEntry: {await response.text()}"
                        )
                    else:
                        logger.error(
                            f"Failed to request Domoticz {self.config.domoticz_url} when updating device: {response.status}"
                        )

    async def _request(self, params: dict) -> None:
        logger.debug(
            f"Requesting Domoticz {self.config.domoticz_url} with params {params}"
        )

        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{self.config.domoticz_url}/json.htm", params=params
            ) as response:
                logger.debug(response.request_info.real_url)
                if response.status == 200:
                    logger.debug(f"Response: {await response.text()}")
                else:
                    logger.error(
                        f"Failed to request Domoticz {self.config.domoticz_url}: {response.status}"
                    )

    def _consumption_to_m3(self, consumption: int) -> int:
        return floor(gas_consumption_kwh_to_m3(consumption) * 1000)

    async def update_current_total_consumption(
        self,
        consumption_context: ConsumptionContext,
        total_consumption: int,
    ) -> None:
        logger.debug(f"Updating current total consumption: {total_consumption}")

        if self.config.gas_consumption_kwh_idx is not None:
            await self._request(
                {
                    "type": "command",
                    "param": "udevice",
                    "idx": self.config.gas_consumption_kwh_idx,
                    "nvalue": 0,
                    "svalue": f"{str(total_consumption * 1000)}",
                }
            )

        logger.debug(f"Updated current total consumption: {total_consumption}")

    async def update_daily_consumption_stats(
        self, consumption_context: ConsumptionContext, consumption: dict[date, int]
    ):
        logger.debug(f"Updating daily consumption stats: {consumption}")

        # Sort the consumption by date ascending
        consumption = dict(sorted(consumption.items()))

        for day, value in consumption.items():
            consumption_after_this_day = sum(
                [v for d, v in consumption.items() if d >= day]
            )

            total_consumption_on_that_day = (
                consumption_context.total_consumption - consumption_after_this_day
            )

            if self.config.gas_consumption_kwh_idx is not None:
                await self._request(
                    {
                        "type": "command",
                        "param": "udevice",
                        "idx": self.config.gas_consumption_kwh_idx,
                        "nvalue": 0,
                        "svalue": f"{str(total_consumption_on_that_day * 1000)};{value * 1000};{day.strftime('%Y-%m-%d')}",
                    }
                )

                time = f"{day.strftime('%Y-%m-%d')} 00:00:00"

                await asyncio.sleep(2)

                await self._request(
                    {
                        "type": "command",
                        "param": "udevice",
                        "idx": self.config.gas_consumption_kwh_idx,
                        "nvalue": 0,
                        "svalue": f"{str(total_consumption_on_that_day * 1000)};{value * 1000 if day != date.today() else 0};{time}",
                    }
                )
                await asyncio.sleep(2)

        logger.debug(f"Updated daily consumption stats: {consumption}")

    async def handle_consumption_midnight_case(
        self,
        consumption_context: ConsumptionContext,
        previous_day_new_value: int,
        offset_previous_day: int,
        current_day_value: int,
        total_counter: int,
    ):
        logger.debug(f"""
Handling midnight case with the following values:
    previous_day_new_value: {previous_day_new_value},
    offset_previous_day: {offset_previous_day},
    current_day_value: {current_day_value},
    total_counter: {total_counter}
""")
        assert consumption_context.previous_consumption_date is not None

        await self.update_current_total_consumption(consumption_context, total_counter)
        # Convert the array of daily values to a dictionary with dates
        # The day_readat is the date of the last value in the array
        # The next values are for previous days (day_readat - 1, day_readat - 2, etc.)
        assert consumption_context.gas_consumption is not None

        daily_values = {
            consumption_context.gas_consumption.day_readat.date()
            - timedelta(days=i): consumption_context.gas_consumption.day[i]
            for i in range(len(consumption_context.gas_consumption.day))
        }

        logger.debug(f"Daily values: {daily_values}")

        await self.update_daily_consumption_stats(consumption_context, daily_values)

        logger.debug("Handled midnight case")

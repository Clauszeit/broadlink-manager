"""Button entities for learned Broadlink commands.

Each named command becomes a stateless button on a logical device. The
Broadlink code store stays owned by the core integration and is read only here.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
import json
import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval

from .codes import async_get_inventory
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)
REFRESH_INTERVAL = timedelta(seconds=30)


def _device_key(mac: str, device: str) -> str:
    """Identify a command group within one physical Broadlink transmitter."""
    return json.dumps([mac, device], ensure_ascii=False, separators=(",", ":"))


def _button_key(mac: str, device: str, command: str) -> str:
    """Use the stored command keys as the canonical identity."""
    return json.dumps([mac, device, command], ensure_ascii=False, separators=(",", ":"))


class BroadlinkCommandButton(ButtonEntity):
    """Press one stored command through its Broadlink remote entity."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self,
        mac: str,
        device: str,
        command: str,
        remote_entity_id: str,
        via_device_id: str | None,
    ) -> None:
        self._attr_unique_id = _button_key(mac, device, command)
        self._attr_name = command
        self._remote_entity_id = remote_entity_id
        self._device = device
        self._command = command
        info = DeviceInfo(
            identifiers={(DOMAIN, _device_key(mac, device))},
            name=device,
            model="IR/RF command device",
        )
        if via_device_id:
            info["via_device_id"] = via_device_id
        self._attr_device_info = info

    @property
    def available(self) -> bool:
        """A learned command is available while its transmitter is online."""
        state = self.hass.states.get(self._remote_entity_id)
        return state is not None and state.state not in ("unknown", "unavailable")

    def update_remote(self, remote_entity_id: str) -> None:
        """Follow a renamed Broadlink remote entity without changing identity."""
        self._remote_entity_id = remote_entity_id
        if hasattr(self, "hass") and self.entity_id:
            self.async_write_ha_state()

    async def async_press(self) -> None:
        """Send the named command using Home Assistant's remote service."""
        if not self.available:
            raise HomeAssistantError("Broadlink remote is unavailable")
        await self.hass.services.async_call(
            "remote",
            "send_command",
            {
                "entity_id": self._remote_entity_id,
                "device": self._device,
                "command": self._command,
            },
            blocking=True,
        )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create buttons and keep them in sync with learned commands."""
    buttons: dict[str, BroadlinkCommandButton] = {}
    refresh_lock = asyncio.Lock()

    async def refresh(_now: object = None) -> None:
        async with refresh_lock:
            try:
                inventory = await async_get_inventory(hass)
            except Exception:
                _LOGGER.exception("Could not refresh Broadlink command buttons")
                return

            found: set[str] = set()
            devices: set[str] = set()
            added: list[BroadlinkCommandButton] = []
            entity_registry = er.async_get(hass)
            for remote in inventory:
                if remote.mac is None:
                    continue
                remote_entry = entity_registry.async_get(remote.entity_id)
                for device, commands in remote.devices.items():
                    if not commands:
                        continue
                    devices.add(_device_key(remote.mac, device))
                    for command in commands:
                        key = _button_key(remote.mac, device, command)
                        found.add(key)
                        if button := buttons.get(key):
                            button.update_remote(remote.entity_id)
                        else:
                            button = BroadlinkCommandButton(
                                remote.mac,
                                device,
                                command,
                                remote.entity_id,
                                remote_entry.device_id if remote_entry else None,
                            )
                            buttons[key] = button
                            added.append(button)

            if added:
                async_add_entities(added)

            for key in buttons.keys() - found:
                button = buttons.pop(key)
                if button.entity_id and hass.states.get(button.entity_id):
                    await button.async_remove()
                if button.entity_id and entity_registry.async_get(button.entity_id):
                    entity_registry.async_remove(button.entity_id)

            for registered in er.async_entries_for_config_entry(entity_registry, entry.entry_id):
                if (registered.domain == "button" and registered.platform == DOMAIN
                        and registered.unique_id not in found):
                    entity_registry.async_remove(registered.entity_id)

            device_registry = dr.async_get(hass)
            for registered in dr.async_entries_for_config_entry(device_registry, entry.entry_id):
                if any(domain == DOMAIN and identifier not in devices
                       for domain, identifier in registered.identifiers):
                    device_registry.async_remove_device(registered.id)

    await refresh()
    entry.async_on_unload(async_track_time_interval(hass, refresh, REFRESH_INTERVAL))

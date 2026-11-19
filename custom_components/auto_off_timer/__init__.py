import logging
from datetime import datetime, timedelta, timezone

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_DOMAIN,
    ATTR_ENTITY_ID,
    ATTR_SERVICE,
    ATTR_SERVICE_DATA,
    EVENT_CALL_SERVICE,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
)
from homeassistant.core import Context, HomeAssistant, ServiceCall, callback
from homeassistant.helpers.event import async_call_later, async_track_state_change_event
from homeassistant.helpers.service import async_extract_entity_ids
from homeassistant.helpers.storage import Store
from homeassistant.helpers.template import Template

from .const import (
    CONF_ENABLE_TEMPLATE,
    CONF_ENTITIES,
    CONF_RESTART_ON_TURN_ON,
    CONF_TIMEOUT,
    DOMAIN,
    EVENT_AUTO_OFF_TIMER_TURN_OFF,
    STORAGE_KEY_TEMPLATE,
    STORAGE_VERSION,
)

_LOGGER = logging.getLogger(__name__)


@callback
def _is_turn_on_service(event_data) -> bool:
    """Return True if the service call is turn_on."""

    return event_data.get(ATTR_SERVICE) == SERVICE_TURN_ON


class AutoOffTimerManager:
    """Manages auto-off timers for entities."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry):
        self.hass = hass
        self.entry = entry
        self.store = Store(
            hass,
            STORAGE_VERSION,
            STORAGE_KEY_TEMPLATE.format(entry_id=entry.entry_id),
        )
        self.unsubs = {}
        self.timers = {}
        self.expiry_map = {}
        self.reload_unsub = None
        self.service_call_unsub = None
        self.entities = set()
        self.restart_on_turn_on = False

    async def async_setup(self) -> bool:
        """Set up the timer manager."""

        # Load existing expiry data
        store_data = await self.store.async_load() or {}
        self.expiry_map = store_data.get("expiry_timestamps", {}).copy()

        # Restore existing timers
        await self._restore_timers()

        # Register update listener
        self.reload_unsub = self.entry.add_update_listener(_update_listener)

        # Track state changes for configured entities
        self.entities = {
            entity_id.lower()
            for entity_id in self.entry.options.get(CONF_ENTITIES, [])
        }
        self.restart_on_turn_on = bool(
            self.entry.options.get(CONF_RESTART_ON_TURN_ON, False)
        )
        for entity in self.entities:
            self.unsubs[entity] = async_track_state_change_event(
                self.hass, entity, self._state_change_handler
            )

        if self.restart_on_turn_on:
            self.service_call_unsub = self.hass.bus.async_listen(
                EVENT_CALL_SERVICE,
                self._handle_turn_on_service,
                event_filter=_is_turn_on_service,
            )

        _LOGGER.info(
            "Auto Off Timer: entry %s configured for %s with timeout %s "
            "(restart_on_turn_on=%s)",
            self.entry.entry_id,
            sorted(self.entities),
            self.entry.options.get(CONF_TIMEOUT),
            self.restart_on_turn_on,
        )
        return True

    async def async_unload(self) -> bool:
        """Unload the timer manager."""

        # Unsubscribe from state changes
        for unsub in self.unsubs.values():
            unsub()
        self.unsubs.clear()

        # Cancel all active timers
        for handle in self.timers.values():
            handle()
        self.timers.clear()

        if self.service_call_unsub:
            self.service_call_unsub()
            self.service_call_unsub = None

        # Remove update listener
        if self.reload_unsub:
            self.reload_unsub()

        # Clean up store data
        await self.store.async_remove()

        return True

    async def _restore_timers(self) -> None:
        """Restore existing timers from stored data."""

        now = datetime.now(timezone.utc)

        for entity_id, expiry_iso in self.expiry_map.items():
            try:
                expiry = datetime.fromisoformat(expiry_iso)
                # Create timer even if expired to ensure state consistency
                delay = max((expiry - now).total_seconds(), 1)
                self.timers[entity_id] = async_call_later(
                    self.hass,
                    delay,
                    lambda _, eid=entity_id: self.hass.create_task(
                        self._on_timeout(eid)
                    ),
                )
                _LOGGER.info("Restoring timer for %s in %s seconds", entity_id, delay)
            except Exception as exc:
                _LOGGER.error("Failed to restore timer for %s: %s", entity_id, exc)
                self._remove_expiry_map_entry(entity_id)

        # Save cleaned expiry map
        await self._save_expiry_map()

    async def _save_expiry_map(self) -> None:
        """Save expiry map to store."""

        await self.store.async_save({"expiry_timestamps": self.expiry_map})

    def _remove_expiry_map_entry(self, entity_id: str) -> None:
        """Remove entity from expiry map."""

        self.expiry_map.pop(entity_id, None)

    def _update_expiry_map_entry(self, entity_id: str, expiry_iso: str) -> None:
        """Update entity expiry timestamp."""

        self.expiry_map[entity_id] = expiry_iso

    async def _on_timeout(self, entity_id: str) -> None:
        """Handle timeout expiration."""

        domain = entity_id.split(".")[0]
        _LOGGER.debug("Timeout expired for %s; calling %s.turn_off", entity_id, domain)

        context = Context()

        self.hass.bus.async_fire(
            EVENT_AUTO_OFF_TIMER_TURN_OFF,
            {
                ATTR_ENTITY_ID: entity_id,
            },
            context=context,
        )

        try:
            await self.hass.services.async_call(
                domain,
                SERVICE_TURN_OFF,
                {"entity_id": entity_id},
                blocking=True,
                context=context,
            )
        except Exception as exc:
            _LOGGER.error("Failed to turn off %s: %s", entity_id, exc)
        finally:
            self.timers.pop(entity_id, None)
            self._remove_expiry_map_entry(entity_id)
            await self._save_expiry_map()

    def _schedule_timeout(self, entity_id: str) -> None:
        """Schedule or renew the off-timer for an entity."""

        # Cancel existing timer
        if entity_id in self.timers:
            handle = self.timers.pop(entity_id)
            handle()

        timeout_seconds = self.entry.options.get(CONF_TIMEOUT)
        expiry = datetime.now(timezone.utc) + timedelta(seconds=timeout_seconds)

        self._update_expiry_map_entry(entity_id, expiry.isoformat())
        self.hass.create_task(self._save_expiry_map())

        handle = async_call_later(
            self.hass,
            timeout_seconds,
            lambda _: self.hass.create_task(self._on_timeout(entity_id)),
        )
        self.timers[entity_id] = handle

        _LOGGER.debug(
            "Timer scheduled for %s in %s seconds", entity_id, timeout_seconds
        )

    async def _handle_turn_on_service(self, event) -> None:
        """Restart the timer when turn_on targets an entity that is already on."""

        service_data = event.data.get(ATTR_SERVICE_DATA) or {}
        domain = event.data.get(ATTR_DOMAIN)
        service = event.data.get(ATTR_SERVICE)
        if not domain or service != SERVICE_TURN_ON:
            return

        try:
            service_call = ServiceCall(
                self.hass,
                domain,
                service,
                service_data,
                event.context,
            )
            entity_ids = {
                entity_id.lower()
                for entity_id in await async_extract_entity_ids(service_call)
            }
        except Exception as exc:
            _LOGGER.error("Failed to resolve turn_on targets: %s", exc)
            return

        for entity_id in entity_ids:
            if entity_id not in self.entities:
                continue

            state = self.hass.states.get(entity_id)
            if state is None or state.state != STATE_ON:
                continue

            if not self._should_enable_for_entity(entity_id):
                _LOGGER.debug(
                    "turn_on for %s ignored, condition not met", entity_id
                )
                continue

            _LOGGER.debug(
                "Restarting timer for %s after turn_on while already on",
                entity_id,
            )
            self._schedule_timeout(entity_id)

    def _cancel_timeout(self, entity_id: str) -> None:
        """Cancel the off-timer for an entity."""

        if entity_id in self.timers:
            handle = self.timers.pop(entity_id)
            handle()
            self._remove_expiry_map_entry(entity_id)
            self.hass.create_task(self._save_expiry_map())
            _LOGGER.debug("Timer canceled for %s", entity_id)

    def _should_enable_for_entity(self, entity_id: str) -> bool:
        """Check if timer should be enabled for entity based on template."""

        template = self.entry.options.get(CONF_ENABLE_TEMPLATE)
        if not template:
            return True

        try:
            rendered_template = Template(template, self.hass).async_render()
            return str(rendered_template).strip().lower() in ("1", "true", "yes", "on")
        except Exception as exc:
            _LOGGER.error("Template evaluation failed for %s: %s", entity_id, exc)
            return False

    @callback
    def _state_change_handler(self, event) -> None:
        """Handle state change events for entities."""

        entity_id = event.data.get("entity_id")
        old_state = event.data.get("old_state")
        new_state = event.data.get("new_state")

        if new_state is None:
            return

        _LOGGER.debug(
            "State change for %s, was %s now %s",
            entity_id,
            old_state.state.upper() if old_state else "None",
            new_state.state.upper(),
        )

        if new_state.state == STATE_ON:
            if not self._should_enable_for_entity(entity_id):
                _LOGGER.info("Entity %s turned ON, but condition not met", entity_id)
                return
            self._schedule_timeout(entity_id)
        elif new_state.state == STATE_OFF or new_state.state == STATE_UNAVAILABLE:
            self._cancel_timeout(entity_id)


async def _update_listener(hass: HomeAssistant, entry: ConfigEntry):
    """Called when options are updated; reloads the config entry."""

    await hass.config_entries.async_reload(entry.entry_id)


async def async_setup(hass: HomeAssistant, config: dict):
    """Initialize the integration data store."""

    hass.data.setdefault(DOMAIN, {})

    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Called when the user creates/activates a config entry via the UI (config_flow)."""

    _LOGGER.info("Setting up Auto Off Timer for entry %s", entry.entry_id)

    manager = AutoOffTimerManager(hass, entry)
    success = await manager.async_setup()

    if success:
        hass.data[DOMAIN][entry.entry_id] = {"manager": manager}

    return success


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Unload a config entry, removing listeners and timers."""

    if entry.entry_id not in hass.data[DOMAIN]:
        return True

    entry_data = hass.data[DOMAIN].pop(entry.entry_id)
    manager = entry_data.get("manager")

    if manager:
        success = await manager.async_unload()
    else:
        success = True

    _LOGGER.info("Auto Off Timer: entry %s unloaded", entry.entry_id)
    return success

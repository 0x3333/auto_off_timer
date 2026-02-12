"""Logbook support for Auto Off Timer."""

from homeassistant.components.logbook import (
    LOGBOOK_ENTRY_ENTITY_ID,
    LOGBOOK_ENTRY_MESSAGE,
    LOGBOOK_ENTRY_NAME,
)
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import Event, HomeAssistant, callback

from .const import DOMAIN, EVENT_AUTO_OFF_TIMER_TURN_OFF


@callback
def async_describe_events(hass: HomeAssistant, async_describe_event):
    """Describe logbook events."""

    @callback
    def async_describe_logbook_event(event: Event):
        """Describe a logbook event."""
        data = event.data
        entity_id = data.get(ATTR_ENTITY_ID)

        if not entity_id:
            return None

        state = hass.states.get(entity_id)
        friendly_name = state.attributes.get("friendly_name") if state else entity_id

        return {
            LOGBOOK_ENTRY_NAME: "Auto Off Timer",
            LOGBOOK_ENTRY_MESSAGE: f"turned off {friendly_name}",
            LOGBOOK_ENTRY_ENTITY_ID: entity_id,
        }

    async_describe_event(
        DOMAIN, EVENT_AUTO_OFF_TIMER_TURN_OFF, async_describe_logbook_event
    )

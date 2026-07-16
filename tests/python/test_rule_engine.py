import pytest

from asyncio import sleep as async_sleep

from app.kasa_device_manager import KasaDeviceManager
from app.rule_engine import (
    Action,
    AsyncCallableAction,
    CallableAction,
    Condition,
    Device,
    Geofence,
    Rule,
    RuleEngine,
    SimulatedSwitchDevice,
)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_create_kasa_device():
    """Real Kasa on LAN; requires a switch aliased ``Basement lamp``."""
    kdm = KasaDeviceManager()
    await kdm.fetch()
    name = "Basement lamp"
    assert kdm.get_device_by_alias(name) is not None
    await kdm.turn_off(name)
    await async_sleep(2)
    await kdm.turn_on(name)


def test_geofence_is_inside():
    device_hcma = Device("hcma")
    device_hcma.setLocation(41.194085, -73.888365)
    close_to_the_house = Geofence("250 m from the house", 41.1940720, -73.8883254, 250)
    assert close_to_the_house.is_inside({device_hcma})
    device_kristen = Device("kristen")
    device_kristen.setLocation(44.417597, -72.023842)
    assert not close_to_the_house.is_inside({device_kristen})


@pytest.mark.asyncio
async def test_rule_evaluation():
    engine = RuleEngine()
    assert engine is not None
    device_hcma = Device("hcma")
    device_hcma.setLocation(41.194085, -73.888365)
    device_kristen = Device("kristen")
    device_kristen.setLocation(44.417597, -72.023842)
    close_to_the_house = Geofence("250 m from the house", 41.1940720, -73.8883254, 250)
    assert Condition(lambda: close_to_the_house.is_inside({device_hcma})).is_true()
    assert not Condition(lambda: close_to_the_house.is_inside({device_kristen})).is_true()
    condition = Condition(lambda: close_to_the_house.is_inside({device_hcma, device_kristen}))
    assert not condition.is_true()
    kitchen_async = SimulatedSwitchDevice("kitchen_light_async")
    async_action = AsyncCallableAction(kitchen_async, SimulatedSwitchDevice.turn_on)
    assert isinstance(async_action, Action)
    await async_action.run()
    assert kitchen_async.is_on

    kitchen_sync = SimulatedSwitchDevice("kitchen_light_sync")
    sync_action = CallableAction(kitchen_sync, lambda d: d.set_power(True))
    assert isinstance(sync_action, Action)
    sync_action.run()
    assert kitchen_sync.is_on
    # rule = Rule(condition, True, action)
    # rule.evaluate()

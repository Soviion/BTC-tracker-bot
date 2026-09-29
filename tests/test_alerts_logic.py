from __future__ import annotations

from bot.alerts import direction_for, evaluate, is_hit
from bot.db import Alert


def make_alert(id: int = 1, target: float = 90000.0, direction: str = "above", armed: bool = True) -> Alert:
    return Alert(
        id=id, user_id=1, target=target, direction=direction, repeat=True, armed=armed, active=True,
        created_at=0, created_price=83000.0, triggered_at=None, triggered_price=None, trigger_count=0,
    )


def test_direction_for_above_below_and_equal() -> None:
    assert direction_for(90000, 83000) == "above"
    assert direction_for(70000, 83000) == "below"
    assert direction_for(83000, 83000) is None
    assert direction_for(83000 * 1.00005, 83000) is None  # в пределах эпсилон — тот же уровень


def test_is_hit() -> None:
    assert is_hit("above", 90000, 90000) is True
    assert is_hit("above", 90000, 90001) is True
    assert is_hit("above", 90000, 89999) is False
    assert is_hit("below", 70000, 70000) is True
    assert is_hit("below", 70000, 69999) is True
    assert is_hit("below", 70000, 70001) is False


def test_evaluate_triggers_armed_alert_on_cross() -> None:
    alert = make_alert(target=90000, direction="above", armed=True)
    triggered, rearm = evaluate([alert], 91000)
    assert triggered == [alert]
    assert rearm == []


def test_evaluate_does_not_retrigger_disarmed_alert() -> None:
    alert = make_alert(target=90000, direction="above", armed=False)
    triggered, rearm = evaluate([alert], 91000)
    assert triggered == []
    assert rearm == []  # цена всё ещё за уровнем — рано «перезаряжать»


def test_evaluate_rearms_after_price_returns_past_hysteresis() -> None:
    # 0.25% запас: 90000 * (1 - 0.0025) = 89775
    alert = make_alert(id=7, target=90000, direction="above", armed=False)
    triggered, rearm = evaluate([alert], 89900)  # ещё внутри запаса (89900 > 89775)
    assert rearm == []
    triggered, rearm = evaluate([alert], 89700)  # уже дальше запаса
    assert triggered == []
    assert rearm == [7]


def test_evaluate_below_direction_symmetric() -> None:
    alert = make_alert(id=3, target=70000, direction="below", armed=False)
    triggered, rearm = evaluate([alert], 70100)  # 70000*1.0025 = 70175, ещё внутри запаса
    assert rearm == []
    triggered, rearm = evaluate([alert], 70200)  # уже за запасом
    assert rearm == [3]

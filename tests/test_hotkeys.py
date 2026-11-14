"""Tests for hotkey spec parsing and Ctrl+Win order-independent matching."""

import pytest

from whisper_lm.hotkeys import (
    is_modifier_only_spec,
    matches_hotkey_press,
    parse_hotkey,
    spec_is_still_held,
)


def test_parse_hotkey_single_and_combo() -> None:
    assert parse_hotkey("") is None
    hold = parse_hotkey("right_ctrl")
    assert hold == ([], {0xA3})

    combo = parse_hotkey("ctrl+shift+space")
    assert combo is not None
    mods, trig = combo
    assert len(mods) == 2
    assert 0x11 in mods[0]
    assert 0x10 in mods[1]
    assert trig == {0x20}


def test_parse_hotkey_rejects_unknown_token() -> None:
    with pytest.raises(ValueError):
        parse_hotkey("ctrl+nonexistent_key")


def test_ctrl_win_matches_regardless_of_press_order() -> None:
    spec = parse_hotkey("ctrl+win")
    assert is_modifier_only_spec(spec)

    # Press Left Ctrl (0xA2) then Left Win (0x5B)
    pressed = {0xA2}
    assert not matches_hotkey_press(spec, pressed, 0xA2)
    pressed.add(0x5B)
    assert matches_hotkey_press(spec, pressed, 0x5B)

    # Press Right Win (0x5C) first then Left Ctrl (0xA2)
    pressed2 = {0x5C}
    assert not matches_hotkey_press(spec, pressed2, 0x5C)
    pressed2.add(0xA2)
    assert matches_hotkey_press(spec, pressed2, 0xA2)

    # Releasing either Ctrl or Win stops the hold
    pressed2.discard(0xA2)
    assert not spec_is_still_held(spec, pressed2, 0xA2)


def test_holding_ctrl_and_double_tapping_win_retriggers_hold_cleanly() -> None:
    spec = parse_hotkey("ctrl+win")
    pressed = {0xA2}  # Ctrl held continuously

    # Tap 1 Win down/up
    pressed.add(0x5B)
    assert matches_hotkey_press(spec, pressed, 0x5B)
    pressed.discard(0x5B)
    assert not spec_is_still_held(spec, pressed, 0x5B)

    # Tap 2 Win down/up while Ctrl stays held
    pressed.add(0x5B)
    assert matches_hotkey_press(spec, pressed, 0x5B)


def test_non_modifier_combo_requires_trigger_last() -> None:
    spec = parse_hotkey("ctrl+shift+space")
    assert not is_modifier_only_spec(spec)

    # Holding space + shift then pressing ctrl must not trigger
    pressed = {0x20, 0xA0, 0xA2}
    assert not matches_hotkey_press(spec, pressed, 0xA2)
    # Holding ctrl + shift then pressing space triggers
    assert matches_hotkey_press(spec, pressed, 0x20)

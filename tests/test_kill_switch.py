import json
import os

import pytest

from app.risk.kill_switch import KillSwitch


@pytest.fixture()
def state_path(tmp_path):
    return str(tmp_path / "kill_switch_state.json")


def test_defaults_to_inactive(state_path):
    ks = KillSwitch(state_path)
    assert ks.is_active() is False


def test_activate_sets_active_with_reason(state_path):
    ks = KillSwitch(state_path)
    state = ks.activate("manual halt", activated_by="alice", close_positions=True)
    assert state.active is True
    assert state.reason == "manual halt"
    assert state.activated_by == "alice"
    assert state.close_positions is True
    assert ks.is_active() is True


def test_state_persists_across_new_instances_same_file(state_path):
    ks1 = KillSwitch(state_path)
    ks1.activate("halt for maintenance", activated_by="ops")
    ks2 = KillSwitch(state_path)  # simulates a process restart
    assert ks2.is_active() is True
    assert ks2.status().reason == "halt for maintenance"


def test_deactivate_requires_named_actor_and_clears_active(state_path):
    ks = KillSwitch(state_path)
    ks.activate("halt", activated_by="ops")
    state = ks.deactivate(deactivated_by="bob")
    assert state.active is False
    assert state.deactivated_by == "bob"
    assert ks.is_active() is False


def test_deactivate_preserves_activation_history(state_path):
    ks = KillSwitch(state_path)
    ks.activate("halt reason", activated_by="ops")
    state = ks.deactivate(deactivated_by="bob")
    assert state.reason == "halt reason"
    assert state.activated_by == "ops"


def test_state_file_is_valid_json_on_disk(state_path):
    ks = KillSwitch(state_path)
    ks.activate("halt", activated_by="ops")
    with open(state_path) as f:
        data = json.load(f)
    assert data["active"] is True
    assert data["activated_by"] == "ops"


def test_corrupted_state_file_recovers_to_inactive(state_path):
    os.makedirs(os.path.dirname(state_path), exist_ok=True)
    with open(state_path, "w") as f:
        f.write("not valid json{{{")
    ks = KillSwitch(state_path)
    assert ks.is_active() is False

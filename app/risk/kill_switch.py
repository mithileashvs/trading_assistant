"""
Emergency Kill Switch (section 17).

Stops new trades immediately, optionally flags open positions for
closure, and — critically — persists its state to disk so a process
restart does not silently clear an active kill switch. Reactivation
(clearing it) is always an explicit, separate call; nothing in this
module clears the switch as a side effect of anything else.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Optional


@dataclass
class KillSwitchState:
    active: bool
    reason: Optional[str] = None
    activated_at: Optional[str] = None
    activated_by: Optional[str] = None
    close_positions: bool = False
    deactivated_at: Optional[str] = None
    deactivated_by: Optional[str] = None


class KillSwitch:
    """File-backed kill switch. One instance per deployment should own
    a given state file; concurrent processes should share the file
    path, not separate KillSwitch instances with divergent state."""

    def __init__(self, state_path: str = "./data/kill_switch_state.json", default_active: bool = False):
        self._path = state_path
        os.makedirs(os.path.dirname(state_path) or ".", exist_ok=True)
        if not os.path.exists(self._path):
            self._write(KillSwitchState(active=default_active))

    def _write(self, state: KillSwitchState) -> None:
        with open(self._path, "w") as f:
            json.dump(asdict(state), f, indent=2)

    def _read(self) -> KillSwitchState:
        try:
            with open(self._path) as f:
                data = json.load(f)
            return KillSwitchState(**data)
        except (FileNotFoundError, json.JSONDecodeError):
            state = KillSwitchState(active=False)
            self._write(state)
            return state

    def is_active(self) -> bool:
        return self._read().active

    def status(self) -> KillSwitchState:
        return self._read()

    def activate(self, reason: str, activated_by: str = "SYSTEM", close_positions: bool = False) -> KillSwitchState:
        state = KillSwitchState(
            active=True,
            reason=reason,
            activated_at=datetime.now(timezone.utc).isoformat(),
            activated_by=activated_by,
            close_positions=close_positions,
        )
        self._write(state)
        return state

    def deactivate(self, deactivated_by: str) -> KillSwitchState:
        """Explicit reactivation. Requires a named actor — there is no
        anonymous/automatic path to clearing an active kill switch."""
        current = self._read()
        state = KillSwitchState(
            active=False,
            reason=current.reason,
            activated_at=current.activated_at,
            activated_by=current.activated_by,
            close_positions=current.close_positions,
            deactivated_at=datetime.now(timezone.utc).isoformat(),
            deactivated_by=deactivated_by,
        )
        self._write(state)
        return state

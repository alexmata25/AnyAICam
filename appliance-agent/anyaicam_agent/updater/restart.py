"""RDM-2 (device-side integration, Group 2B): the real restart_signal
implementation for UpdateStateMachine.

Preserves commands.py's existing restart_service command handler's exact
semantics (`if stop_event: stop_event.set()`) -- there is no new restart
mechanism here, no subprocess call, no service-manager/systemd call, and
no real process exit performed by this module or anything it calls.
Setting stop_event only signals intent; the agent's own main loop
(service.py's run()) is what actually notices the flag and exits
gracefully on its own next check -- exactly as it already does for the
existing restart_service command. Whatever restarts the process
afterward (a supervisor, systemd, a container orchestrator) is unchanged
by, and entirely outside the scope of, this module. restart_signal's
whole job, both here and in restart_service's handler, is only ever "ask
the current process to stop gracefully" -- never "make a new process
start."
"""

from threading import Event
from typing import Callable


def make_restart_signal(stop_event: Event) -> Callable[[], None]:
    """Returns a restart_signal callable for UpdateStateMachine that does
    exactly what commands.py's restart_service handler already does:
    stop_event.set(). No new mechanism -- this only exposes the existing
    one through the Callable[[], None] shape UpdateStateMachine's
    constructor expects, so it has a real (non-placeholder) restart_signal
    once Group 2A's _unwired_restart_signal placeholder is no longer
    appropriate.
    """
    return stop_event.set

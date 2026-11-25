"""Redis keys and channels for the live world (one place, so producer and consumer agree)."""

import os

# Pub/sub channels are global to the Redis server, not per database number, so tests on their
# own database still heard the dev stack's frames. They get a namespace: the tests set
# VAZHISYNC_PUBSUB_NAMESPACE (tests/conftest.py); everything else uses the default.
_PUBSUB = os.environ.get("VAZHISYNC_PUBSUB_NAMESPACE", "vazhisync")

LIVE_CHANNEL = f"{_PUBSUB}:live:frames"  # pub/sub: one LiveFrame JSON per runner tick
LAST_FRAME_KEY = "vazhisync:live:last"  # the latest frame, for clients that just connected
LAST_FRAME_TTL_SECONDS = 60
SPEED_KEY = "vazhisync:world:speed"  # 1, 5 or 10; set by officers, read by the runner
BUNCHING_CHANNEL = f"{_PUBSUB}:events:bunching"  # pub/sub: a BunchingRaised per new event
SCENARIO_KEY = "vazhisync:world:scenario"  # WorldScenario JSON; a new token resets the world
INSTRUCTION_CHANNEL = f"{_PUBSUB}:instructions"  # pub/sub: an InstructionSent per new instruction
WORLD_ACTIONS_KEY = "vazhisync:world:actions"  # list: acknowledged instructions the world applies
ALERT_CHANNEL = f"{_PUBSUB}:alerts"  # pub/sub: officer alerts (e.g. a driver can't comply)


# AG-UI run streams (decision 10): one Redis Stream of AG-UI events per workflow run, plus a
# hash with the latest RunView and the stream id it includes (attach = snapshot + tail).
def agui_stream_key(run_id: str) -> str:
    return f"vazhisync:agui:{run_id}"


def agui_view_key(run_id: str) -> str:
    return f"vazhisync:agui:{run_id}:view"


AGUI_TTL_SECONDS = 24 * 3600
AGUI_MAXLEN = 2000  # events per run (a run has a few dozen; resumes add more)

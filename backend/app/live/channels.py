"""Redis keys and channels for the live world (one place, so producer and consumer agree)."""

LIVE_CHANNEL = "vazhisync:live:frames"  # pub/sub: one LiveFrame JSON per runner tick
LAST_FRAME_KEY = "vazhisync:live:last"  # the latest frame, for clients that just connected
LAST_FRAME_TTL_SECONDS = 60
SPEED_KEY = "vazhisync:world:speed"  # 1, 5 or 10; set by officers, read by the runner
BUNCHING_CHANNEL = "vazhisync:events:bunching"  # pub/sub: a BunchingRaised per new event
SCENARIO_KEY = "vazhisync:world:scenario"  # WorldScenario JSON; a new token resets the world
INSTRUCTION_CHANNEL = "vazhisync:instructions"  # pub/sub: an InstructionSent per new instruction

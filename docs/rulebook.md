# VazhiSync — Operating rulebook

Hard operating rules. They are **code constraints** (`backend/app/agents/rulebook.py`), applied
to every candidate before it is simulated; no model can override them (root CLAUDE.md rule 13).
A candidate that breaks a rule is dropped and the rule id is recorded in `agent_steps`.

| Id | Rule | Where it is enforced |
|---|---|---|
| R1 | A hold is at most **180 s**. | Action Generator (rulebook) |
| R2 | At most **2 stops** are skipped in one instruction. | Action Generator (rulebook) |
| R3 | **Never skip** a hospital or major interchange stop. Route 32B: Stanley Hospital (both directions), Rajiv Gandhi Government General Hospital, M.G.R. Central railway station. Any stop whose name contains "Hospital" on any route. | Action Generator (rulebook) |
| R4 | **No short-turns on the day's last two trips** (within 2 scheduled headways of the direction's last departure). | Action Generator (rulebook) |
| R5 | **Cooldown:** no new instruction to a bus instructed in the last **10 minutes**. | Action Generator (rulebook) |
| R6 | **Never hold a bus that is already holding** (an active hold instruction). | Action Generator (rulebook) |
| R7 | **Reject a fix that creates new bunching:** a candidate whose simulated bunching count exceeds doing nothing's is not recommended. | Control Advisor (needs simulation results) |
| R8 | **Never instruct a stale bus** (GPS older than `STALE_GPS_SECONDS`). | Headway Monitor ends the run; harness send guard; `send_driver_instruction` (rule 4) |

Changing a rule means changing the code and this table together; `tests/unit/test_agents.py`
covers each rule.

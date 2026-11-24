# GTFS data

| File | In git? | Source |
|---|---|---|
| `chennai-unified-gtfs.zip` | no (gitignored, ~10 MB) | [ungalsoththu/ChennaiGTFS](https://github.com/ungalsoththu/ChennaiGTFS) — community-maintained unified Chennai feed (MTC, CMRL, Southern Railway), feed version 0.2.0 |
| `routes/<id>.json` | yes | Hand-written route config: which GTFS route ids form the two directions |
| `routes/<id>.dir<N>.geojson` | yes | Road-snapped route geometry, generated once by `python -m app.gtfs.snap_shape`; `properties.segments` holds each stop-to-stop stretch's road distance, OSRM free-flow duration, road/straight ratio, `geometry_source` and `flagged` |
| `routes/<id>.turns.geojson` | yes | 3–4 short-turn connections (a bus turning round to the opposite direction mid-route), routed with OSRM |
| `routes/<id>.waypoints.json` | optional | Via points that force a stretch onto a road: `{"directions": {"0": {"3": [[lon, lat]]}}}` = direction 0, stop 3 → 4 |

Download the feed (needed for import, not for tests/CI):

```
curl -fL -o backend/data/gtfs/chennai-unified-gtfs.zip \
  https://raw.githubusercontent.com/ungalsoththu/ChennaiGTFS/main/data/chennai-unified-gtfs.zip
```

## Route 32B

- Directions: `MTC-20581` (Korukkupet R.S → Vivekananda House, 21 stops) and `MTC-20582`
  (back, 22 stops). ~145 trips/day each way, ~5–6 min peak headway, 35/37 min run time.
- MTC also runs "CT" cut-trip variants of 32B (e.g. M.G.R. Central → Vivekananda House) with
  0–2 trips/day — real short-turns, not imported.
- The feed has **no shapes for any bus route** (only Metro), so the geometry is snapped with
  OSRM, one stop pair at a time (B6). A stretch whose road distance is more than 2.5× the
  straight-line distance is a car-routing artefact (one-way streets, U-turns); it is flagged
  and drawn straight at the same speed. On 32B only outbound stop 2 → 3 is flagged. Add a
  `waypoints.json` entry to force the real road, then re-run `snap_shape`.
- The per-segment durations are OSRM car free-flow times. The simulator uses only their
  *proportions* to spread the timetabled running time along the route.

## Untrusted data

The feed is third-party input and contains at least one deliberately hostile value (a
`service_id` with a `<script>` tag). The importer validates every row it uses (ids, lengths,
coordinates inside Chennai) and stores names as plain text; the API returns JSON and the
frontend must render these strings as text, never HTML.

## Licences and attribution

- Route geometry in `routes/*.geojson`: **Map data © OpenStreetMap contributors**, available
  under the [Open Database License (ODbL 1.0)](https://opendatacommons.org/licenses/odbl/1-0/),
  routed with [OSRM](https://project-osrm.org/). The API returns this attribution with any
  snapped route, and any map showing it must display it.
- GTFS feed: see the licence in the ChennaiGTFS repository before redistributing the zip.

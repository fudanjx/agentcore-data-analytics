---
name: ah-geo-map
description: Postal-code-to-district mapping rules and the Leaflet/OpenStreetMap map contract for AH dashboards. Read whenever the request is geographic, map-based, or postal-code-based.
---

# Geographic dashboards: postal mapping + map contract

Read `references/dashboard-design.md` first for the visual language. This file adds
the postal-code mapping rules and the interactive map component. Apply it whenever the
deliverable is geographic (map, catchment, "where do patients come from", by district,
by area, by postal code).

## 1. Postal columns

| Table | Postal column |
|---|---|
| `admission` | `postal_code` |
| `discharge` | `postal` **and** `postal_code` — profile both, use the one with higher non-null coverage |
| `outpatient` | `postal_code` |
| `procedure` | `postal_code` |
| `inflight`, `urgentcarecenter` | none — join to `admission`/`outpatient` on `pat_enc_csn_id` (then `case_no`) and report join coverage separately from postal coverage |

Confirm with `describe_table` before writing the query. Keep the table's standard
ontology filters (`prelim_flag`, `visit_type`, ward exclusions) in place.

## 2. Normalisation rule — non-negotiable

Source postal codes are truncated to the first 2 digits and zero-filled
(`312139 → 310000`), and leading zeros are lost when stored numerically
(`013456 → 010000 → 10000`). So:

1. Strip non-digits.
2. Length 5 → left-pad one `0` to 6 digits. Length 6 → keep.
3. Any other length → **unmapped** (do not guess).
4. Take the first 2 digits = postal sector.
5. Look up the sector in `references/postal-districts.json` (`prefix_to_district`,
   `prefix_to_area`, `districts[]` with `lat`/`lon`). Valid sectors are `01`–`82`
   except `74`; anything else is unmapped.

```python
import json
REF = json.load(open("references/postal-districts.json"))

def sector(raw):
    digits = "".join(c for c in str(raw or "") if c.isdigit())
    if len(digits) not in (5, 6):
        return None
    return digits.zfill(6)[:2]

df["sector"]  = df["postal_code"].map(sector)
df["district"] = df["sector"].map(REF["prefix_to_district"])
df["area"]     = df["sector"].map(REF["prefix_to_area"])
```

Equivalent in SQL (Trino/Athena) — `lpad` to 6 keeps short/invalid values in the
unmapped bucket rather than mislabelling them:

```sql
substr(lpad(regexp_replace(cast(postal_code as varchar), '[^0-9]', ''), 6, '0'), 1, 2) AS sector
```

## 3. Coverage gate — 95%

Mapped share of non-null postal records must be **≥ 95%**. Below that, stop and
diagnose before building anything; a low rate means the data structure is wrong,
not that the population is unusual.

Diagnostic order:
1. Histogram the digit-stripped lengths — expect only 5 and 6. Any other mode means
   the wrong column, a numeric cast, or upstream truncation.
2. Sentinels: `''`, `0`, `000000`, `999999`, `NA`, `NIL`, `-`.
3. Wrong column — for `discharge`, compare `postal` against `postal_code`.
4. Genuinely non-local patients (foreign address, no SG postal). These are legitimately
   unmapped; quantify them and state the number.
5. Sectors outside `01`–`82`, or `74`.

Report the mapped percentage as a KPI **and** in the data-quality notice. Never publish
a geographic dashboard without stating it.

## 4. One metrics object

Build one validated dict before any component, and let the map, KPIs, table and charts
all read from it. Never recompute a total per component.

```python
geo_metrics = {
    "total_records": ...,
    "postal_present": ...,
    "mapped_records": ...,
    "unmapped_records": ...,
    "mapped_pct": ...,        # mapped_records / postal_present * 100
    "district_count": ...,    # districts with at least 1 record
    "top_district": ...,
    "points": [...],          # [{district, area, lat, lon, value, share_pct}]
}
```

Assert before rendering:

```python
assert geo_metrics["postal_present"] == geo_metrics["mapped_records"] + geo_metrics["unmapped_records"]
assert sum(p["value"] for p in geo_metrics["points"]) == geo_metrics["mapped_records"]
assert len(geo_metrics["points"]) > 0
assert geo_metrics["mapped_pct"] >= 95, geo_metrics["mapped_pct"]
assert all(1.15 < p["lat"] < 1.50 and 103.6 < p["lon"] < 104.1 for p in geo_metrics["points"])
```

## 5. Map component — Leaflet + OpenStreetMap

The map is mandatory for a geographic dashboard, and it goes **inline in the dashboard
HTML** — Leaflet from CDN, OpenStreetMap tiles, points injected as a JSON array. Do not
generate map HTML in Python and embed it as an iframe, and never place map markup in a
component that strips `<script>`.

Place it in a `card wide` as the primary visual, directly under the KPI row, paired with
a ranked horizontal bar chart or top-10 table of districts in the row below.

```html
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>

<div class="card wide">
  <h2>Patient Origin by Postal District</h2>
  <div class="subtitle">Circle size = attendances; 28 Singapore postal districts, mapped
    on the first 2 digits of the postal code.</div>
  <div id="map" style="height:560px;min-height:500px;width:100%;border-radius:8px"></div>
</div>

<script>
const POINTS = /* geo_metrics["points"] as JSON */;

const map = L.map('map', { scrollWheelZoom: false }).setView([1.3521, 103.8198], 11);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
  maxZoom: 18
}).addTo(map);
map.on('click', () => map.scrollWheelZoom.enable());

const max = Math.max(...POINTS.map(p => p.value));
const RAMP = ['#d7e7f3', '#a6cbe3', '#6aa9cf', '#3a89bd', '#12304a'];
const shade = v => RAMP[Math.min(4, Math.floor((v / max) * 5))];
const radius = v => 6 + 26 * Math.sqrt(v / max);   // area-proportional

POINTS.forEach(p => {
  L.circleMarker([p.lat, p.lon], {
    radius: radius(p.value), fillColor: shade(p.value), fillOpacity: 0.75,
    color: '#12304a', weight: 1
  }).bindTooltip(
    `<strong>D${p.district} — ${p.area}</strong><br>` +
    `${p.value.toLocaleString()} (${p.share_pct.toFixed(1)}%)`,
    { sticky: true }
  ).addTo(map);
});

map.fitBounds(L.latLngBounds(POINTS.map(p => [p.lat, p.lon])).pad(0.15));
</script>
```

Rules:
- Container needs an explicit non-zero height before init — Leaflet fails silently in a
  hidden or zero-height box. If the map sits in a tab or collapsible, call
  `map.invalidateSize()` when it becomes visible.
- Sort points descending by value so large circles are drawn first and small ones stay
  clickable; draw order follows array order.
- Radius scales with `sqrt` (area-proportional), never linearly with value.
- Add a small static legend for the size/colour ramp, and a caption noting that circles
  sit at approximate district centres, not patient addresses.
- No API key, no basemap other than OpenStreetMap.

## 6. Final gate

Before returning the dashboard URL, confirm:

- [ ] Map present inline, container height > 0, `POINTS` non-empty and all inside Singapore.
- [ ] `mapped_pct` ≥ 95%, shown as a KPI and in the data-quality notice.
- [ ] Circle values sum to `mapped_records`; every displayed total traces to `geo_metrics`.
- [ ] Unmapped records counted and explained in the notice, not dropped silently.
- [ ] Ranked district table or bar chart present alongside the map.
- [ ] Footer states the mapping rule (2-digit sector), the centroid caveat, and the source table + period.

A failed check is a dashboard-generation failure. Diagnose and rebuild — do not publish
a partial or map-less geographic dashboard.

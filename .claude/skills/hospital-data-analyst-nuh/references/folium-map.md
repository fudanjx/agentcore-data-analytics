---
name: nuh-folium-map
description: Deterministic Folium interactive map rendering contract for NUH dashboards. Read whenever the dashboard includes a geographic or location-based map component.
---

# Folium map rendering contract

Read `references/dashboard.md` first for general dashboard rules. This file
controls interactive map generation, validation, embedding, and data consistency.

Apply this contract whenever the dashboard includes a geographic or mapped component.

## 1. Mandatory component

An interactive map is required for any dashboard containing geographic data.
Never publish the dashboard without a successfully generated and validated map.
Absence of the map is a dashboard-generation failure, not an acceptable partial result.

## 2. Generation sequence

Execute in this order. Never build dashboard components before the map exists.

```
DATA → DATA VALIDATION → MAP DATASET → FOLIUM MAP
→ MAP HTML → MAP VALIDATION → DASHBOARD COMPONENTS
→ DASHBOARD VALIDATION → PUBLISH
```

## 3. Deterministic rendering

Always render and save using this exact pattern:

```python
map_html = folium_map.get_root().render()

map_path = "/tmp/dashboard_map.html"
with open(map_path, "w", encoding="utf-8") as f:
    f.write(map_html)
```

Do not improvise different rendering approaches between runs.

## 4. Map validation

Run all assertions before proceeding to dashboard construction:

```python
assert map_html
assert len(map_html) > 3000
assert "leaflet" in map_html.lower()
assert "L.map(" in map_html
assert "<div" in map_html
```

Also verify:
- at least one valid lat/lon point in the dataset
- all latitude and longitude values are numeric and in range
- number of plotted entities is greater than zero
- no exceptions raised during Folium generation
- the saved file exists and has non-zero size

On failure: diagnose → regenerate → re-validate. Allow up to 2 retries.
If map generation still fails, return an explicit dashboard-generation error.
Do not silently publish without the map.

## 5. Embedding

Embed using the dashboard application's iframe or sandboxed HTML component.
Supply the complete `map_html` document as `srcdoc` if using an iframe.

Required layout:
- width: 100%
- height: 560–650 px
- min-height: 500 px

The map container must have a defined, non-zero height before the map
initialises. Leaflet silently fails inside hidden or zero-height containers.

Never inject Folium HTML into a Markdown component or any component that
strips `<script>` tags.

## 6. Session consistency

Generate the map, validate the HTML, and retrieve the artefact within the
**same** Code Interpreter session. Do not split these steps across sessions.

If the artefact must survive beyond the session, write it to durable storage
before the session ends.

## 7. Single metrics object

Create one validated metrics dict before building any dashboard component.
All KPI cards, charts, tables, and map bubbles must consume this dict —
never recalculate independently.

```python
dashboard_metrics = {
    "total_attendance": ...,
    "mapped_attendance": ...,
    "unmapped_attendance": ...,
    "mapped_percentage": ...,
    "district_count": ...
}
```

Required reconciliation checks:

```
total_attendance == mapped_attendance + unmapped_attendance
map_value_total == mapped_attendance
number_of_map_points > 0
```

## 8. Final gate

Before returning the dashboard URL, verify all of the following:

- dashboard title present
- KPI cards present
- map component present
- map HTML passed all validation assertions
- map container height > 0
- ranking table or summary table present
- all dashboard totals reconcile with `dashboard_metrics`
- map totals reconcile with `dashboard_metrics`

Any failed check is a dashboard-generation failure. Do not return the URL
until all checks pass.

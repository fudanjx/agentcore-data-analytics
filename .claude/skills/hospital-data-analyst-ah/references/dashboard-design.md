---
name: executive-dashboard-design
description: Use this skill whenever designing, generating, reviewing, or refining an HTML analytics dashboard, KPI dashboard, management dashboard, operational dashboard, healthcare dashboard, performance dashboard, reporting page, or dashboard-style HTML. The required visual language is a clean enterprise style with a navy-to-blue header, white KPI and chart cards on a very light blue-grey background, restrained colour usage, strong information hierarchy, compact explanatory text, Chart.js-style charts, and responsive layouts. Prefer clarity, credibility, and executive readability over decorative design.
---

# Executive Dashboard Design Skill

## Purpose

Design dashboards in a consistent enterprise analytics style based on the user's preferred reference dashboard.

The target look is:

- clean and professional
- executive-friendly
- data-dense without feeling crowded
- restrained rather than decorative
- suitable for healthcare, operations, finance, management, and corporate analytics
- immediately readable on desktop while remaining responsive on smaller screens

The dashboard should feel like a polished internal management product, not a marketing website.

---

# 1. Core Visual Identity

## Colour palette

Use these colours as the default design tokens:

```css
:root {
  --navy: #12304a;
  --blue: #1f78b4;
  --teal: #1b9e77;
  --orange: #e67e22;
  --red: #c0392b;
  --purple: #8064a2;

  --bg: #f4f7fb;
  --card: #ffffff;
  --text: #172b3a;
  --muted: #667085;
  --border: #e5e7eb;
}
```

### Colour hierarchy

Use colours semantically and sparingly.

- **Navy**: primary headings, KPI values, key structural text.
- **Blue**: default primary data series and default insight accent.
- **Teal**: positive, throughput, completion, or secondary comparison series.
- **Orange**: pressure, utilisation, admission, warning, or secondary metric.
- **Red**: risk, adverse outcome, re-attendance, breach, or deterioration.
- **Purple**: secondary analytical series or supporting category.
- **Muted grey**: labels, subtitles, explanatory text, footnotes.

Do not introduce many additional colours unless the data genuinely requires categorical distinction.

Avoid neon colours, highly saturated palettes, rainbow dashboards, or decorative gradients inside chart cards.

---

# 2. Page Structure

Use this default hierarchy:

1. full-width dashboard header
2. optional data-quality / methodology notice
3. KPI summary row
4. analytical chart grid
5. management insights section
6. definitions, methodology, and limitations footer

The dashboard should communicate in this order:

**What is happening → how it is changing → why it matters → what the limitations are.**

---

# 3. Header

Use a strong but restrained enterprise header.

```css
header {
  background: linear-gradient(135deg, #12304a, #1f5f86);
  color: white;
  padding: 28px 4%;
}
```

## Header title

- large but not oversized
- bold
- concise
- preferably one line on desktop
- default size approximately `24px–38px` using responsive sizing

Example:

> Alexandra Hospital UCC Performance Dashboard

## Header subtitle

Use a short contextual line containing information such as:

- reporting period
- business unit
- source system
- data refresh date

Keep it visually secondary.

Example:

> Five-year operational view: June 2021–May 2026 | Source: AH S3 Tables

Do not place multiple badges, decorative icons, navigation tabs, or unnecessary controls inside the header unless the application genuinely needs them.

---

# 4. Main Content Container

Default desktop layout:

```css
main {
  width: 92%;
  max-width: 1500px;
  margin: 24px auto 50px;
}
```

The design should have generous outer margins while allowing charts to use most of the available width.

Avoid very narrow centred layouts intended for articles or marketing pages.

---

# 5. Data-Quality / Methodology Notice

When the dashboard includes important caveats, place them near the top before KPI cards.

Preferred style:

```css
.notice {
  background: #fff8e6;
  border: 1px solid #f4d28b;
  border-radius: 10px;
  padding: 14px 18px;
  line-height: 1.5;
  color: #674b00;
  font-size: 14px;
}
```

Use this block for information that materially changes interpretation, such as:

- incomplete source fields
- proxy definitions
- exclusion rules
- data freshness
- historical system migrations
- unavailable measures

Do not hide important methodology in tooltips only.

---

# 6. KPI Cards

KPI cards are a major part of this design language.

## Desktop layout

Prefer a single horizontal row of **4 to 6 KPI cards**.

For six KPIs:

```css
.kpis {
  display: grid;
  grid-template-columns: repeat(6, 1fr);
  gap: 14px;
  margin-bottom: 20px;
}
```

## Card styling

```css
.kpi,
.card {
  background: #ffffff;
  border: 1px solid #e5e7eb;
  border-radius: 12px;
  box-shadow: 0 2px 8px rgba(18,48,74,.05);
}

.kpi {
  padding: 18px;
  min-height: 118px;
}
```

The shadow must remain subtle. Do not use floating cards with heavy shadows.

## KPI content hierarchy

Each KPI should normally contain:

1. small muted label
2. large navy value
3. small muted contextual line

Example:

```text
Inpatient admissions
21,593
20.27% admission rate
```

Recommended styling:

```css
.kpi .label {
  color: #667085;
  font-size: 13px;
  line-height: 1.3;
}

.kpi .value {
  color: #12304a;
  font-size: 28px;
  font-weight: bold;
  margin-top: 12px;
}

.kpi .sub {
  color: #667085;
  font-size: 12px;
  margin-top: 5px;
}
```

## KPI rules

- Use meaningful operational measures, not decorative statistics.
- Keep the primary value short.
- Use consistent number formatting.
- Percentages should normally use 1–2 decimal places where analytically useful.
- Use commas for thousands.
- Units should be obvious.
- Do not turn every KPI into a coloured tile.
- Do not use oversized icons unless explicitly requested.

---

# 7. Analytical Card Grid

The default analytical area uses a **two-column desktop grid**.

```css
.grid {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 20px;
  margin-bottom: 20px;
}
```

Charts should appear inside white cards.

```css
.card {
  padding: 18px;
  min-height: 340px;
}
```

## Wide cards

The most important trend chart should often span both columns.

```css
.card.wide {
  grid-column: span 2;
}
```

Use a wide card for:

- primary volume trend
- volume + rate combination chart
- major time-series comparison
- the dashboard's most important analytical view

---

# 8. Card Titles and Subtitles

Every analytical card should have a clear title and, where useful, one short explanatory subtitle.

Preferred hierarchy:

```css
.card h2 {
  margin: 0 0 4px;
  font-size: 20px;
  color: #12304a;
}

.card .subtitle {
  color: #667085;
  font-size: 13px;
  margin-bottom: 14px;
}
```

## Writing style

Titles should describe the metric directly.

Good:

- Monthly Attendance and Inpatient Admission Rate
- 48-hour Re-attendance
- EMD Turnaround Time
- Patient Demographics

Avoid vague titles such as:

- Analytics
- Overview 1
- Interesting Trends
- Performance Graph

Subtitles should explain definition, period, denominator, or chart encoding in one sentence.

---

# 9. Chart Design Rules

Use charts only when they make comparison or change easier to understand.

Default charting style should resemble a clean Chart.js implementation.

## Global chart typography

```javascript
Chart.defaults.font.family = "Arial, Helvetica, sans-serif";
Chart.defaults.font.size = 12;
Chart.defaults.color = "#344054";
```

## Chart height

Use approximately `275px` inside standard cards.

```css
.chart-wrap {
  height: 275px;
  position: relative;
}
```

---

# 10. Preferred Chart Types

## Time-series volume

Use bars when monthly or quarterly volumes are important.

Primary series:

```text
#1f78b4
```

## Rate or secondary time-series

Use a line over the bars when comparing a rate against volume.

Example:

- attendance = blue bars
- admission rate = orange line

For dual-axis charts:

- left axis = counts
- right axis = percentage or rate
- disable right-axis gridlines over the chart area

## Single rate over time

Use a line chart with a restrained translucent fill when useful.

For risk or adverse measures, default to red.

## Average vs median

Use two clearly differentiated lines.

Recommended pairing:

- average = purple
- median = teal

## Category distributions

Use vertical bars for small categorical sets.

Use horizontal bars when labels are long or when presenting ranked categories such as:

- top geographic areas
- nationalities
- departments
- diagnoses
- service types

## Composition

Use doughnut charts only when there are relatively few meaningful categories and composition is the main message.

Do not use pie/doughnut charts for precise comparison across many categories.

---

# 11. Chart Formatting

## Axis rules

- Start count axes at zero unless there is a strong analytical reason not to.
- Clearly label units such as `%`, `Minutes`, `Attendances`, or `Records`.
- Use angled x-axis labels only when necessary.
- Keep gridlines light and unobtrusive.
- Avoid unnecessary decimal places.

## Legends

- Hide the legend for a single obvious series.
- Keep legends for multi-series charts.
- Place doughnut-chart legends to the right when desktop space permits.

## Line charts

Default:

```javascript
tension: 0.25,
pointRadius: 4
```

Lines should remain readable and analytical rather than decorative.

## Interaction

For multi-series time trends, use index-based hover behaviour where available so users can compare values for the same period.

---

# 12. Management Insights Section

A dashboard should not stop at visualisation. Include a concise management interpretation section when analytical commentary is appropriate.

Use a white card containing a **three-column insight grid** on desktop.

```css
.insights {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 14px;
}
```

Each insight should look like:

```css
.insight {
  border-left: 4px solid #1f78b4;
  background: #f8fafc;
  padding: 14px 16px;
  border-radius: 8px;
  line-height: 1.45;
  font-size: 14px;
}
```

## Insight structure

Each insight should contain:

1. a bold management conclusion
2. one short evidence-based explanation

Example:

> **Demand has increased materially.**
>
> Monthly attendance increased from approximately 1,100–1,300 in the earlier period to around 1,800–2,200 in the most recent year.

## Insight accent colours

Use accent colours to distinguish themes, not decoration.

Examples:

- blue = general operational trend
- orange = capacity or pressure
- red = risk or deterioration
- teal = positive operational characteristic
- purple = clinical/service-mix observation
- grey = data limitation

Every statement must be supported by the displayed data or known analysis.

Do not invent causal explanations.

---

# 13. Footer: Definitions and Limitations

Always provide a methodology / definitions footer when metrics could be misunderstood.

Preferred appearance:

```css
footer {
  color: #667085;
  font-size: 12px;
  line-height: 1.5;
  margin-top: 20px;
  background: white;
  padding: 16px;
  border-radius: 10px;
  border: 1px solid #e5e7eb;
}
```

Use it for:

- metric definitions
- numerator / denominator rules
- exclusions
- source-table limitations
- proxy fields
- incomplete historical joins
- known missing data
- caveats affecting interpretation

This is part of the dashboard design, not an afterthought.

---

# 14. Typography

Default font stack:

```css
font-family: Arial, Helvetica, sans-serif;
```

Typography should be neutral and highly readable.

Hierarchy:

- dashboard title: 24–38px
- card title: ~20px
- KPI value: ~28px bold
- body / insight: ~14px
- labels / subtitles: ~13px
- footnotes / secondary KPI text: ~12px

Do not use decorative fonts.

---

# 15. Spacing and Density

Use compact enterprise spacing rather than oversized consumer-app spacing.

Recommended defaults:

- page margin above content: 24px
- KPI gap: 14px
- analytical grid gap: 20px
- card padding: 18px
- card radius: 12px
- insight radius: 8px

Aim for a dashboard where a manager can see the KPIs and several major charts on a standard desktop screen without excessive scrolling.

---

# 16. Responsive Behaviour

The dashboard must be responsive.

Recommended breakpoints:

```css
@media (max-width: 1050px) {
  .kpis {
    grid-template-columns: repeat(3, 1fr);
  }
}

@media (max-width: 700px) {
  .kpis,
  .grid,
  .insights {
    grid-template-columns: 1fr;
  }

  .card.wide {
    grid-column: span 1;
  }

  .chart-wrap {
    height: 300px;
  }
}
```

Desktop is the primary experience, but smaller layouts must remain fully usable.

Do not shrink text or charts excessively to preserve the desktop layout on mobile.

---

# 17. Information Design Principles

When deciding what to display, follow these rules.

## Prioritise

1. operational scale
2. quality / outcome rate
3. capacity / throughput
4. trend over time
5. composition / segmentation
6. management implications
7. data limitations

## Prefer

- a few high-value KPIs over dozens of small metrics
- clear trends over decorative visualisations
- descriptive titles over abbreviations where space permits
- directly labelled units
- transparent caveats
- charts that answer a management question

## Avoid

- excessive icons
- glassmorphism
- large empty hero sections
- bright gradient cards
- dark-mode styling by default
- gauges unless genuinely necessary
- 3D charts
- excessive pie charts
- dozens of colours
- animations that delay comprehension
- ornamental illustrations inside analytical cards
- overly rounded pill-shaped UI elements
- tiny text
- dense control panels when no interaction is required

---

# 18. Dashboard Composition Rules

Unless the data requires another structure, default to this composition:

```text
HEADER
Dashboard title
Reporting period | Source

OPTIONAL DATA NOTE
Important exclusions / limitations

KPI ROW
KPI 1 | KPI 2 | KPI 3 | KPI 4 | KPI 5 | KPI 6

PRIMARY ANALYSIS
Wide time-series chart spanning full width

SECONDARY ANALYSIS
Chart | Chart
Chart | Chart
Chart | Chart

MANAGEMENT INSIGHTS
Insight | Insight | Insight
Insight | Insight | Insight

DEFINITIONS AND LIMITATIONS
Methodology / caveats / data-quality notes
```

This is the default pattern unless the user's content strongly calls for another hierarchy.

---

# 19. Data Storytelling Rules

A finished dashboard should answer these questions quickly:

1. **What is the current scale?**
2. **What changed over time?**
3. **Which rates or outcomes matter?**
4. **Where are the operational pressures?**
5. **What segments explain the picture?**
6. **What should management notice?**
7. **What limitations affect interpretation?**

If the dashboard does not answer these questions, improve the information architecture before adding more styling.

---

# 20. Accuracy and Analytical Integrity

Never sacrifice analytical correctness for visual neatness.

When generating dashboard content:

- do not invent missing data
- do not silently substitute definitions
- clearly mark proxy measures
- explicitly identify missing or incomplete data
- distinguish counts, rates, averages, and medians
- make denominators clear where percentages may be ambiguous
- keep reporting periods consistent
- do not imply causation from correlation or trend alone
- label estimated or derived values

If an important limitation exists, surface it visibly in the notice, subtitle, insight, or methodology footer.

---

# 21. Implementation Preference for HTML Dashboards

When producing a self-contained HTML dashboard, prefer:

- semantic HTML
- CSS Grid
- CSS custom properties for design tokens
- Chart.js or an equivalent lightweight charting library
- responsive chart containers
- minimal JavaScript outside data preparation and chart configuration

Use reusable CSS classes rather than styling every element inline.

Inline colour overrides are acceptable for individual insight accents or unique data-series semantics.

---

# 22. Default Dashboard Skeleton

Use this as the starting structure when generating a new dashboard:

```html
<body>
  <header>
    <h1>Dashboard Title</h1>
    <p>Reporting period | Source</p>
  </header>

  <main>
    <div class="notice">
      <strong>Data-quality note:</strong>
      Important methodology or limitations.
    </div>

    <section class="kpis">
      <!-- 4–6 KPI cards -->
    </section>

    <section class="grid">
      <div class="card wide">
        <h2>Primary Trend</h2>
        <div class="subtitle">Short explanation.</div>
        <div class="chart-wrap">
          <canvas></canvas>
        </div>
      </div>

      <!-- secondary chart cards -->
    </section>

    <section class="card">
      <h2>Key Management Insights</h2>
      <div class="subtitle">Evidence-based observations.</div>
      <div class="insights">
        <!-- insight blocks -->
      </div>
    </section>

    <footer>
      <strong>Definitions and limitations:</strong>
      <ul>
        <!-- methodology notes -->
      </ul>
    </footer>
  </main>
</body>
```

---

# 23. Final Quality Check

Before completing any dashboard, verify all of the following.

- [ ] The dashboard uses the navy / blue enterprise visual identity.
- [ ] The page background is light blue-grey, not pure white.
- [ ] Cards are white with thin grey borders and very subtle shadows.
- [ ] The header uses the navy-to-blue gradient.
- [ ] The KPI row is immediately visible near the top.
- [ ] KPI cards use muted labels and large navy values.
- [ ] The most important trend chart receives the most space.
- [ ] Chart colours are restrained and semantically consistent.
- [ ] Chart titles explain exactly what is being shown.
- [ ] Units and denominators are clear.
- [ ] Management insights are derived from evidence rather than generic commentary.
- [ ] Important data-quality issues are visible.
- [ ] Definitions and limitations are included where needed.
- [ ] The dashboard remains readable at tablet and mobile widths.
- [ ] The result looks like an executive analytics product, not a marketing page.

---

# 24. Style Priority

When another instruction conflicts with the visual treatment in this skill, preserve this dashboard style unless the user explicitly requests a different visual direction.

Content requirements may change from dashboard to dashboard, but the following should remain recognisable:

**navy gradient header + light blue-grey page + white bordered cards + strong KPI row + two-column analytical grid + restrained Chart.js-style colours + management insight cards + visible methodology footer.**

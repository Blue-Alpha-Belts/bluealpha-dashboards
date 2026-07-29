# Blue Alpha Dashboards — Project Context

> This file is a living document. **Both AIs (Maverick and Patty's assistant) should update it whenever meaningful changes are made** — new features, design decisions, data sources added, bugs fixed, etc. Keep it accurate and current.

---

## What This Project Is

Internal dashboards for Blue Alpha (bluealphabelts.com) — a ~$5M/year tactical gear company based in Newnan, GA. The dashboards give key staff role-specific views of operations, email marketing, production, and product data pulled live from Airtable.

**Live URL:** Deployed on Railway (auto-deploys on push to `master`)
**Repo:** https://github.com/maverick-for-jesse/bluealpha-dashboards
**Stack:** Plain HTML/CSS/JavaScript — no framework, no build step. Files are static and served directly.

---

## Dashboards

### Jesse (`jesse.html`) — Owner/CEO
- **Email Calendar** — Visual monthly calendar of scheduled email campaigns
- **Email Pipeline** — Kanban-style view of email ideas from idea → live
- **Ideas** — Product/marketing idea tracker

### Kelly (`kelly.html`) — Director of Operations & Personnel
- **Email Calendar** — Same calendar view as Jesse
- **Email Pipeline** — Campaign pipeline

### Patty (`patty.html`) — Director of Systems & Finance
- **Email Calendar** — Same calendar view
- **Email Pipeline** — Campaign pipeline
- Auth protected with key: `ba_patty_auth`

### Kurt (`kurt.html`) — Co-founder
- **Email Calendar** — Campaign calendar
- **Ideas** — Product/marketing ideas
- **Mentions** — Brand mentions monitor

---

## Brand & Design

- **Primary color:** `#1B3A6B` (navy blue) — used for sidebar, headers, buttons
- **Accent color:** `#B22222` (dark red) — used for active nav states, highlights
- **Background:** `#f0f2f5` (light gray)
- **Cards:** White (`#fff`), `border-radius: 12px`, subtle shadow
- **Font:** System stack — `-apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif`
- **Logo:** `ba-logo.jpg` in the `static/` folder
- Calendar cells: `min-height: 130px` (updated Mar 11 2026 — Jesse prefers taller/squarer cells)
- No markdown tables in UI — use HTML tables or bullet lists

---

## Data Source — Airtable

All dashboards pull data client-side directly from Airtable via the REST API.

**Base ID:** `appTCZwoETAInVuiX`
**Token:** `pat3wxVXHb3JhDBBn.e0ca55ff285ba026b093f4232320f63fff16252679f6a4c08cc85ea3425c4954`

### Key Tables
| Table | Purpose |
|-------|---------|
| Email campaigns / calendar data | Email Calendar tab |
| Pipeline stages | Email Pipeline (Kanban) |
| Ideas | Product/marketing ideas |
| Mentions | Brand mentions (Kurt's dashboard) |

> Note: Table IDs are fetched dynamically. Check the existing dashboard JS for the exact `tbl...` IDs in use if adding new data.

---

## Deployment

Push to `master` → Railway auto-deploys within ~60 seconds.

```bash
# Deploy manually if needed
RAILWAY_TOKEN=<token> railway up
```

Project ID: `6df1f71b-1005-4f8b-b096-22f7a2129262`
Environment ID: `1f24309d-c9a5-4430-a8bb-e3e0fbd14843`
Service ID: `c8521da4-abc9-47df-a665-ce1bdb26f02d`

---

## Conventions

- **One HTML file per person** — all tabs/sections live in a single file per dashboard user
- **Tab switching** is handled by `showTab('tabname')` in vanilla JS
- **No external dependencies** — no npm, no webpack, no CDN (except Airtable API calls)
- **Auth** is lightweight client-side PIN/key (not production-grade security — internal tool only)
- **Mobile** — not a priority, desktop-first

---

## Change Log

| Date | Change | Who |
|------|--------|-----|
| 2026-03-11 | Calendar cells increased from 90px → 130px on all dashboards | Maverick |
| 2026-03-11 | CONTEXT.md created | Maverick |
| 2026-07-29 | Quote portal three-tier access: guest quote building (print-only, nothing saved), self-serve registration (`/signup`, saves quotes, `Application Status="Registered"`), and approval-gated ordering (existing `/apply` doubles as the ordering-access upgrade for registered accounts). Quote/PDF/invoice APIs locked down to session + ownership (or staff admin session). Tax rate now required to approve non-exempt applicants. Airtable Customers table: added "Registered" select option + "Registered Date" field. | Patty's assistant |

---

## Notes for Patty's AI

- Jesse's preferences lean toward **clean, data-dense, professional** — not flashy
- When in doubt, match the existing style rather than introducing new patterns
- Always test that Airtable data loads correctly after changes
- Push to `master` to deploy — Railway handles the rest
- If you make a significant design or data change, **add a row to the Change Log above**

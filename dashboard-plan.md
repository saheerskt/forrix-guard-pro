# MDGuard Dashboard — Lovable AI Redesign Prompt

> Copy everything between the horizontal rules below and paste it directly into Lovable.

---

Design a **professional industrial energy management dashboard** for **MDGuard by ForrixGuard** — a maximum-demand intelligence platform for C&I (commercial & industrial) sites in India and the GCC region. The product sits between existing hardware (grid meters, solar inverters, BESS/battery systems, DG sets) and the site operator, providing demand prediction, tariff-aware BESS dispatch, and ROI reporting.

---

### WHO USES THIS

**Primary user:** Site energy manager / electrical engineer at a factory, hospital, or commercial building.  
They check this screen every morning and during peak hours (09:00–13:00, 18:00–21:00).  
They understand kW, kVA, tariff slabs, and SOC — but they are **not** software developers.  
They do **not** care about phase-level details or raw telemetry during normal operation.

**Critical user questions — design must answer these instantly, without reading:**
1. Am I safe from a demand breach right now?
2. How close is my peak demand to the sanctioned limit this month?
3. Is my BESS ready to protect me if demand spikes?
4. How much has MDGuard saved me this month vs. what I would have paid in penalties?
5. Is solar contributing or is everything coming from the grid?

---

### DESIGN PHILOSOPHY

- **Industrial HMI meets modern SaaS** — Siemens SCADA clarity + Grafana dark mode polish
- **KPI-first hierarchy**: The most important number must be the largest element on screen. Secondary data is collapsed or visually de-emphasized by default.
- **Traffic-light language**: Every status communicates in green / amber / red. No jargon required at a glance.
- **Zero noise**: Phase-level detail (V, I, PF per phase) belongs behind a "Technical Detail" expand toggle — never visible by default.
- **ISA-101 / IEC 61511 industrial HMI principles**: Muted dark background, high-contrast value display, consistent color semantics, clear alarm hierarchy.
- **Confidence, not decoration**: Cards feel solid and measured — no glassmorphism blur, no animated value counters, no gradient fills on data.

---

### COLOR SYSTEM

**Theme**: Dark mode primary. Light mode optional.

| Role | Color | Hex |
|---|---|---|
| Page background | Deep navy | `#0a0f1e` |
| Card surface | Elevated dark | `#111827` |
| Card border | Subtle rule | `rgba(255,255,255,0.08)` |
| Grid / primary values | Cyan | `#06b6d4` |
| Safe / OK / savings | Emerald | `#10b981` |
| Caution / warning | Amber | `#f59e0b` |
| Breach / fault / cost | Red | `#ef4444` |
| BESS / battery | Purple | `#a855f7` |
| Solar / PV | Yellow | `#fbbf24` |
| Primary text | Near-white | `#f1f5f9` |
| Labels / units | Mid-grey | `#64748b` |

Status = solid color + icon. Never color alone.

---

### TYPOGRAPHY

- **Live values** (kW, %, kVA, ₹): `JetBrains Mono` bold, 1.2–2.4rem scaled by importance
- **Field labels**: `Inter` 0.72rem, uppercase, letter-spacing 0.08em, `#64748b`
- **Card titles**: `Inter` 0.9rem medium, `#f1f5f9`
- **Navigation**: `Inter` 0.85rem medium

Rules:
- Demand kW: 1 decimal place, right-aligned
- Financial (₹, AED): locale-formatted separators — ₹2,14,000
- SOC: large bold integer, no decimal
- Window countdown: `MM:SS` monospace

---

### PAGE LAYOUT

**Fixed top nav bar (56px):**
- Left: MDGuard logo + "ForrixGuard" product name
- Centre: Site name + ● connection status (green = live, red = disconnected)
- Right: Last-updated timestamp + user avatar + settings icon

**Main content — desktop ≥1200px:**  
Two-column layout: Left 60% | Right 40%. Single column on tablet/mobile.

---

### CARD HIERARCHY

#### LEFT COLUMN — Hero "Am I Safe?" Demand Card

The single most important card on the page. Cannot be missed.

```
┌──────────────────────────────────────────────────────┐
│  ● DEMAND SAFE                 Window: 08:23 left    │
├──────────────────────────────────────────────────────┤
│  Current        Projected        Allowed             │
│  612 kW         689 kW           760 kW              │
│                                                      │
│  [================================---------] 89%     │
│   Green ─────────── Amber ─── Red threshold          │
│                                                      │
│  Headroom: +71 kW          Correction needed: 0 kW  │
│  Next window forecast: 702 kW   ● Low risk           │
└──────────────────────────────────────────────────────┘
```

- Progress bar: green 0–80%, amber 80–90%, red 90–100%+
- Badge: "DEMAND SAFE" / "CAUTION" / "BREACH RISK" — large, color-filled, full header width
- Breach active: card border pulses red; amber action banner appears below with BESS recommendation
- Remove from this view: raw event cause labels, "Recommendation: monitor" text, telemetry staleness text (reduce to a small dot indicator in card corner)

#### LEFT COLUMN — Site Power Flow Card (below demand card)

Simple icon-based energy flow:

```
  ☀ Solar        🔋 BESS          ⚡ Grid
  245 kW         -89 kW           612 kW
      ↘               ↘               ↘
                             → 🏗 Site Load: 768 kW
```

kW values and direction only. No phase data, no frequency, no PF.  
Color: green = generating/supporting, cyan = grid import, purple = BESS, amber = DG.

---

#### RIGHT COLUMN — Stacked KPI Cards

**R1 — This Month's Demand**
```
MTD Peak              Sanctioned
698 kW                800 kW

[====================-------] 87.3%

Demand charge exposure:   ₹2,44,300
Breach events this month: 0
```

**R2 — BESS / Battery Status**
```
🔋 Battery 1                    85%
████████████████████░░░░
Discharge available: 320 kWh
Mode: IDLE — Ready to protect
Reserve floor: 35%
```
SOC as a bold horizontal bar — not a semicircle arc gauge. Mode in plain English, not enum.

**R3 — Tariff & Savings**
```
BESS Savings Today          ₹ 4,200  ↑
BESS Savings MTD            ₹31,400  ↑
─────────────────────────────────────
Demand charge rate          ₹350 /kVA/mo
Breach cost if hit now      ₹28,000
```
Green for savings. Red for cost exposure. Hidden if tariff not configured — show "Configure tariff → Setup" link instead.

**R4 — DG Status** (shown only when DG is configured)
```
🔧 DG 1                     ● IDLE
Runtime today:  0 min
Runtime MTD:    4h 22 min
```

---

### DEVICE HEALTH STRIP (below two-column zone)

Horizontal scrollable row of compact 180px device cards — one per meter, inverter, BESS unit:

```
┌────────────┐  ┌────────────┐  ┌────────────┐
│ ⚡ Grid     │  │ ☀ Solar    │  │ 🔋 BESS    │
│ SDM630     │  │ SMA 50kW   │  │ BYD 200kWh │
│ 612 kW     │  │ 245 kW     │  │ SOC 85%    │
│ ● Live     │  │ ● Live     │  │ ● Live     │
└────────────┘  └────────────┘  └────────────┘
```

Clicking a card opens a slide-in side drawer showing:
- Phase A/B/C voltage, current, power, PF
- Battery cell voltages, temperature, MPPT data

Phase-level data is **never visible on the main dashboard** — only accessible through this drawer interaction.

---

### ALARM SYSTEM

**Full-width dismissable banner** at top of content area:

- 🔴 Red: "BREACH RISK — Demand projected to exceed limit in 4 min. Recommended: discharge 89 kW from BESS."
- 🟡 Amber: "CAUTION — Demand at 87% of sanctioned limit. Monitoring closely."
- 🟢 Green (5s auto-dismiss): "BESS dispatch applied — 89 kW discharge for 8 minutes."

**Toast stack** (bottom-right): icon + short text, 4s auto-dismiss.

---

### MONTHLY REPORT PAGE

**Hero numbers bar** (5 values, large, one row):
```
Peak This Month   Sanctioned   Breach Events   BESS Savings   Demand Charge
698 kW            800 kW       0               ₹31,400        ₹2,44,300
```

**Daily demand chart**: Bar chart, one bar per day, sanctioned limit as horizontal line.  
Bar colors: green = safe, amber = caution, red = breach.

**Events table**: Date & Time | Peak Demand | Status | BESS Action  
Plain English descriptions — no raw event codes.

**Navigation**: `← March 2026 →` with Export to PDF / CSV button.

---

### SETUP PAGE

**Tabbed layout** — not a long-scroll page:

Tabs: `Site & Contract` | `Tariff & Rates` | `Device Roles` | `System`

**Status strip** at top of page (always visible):
```
● Grid meter — Live    ● Solar — Live    ● BESS — Live    ○ DG — Not configured
```

**Tariff tab** with live preview:
- Currency picker: INR / AED / USD / GBP
- Demand charge field with inline note: "At ₹350/kVA × 698 kW peak = ₹2,44,300/month"
- Peak hours shown as a 24h timeline bar with peak band shaded amber

---

### COMPONENTS TO DELIVER

1. **DemandHeroCard** — Status badge, progress bar, current/projected/allowed, countdown, forecast
2. **PowerFlowCard** — Energy flow diagram (solar/grid/BESS/DG → load)
3. **MonthlyDemandCard** — MTD peak vs. sanctioned with exposure cost
4. **BessStatusCard** — Horizontal SOC bar, mode text, discharge capacity
5. **TariffSavingsCard** — Savings vs. breach cost in financial language
6. **DeviceHealthStrip** — Horizontal compact cards with expandable drawer
7. **AlarmBanner** — Contextual breach/caution/safe full-width banner
8. **NavigationBar** — Fixed top bar with site name, connection dot, timestamp
9. **MonthlyReportPage** — Hero metrics + daily chart + plain-English events table
10. **SetupPage** — Tabbed commissioning form with live preview and device status strip

---

### WHAT TO AVOID

- ❌ Semicircle arc gauge dials — use horizontal progress bars and large bold numbers instead
- ❌ Phase-level V/I/PF/frequency visible on the main dashboard
- ❌ Raw event codes or JSON field names visible to end user
- ❌ Glassmorphism blur effects on cards — reduces legibility at information density
- ❌ Animated value counters — live readings should update cleanly without animation
- ❌ Decorative gradient card backgrounds
- ❌ Making decorative elements larger than the most important data values
- ❌ Skeleton shimmer loading — use `--` placeholder for unavailable data
- ❌ Jargon labels: "Cause", "Telemetry", "Recommendation" → use plain language equivalents

---

### REFERENCE AESTHETICS

- **Grafana Cloud** dark mode — data density, professional grid layout, chart styling
- **Linear** — clean confident typography and deliberate card hierarchy
- **Siemens WinCC / ISA-101** — alarm hierarchy, value legibility, industrial readability
- **Stripe Dashboard** — financial summary presentation (MTD, savings, charges)

**The test:** A grid operator trusts it immediately. A site manager reads the key number from 2 metres away during a demand peak event.

---

*Product:* MDGuard by ForrixGuard — middle intelligence layer for C&I maximum demand management  
*Markets:* India (INR tariff slabs, kVA demand charges), GCC (AED demand structure)  
*Platform:* Embedded Linux (iMX93 gateway), accessed via local LAN web browser  
*Primary screen:* 1920×1080 desktop / control room monitor. Responsive to 1024px tablet.

import { createFileRoute } from "@tanstack/react-router";
import { useEffect, useMemo, useState } from "react";

export const Route = createFileRoute("/")({
  component: MDGuardDashboard,
  head: () => ({
    meta: [
      { title: "MDGuard Live — ForrixGuard" },
      {
        name: "description",
        content:
          "Live maximum-demand intelligence: BESS dispatch advisory, tariff exposure, and breach-risk monitoring for C&I sites.",
      },
    ],
  }),
});

/* ------------------------------------------------------------------ */
/*  Mock telemetry — simulates the ForrixGuard /api/data shape         */
/* ------------------------------------------------------------------ */

type Telemetry = {
  current_kw: number;
  projected_kw: number;
  allowed_kw: number;
  sanctioned_kw: number;
  correction_kw: number;
  breach_risk: boolean;
  time_left_sec: number;
  next_window_kw: number;
  control_command_allowed: boolean;
  telemetry_fresh: boolean;
  // power flow
  solar_kw: number;
  bess_kw: number; // +discharge / -charge
  grid_kw: number;
  load_kw: number;
  // bess
  soc: number;
  bess_mode: "IDLE" | "DISCHARGING" | "CHARGING";
  discharge_avail_kwh: number;
  reserve_floor_pct: number;
  // monthly
  mtd_peak_kw: number;
  breach_events_mtd: number;
  // tariff
  currency: "INR" | "AED" | "USD" | "GBP";
  demand_charge_per_kva: number;
  projected_demand_charge: number;
  breach_cost_if_hit: number;
  bess_savings_today: number;
  bess_savings_mtd: number;
  tariff_loaded: boolean;
  // dg
  dg_running: boolean;
  dg_runtime_today_min: number;
  dg_runtime_mtd_min: number;
  // event
  event_cause: string;
  // site
  site_name: string;
};

function useMockTelemetry(): Telemetry {
  const [tick, setTick] = useState(0);
  useEffect(() => {
    const t = setInterval(() => setTick((n) => n + 1), 2000);
    return () => clearInterval(t);
  }, []);

  return useMemo<Telemetry>(() => {
    // Sine wave around realistic factory load
    const base = 612;
    const swing = Math.sin(tick / 6) * 60;
    const noise = (Math.sin(tick * 1.7) + Math.cos(tick * 0.9)) * 15;
    const current_kw = Math.max(380, base + swing + noise);
    const projected_kw = current_kw + 60 + Math.sin(tick / 4) * 25;
    const allowed_kw = 760;
    const correction = Math.max(0, projected_kw - allowed_kw);
    const breach = projected_kw > allowed_kw;
    const solar_kw = 245 + Math.sin(tick / 8) * 35;
    const bess_kw = breach ? 89 : 0;
    const grid_kw = current_kw;
    const load_kw = grid_kw + solar_kw - (-bess_kw);

    return {
      current_kw,
      projected_kw,
      allowed_kw,
      sanctioned_kw: 800,
      correction_kw: correction,
      breach_risk: breach,
      time_left_sec: 600 - ((tick * 2) % 600),
      next_window_kw: 702,
      control_command_allowed: false, // Advisory only
      telemetry_fresh: true,
      solar_kw,
      bess_kw,
      grid_kw,
      load_kw,
      soc: 85 - (tick % 40) * 0.05,
      bess_mode: bess_kw > 0 ? "DISCHARGING" : "IDLE",
      discharge_avail_kwh: 320,
      reserve_floor_pct: 35,
      mtd_peak_kw: 698,
      breach_events_mtd: 0,
      currency: "INR",
      demand_charge_per_kva: 350,
      projected_demand_charge: 244300,
      breach_cost_if_hit: Math.round(correction * 350),
      bess_savings_today: 4200,
      bess_savings_mtd: 31400,
      tariff_loaded: true,
      dg_running: false,
      dg_runtime_today_min: 0,
      dg_runtime_mtd_min: 262,
      event_cause: breach ? "Load step increase" : "Stable",
      site_name: "Coimbatore Plant 02",
    };
  }, [tick]);
}

/* ------------------------------------------------------------------ */
/*  Helpers                                                            */
/* ------------------------------------------------------------------ */

const fmt = (n: number, d = 0) =>
  n == null || isNaN(n)
    ? "—"
    : Number(n).toLocaleString("en-IN", {
        maximumFractionDigits: d,
        minimumFractionDigits: d,
      });

const fmtMoney = (n: number, ccy: Telemetry["currency"]) => {
  const sym: Record<Telemetry["currency"], string> = {
    INR: "₹",
    AED: "AED ",
    USD: "$",
    GBP: "£",
  };
  return (
    sym[ccy] +
    Math.round(n).toLocaleString(ccy === "INR" ? "en-IN" : "en-US")
  );
};

const fmtCountdown = (sec: number) => {
  const m = Math.floor(sec / 60);
  const s = sec % 60;
  return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
};

const useNow = () => {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const t = setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(t);
  }, []);
  return now;
};

/* ------------------------------------------------------------------ */
/*  Components                                                         */
/* ------------------------------------------------------------------ */

function NavBar({ siteName, fresh }: { siteName: string; fresh: boolean }) {
  const now = useNow();
  return (
    <header className="sticky top-0 z-50 flex h-14 items-center gap-6 border-b border-[--color-border] bg-[--color-surface] px-5">
      <div className="flex items-center gap-2 font-semibold">
        <div className="grid h-7 w-7 place-items-center rounded-md bg-gradient-to-br from-[--color-cyan] to-sky-500 font-mono text-sm font-extrabold text-[#001018]">
          M
        </div>
        <span className="text-[--color-text]">MDGuard</span>
        <span className="ml-1 text-xs font-medium text-[--color-label]">
          by ForrixGuard
        </span>
      </div>

      <nav className="ml-2 hidden items-center gap-1 md:flex">
        <NavLink active icon="fa-gauge-high" label="Live" />
        <NavLink icon="fa-file-lines" label="Report" />
        <NavLink icon="fa-sliders" label="Setup" />
        <NavLink icon="fa-grip" label="Devices" />
      </nav>

      <div className="ml-auto flex flex-1 items-center justify-center gap-2 text-sm text-[--color-text-dim]">
        <span className="font-semibold text-[--color-text]">{siteName}</span>
        <span
          className={`h-2 w-2 rounded-full ${
            fresh
              ? "bg-[--color-good] shadow-[0_0_8px_rgba(16,185,129,0.6)]"
              : "bg-[--color-bad] shadow-[0_0_8px_rgba(239,68,68,0.6)]"
          }`}
        />
        <span>{fresh ? "Live" : "Disconnected"}</span>
      </div>

      <div className="flex items-center gap-2 text-xs text-[--color-text-dim]">
        <span>Updated</span>
        <span className="mono text-[--color-text]">
          {now.toLocaleTimeString()}
        </span>
        <button
          aria-label="Settings"
          className="grid h-9 w-9 place-items-center rounded-lg border border-[--color-border] text-[--color-text-dim] transition hover:border-[--color-border-strong] hover:bg-[--color-surface-3] hover:text-[--color-text]"
        >
          <i className="fa-solid fa-gear" />
        </button>
      </div>
    </header>
  );
}

function NavLink({
  icon,
  label,
  active,
}: {
  icon: string;
  label: string;
  active?: boolean;
}) {
  return (
    <a
      href="#"
      className={`flex items-center gap-2 rounded-lg px-3 py-2 text-sm transition ${
        active
          ? "bg-[--color-surface-3] text-[--color-text]"
          : "text-[--color-text-dim] hover:bg-[--color-surface-3] hover:text-[--color-text]"
      }`}
    >
      {active && (
        <span className="h-1.5 w-1.5 rounded-full bg-[--color-cyan] shadow-[0_0_8px_var(--color-cyan)]" />
      )}
      <i className={`fa-solid ${icon}`} />
      {label}
    </a>
  );
}

/* ------- Alarm banner ------- */
function AlarmBanner({ t }: { t: Telemetry }) {
  const pct = (t.current_kw / t.allowed_kw) * 100;
  if (t.breach_risk) {
    const verb = t.control_command_allowed
      ? "Recommended (auto-dispatch enabled)"
      : "Recommended (advisory only)";
    return (
      <Banner
        tone="bad"
        icon="fa-triangle-exclamation"
        text={`BREACH RISK — Demand projected to exceed limit. ${verb}: discharge ${fmt(t.correction_kw, 0)} kW from BESS.`}
      />
    );
  }
  if (pct >= 80) {
    return (
      <Banner
        tone="warn"
        icon="fa-circle-exclamation"
        text={`CAUTION — Demand at ${fmt(pct, 0)}% of allowed (${fmt(t.current_kw, 0)} / ${fmt(t.allowed_kw, 0)} kW). Monitoring closely.`}
      />
    );
  }
  return null;
}

function Banner({
  tone,
  icon,
  text,
}: {
  tone: "bad" | "warn" | "good";
  icon: string;
  text: string;
}) {
  const cls =
    tone === "bad"
      ? "border-red-500/40 bg-red-500/10 text-red-200"
      : tone === "warn"
        ? "border-amber-500/40 bg-amber-500/10 text-amber-200"
        : "border-emerald-500/40 bg-emerald-500/10 text-emerald-200";
  return (
    <div
      className={`mb-4 flex items-center justify-between gap-4 rounded-xl border px-4 py-3 text-sm ${cls}`}
      role="alert"
    >
      <div className="flex items-center gap-3">
        <i className={`fa-solid ${icon}`} />
        <span>{text}</span>
      </div>
    </div>
  );
}

/* ------- Hero demand card ------- */
function DemandHeroCard({ t }: { t: Telemetry }) {
  const pct = Math.min(100, (t.current_kw / t.allowed_kw) * 100);
  const headroom = t.allowed_kw - t.current_kw;

  let badgeTone: "good" | "warn" | "bad" = "good";
  let badgeText = "DEMAND SAFE";
  if (t.breach_risk || pct >= 90) {
    badgeTone = "bad";
    badgeText = "BREACH RISK";
  } else if (pct >= 80) {
    badgeTone = "warn";
    badgeText = "CAUTION";
  }

  const fillColor =
    pct >= 90
      ? "bg-[--color-bad]"
      : pct >= 80
        ? "bg-[--color-warn]"
        : "bg-[--color-good]";

  const cardCls =
    badgeTone === "bad"
      ? "mg-card mg-pulse border-red-500/50 p-5"
      : badgeTone === "warn"
        ? "mg-card border-amber-500/40 p-5"
        : "mg-card p-5";

  return (
    <section className={cardCls} aria-label="Current demand status">
      <header className="mb-4 flex items-center justify-between gap-3">
        <StatusBadge tone={badgeTone} text={badgeText} />
        <div className="flex items-center gap-2 text-sm text-[--color-text-dim]">
          <i className="fa-regular fa-clock" />
          Window:{" "}
          <b className="mono text-[--color-text]">
            {fmtCountdown(t.time_left_sec)}
          </b>{" "}
          left
        </div>
      </header>

      <div className="mb-4 grid grid-cols-3 gap-4">
        <HeroNum label="Current" value={t.current_kw} unit="kW" primary />
        <HeroNum label="Projected" value={t.projected_kw} unit="kW" />
        <HeroNum label="Allowed" value={t.allowed_kw} unit="kW" />
      </div>

      <div className="relative h-3.5 overflow-hidden rounded-lg border border-[--color-border] bg-[--color-surface-3]">
        <div
          className={`absolute inset-y-0 left-0 transition-[width,background] duration-500 ${fillColor}`}
          style={{ width: `${pct}%` }}
        />
        <div
          className="absolute inset-y-0 w-px bg-white/20"
          style={{ left: "80%" }}
        />
        <div
          className="absolute inset-y-0 w-px bg-white/20"
          style={{ left: "90%" }}
        />
      </div>
      <div className="mono mt-1 flex justify-between text-[11px] text-[--color-label]">
        <span>0%</span>
        <span>80% caution</span>
        <span>90% breach</span>
        <span>100%</span>
      </div>

      <footer className="mt-4 grid grid-cols-3 gap-4 border-t border-[--color-border] pt-4">
        <FootItem
          label="Headroom"
          value={`${headroom >= 0 ? "+" : ""}${fmt(headroom, 0)}`}
          unit="kW"
          tone={headroom < 0 ? "bad" : "default"}
        />
        <FootItem
          label="Correction needed"
          value={fmt(t.correction_kw, 0)}
          unit="kW"
          tone={t.correction_kw > 0 ? "bad" : "default"}
        />
        <FootItem
          label="Next-window forecast"
          value={fmt(t.next_window_kw, 0)}
          unit="kW"
        />
      </footer>

      {t.correction_kw > 0 && (
        <div className="mt-4 rounded-lg border border-amber-500/30 bg-amber-500/5 p-3 text-sm text-amber-200">
          <div className="font-semibold">
            Recommended: discharge {fmt(t.correction_kw, 0)} kW from BESS or
            reduce import.
          </div>
          <div className="mt-1 text-xs opacity-80">
            {t.control_command_allowed
              ? "Auto-dispatch will fire if not acknowledged."
              : "Advisory only — control mode is monitor-only."}
          </div>
        </div>
      )}
    </section>
  );
}

function StatusBadge({
  tone,
  text,
}: {
  tone: "good" | "warn" | "bad";
  text: string;
}) {
  const map = {
    good: "bg-emerald-500/10 text-emerald-300 border-emerald-500/40",
    warn: "bg-amber-500/10 text-amber-300 border-amber-500/40",
    bad: "bg-red-500/10 text-red-300 border-red-500/40",
  };
  const icon =
    tone === "bad"
      ? "fa-triangle-exclamation"
      : tone === "warn"
        ? "fa-circle-exclamation"
        : "fa-shield-halved";
  return (
    <span
      className={`inline-flex items-center gap-2 rounded-full border px-3 py-1.5 text-xs font-bold uppercase tracking-wider ${map[tone]}`}
    >
      <i className={`fa-solid ${icon}`} />
      {text}
    </span>
  );
}

function HeroNum({
  label,
  value,
  unit,
  primary,
}: {
  label: string;
  value: number;
  unit: string;
  primary?: boolean;
}) {
  return (
    <div>
      <div className="mg-label">{label}</div>
      <div
        className={`mono mt-1 font-bold leading-none ${
          primary
            ? "text-[2.4rem] text-[--color-cyan]"
            : "text-[2rem] text-[--color-text]"
        }`}
      >
        {fmt(value, 0)}
        <span className="ml-1 text-sm font-medium text-[--color-text-dim]">
          {unit}
        </span>
      </div>
    </div>
  );
}

function FootItem({
  label,
  value,
  unit,
  tone = "default",
}: {
  label: string;
  value: string;
  unit: string;
  tone?: "default" | "bad";
}) {
  return (
    <div>
      <div className="mg-label">{label}</div>
      <div
        className={`mono mt-1 text-xl font-bold ${
          tone === "bad" ? "text-[--color-bad]" : "text-[--color-text]"
        }`}
      >
        {value}
        <span className="ml-1 text-xs font-medium text-[--color-text-dim]">
          {unit}
        </span>
      </div>
    </div>
  );
}

/* ------- Power flow ------- */
function PowerFlowCard({ t }: { t: Telemetry }) {
  return (
    <section className="mg-card p-5">
      <CardHead icon="fa-bolt-lightning" title="Site Power Flow" sub="Live" />
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <FlowNode
          tone="yellow"
          icon="fa-sun"
          role="Solar"
          name="PV"
          kw={t.solar_kw}
          direction="generating"
        />
        <FlowNode
          tone="purple"
          icon="fa-battery-half"
          role="BESS"
          name={
            t.bess_mode === "DISCHARGING"
              ? "Discharging"
              : t.bess_mode === "CHARGING"
                ? "Charging"
                : "Idle"
          }
          kw={Math.abs(t.bess_kw)}
          direction={t.bess_kw > 0 ? "supporting" : "idle"}
          signed={t.bess_kw < 0 ? -t.bess_kw : t.bess_kw}
          showSign
          rawKw={t.bess_kw}
        />
        <FlowNode
          tone="cyan"
          icon="fa-plug"
          role="Grid"
          name="Import"
          kw={t.grid_kw}
          direction="import"
        />
        <FlowNode
          tone="text"
          icon="fa-industry"
          role="Site Load"
          name="Total"
          kw={t.load_kw}
          direction="load"
        />
      </div>
    </section>
  );
}

function FlowNode({
  tone,
  icon,
  role,
  name,
  kw,
  showSign,
  rawKw,
}: {
  tone: "yellow" | "purple" | "cyan" | "amber" | "text";
  icon: string;
  role: string;
  name: string;
  kw: number;
  direction?: string;
  showSign?: boolean;
  signed?: number;
  rawKw?: number;
}) {
  const colorMap = {
    yellow: "text-[--color-yellow]",
    purple: "text-[--color-purple]",
    cyan: "text-[--color-cyan]",
    amber: "text-[--color-warn]",
    text: "text-[--color-text]",
  };
  const sign =
    showSign && rawKw != null ? (rawKw < 0 ? "-" : rawKw > 0 ? "+" : "") : "";
  return (
    <div className="rounded-lg border border-[--color-border] bg-[--color-surface-2] p-3.5">
      <div className="flex items-center gap-2">
        <span className="grid h-8 w-8 place-items-center rounded-lg bg-[--color-surface-3]">
          <i className={`fa-solid ${icon} ${colorMap[tone]}`} />
        </span>
        <div className="flex flex-col leading-tight">
          <span className="mg-label">{role}</span>
          <span className="text-sm text-[--color-text]">{name}</span>
        </div>
      </div>
      <div className={`mono mt-2 text-2xl font-bold ${colorMap[tone]}`}>
        {sign}
        {fmt(kw, 0)}
        <span className="ml-1 text-[10px] font-medium text-[--color-text-dim]">
          kW
        </span>
      </div>
    </div>
  );
}

function CardHead({
  icon,
  title,
  sub,
}: {
  icon: string;
  title: string;
  sub?: string;
}) {
  return (
    <header className="mb-3.5 flex items-center justify-between">
      <div className="flex items-center gap-2 text-[0.9rem] font-semibold">
        <i className={`fa-solid ${icon} text-[--color-text-dim]`} />
        <span>{title}</span>
      </div>
      {sub && (
        <span className="mono text-[11px] uppercase tracking-wider text-[--color-label]">
          {sub}
        </span>
      )}
    </header>
  );
}

/* ------- Right column KPI cards ------- */
function MonthlyDemandCard({ t }: { t: Telemetry }) {
  const pct = (t.mtd_peak_kw / t.sanctioned_kw) * 100;
  const fillColor =
    pct >= 90
      ? "bg-[--color-bad]"
      : pct >= 80
        ? "bg-[--color-warn]"
        : "bg-[--color-good]";
  return (
    <section className="mg-card p-5">
      <CardHead
        icon="fa-calendar"
        title="This Month's Demand"
        sub={new Date().toLocaleString("en-US", {
          month: "long",
          year: "numeric",
        })}
      />
      <div className="flex items-baseline justify-between gap-4">
        <div>
          <div className="mg-label">MTD Peak</div>
          <div className="mono mt-1 text-2xl font-bold">
            {fmt(t.mtd_peak_kw, 0)}
            <span className="ml-1 text-xs font-medium text-[--color-text-dim]">
              kW
            </span>
          </div>
        </div>
        <div className="text-right">
          <div className="mg-label">Sanctioned</div>
          <div className="mono mt-1 text-2xl font-bold text-[--color-text-dim]">
            {fmt(t.sanctioned_kw, 0)}
            <span className="ml-1 text-xs font-medium">kW</span>
          </div>
        </div>
      </div>
      <div className="mt-3 h-3 overflow-hidden rounded-lg border border-[--color-border] bg-[--color-surface-3]">
        <div
          className={`h-full transition-[width] duration-500 ${fillColor}`}
          style={{ width: `${Math.min(100, pct)}%` }}
        />
      </div>
      <KvGrid
        rows={[
          [
            "Demand exposure",
            <span key="exp" className="text-[--color-bad]">
              {fmtMoney(t.projected_demand_charge, t.currency)}
            </span>,
          ],
          ["Breach events MTD", String(t.breach_events_mtd)],
        ]}
      />
    </section>
  );
}

function BessCard({ t }: { t: Telemetry }) {
  return (
    <section className="mg-card p-5">
      <CardHead icon="fa-battery-three-quarters" title="Battery (BESS)" sub={t.bess_mode} />
      <div className="mt-2 flex items-baseline justify-between">
        <span className="text-sm text-[--color-text-dim]">State of Charge</span>
        <span className="mono text-2xl font-bold">
          {fmt(t.soc, 0)}
          <span className="ml-1 text-xs font-medium text-[--color-text-dim]">
            %
          </span>
        </span>
      </div>
      <div className="relative mt-2 h-4 overflow-hidden rounded border border-[--color-border] bg-[--color-surface-3]">
        <div
          className="h-full bg-gradient-to-r from-[--color-purple] to-purple-300 transition-[width] duration-500"
          style={{ width: `${Math.min(100, Math.max(0, t.soc))}%` }}
        />
        <div
          className="absolute inset-y-0 w-0.5 bg-white/40"
          style={{ left: `${t.reserve_floor_pct}%` }}
          title={`Reserve floor ${t.reserve_floor_pct}%`}
        />
      </div>
      <KvGrid
        rows={[
          ["Discharge available", `${fmt(t.discharge_avail_kwh, 0)} kWh`],
          ["Reserve floor", `${t.reserve_floor_pct}%`],
        ]}
      />
    </section>
  );
}

function TariffCard({ t }: { t: Telemetry }) {
  if (!t.tariff_loaded) {
    return (
      <section className="mg-card p-5">
        <CardHead icon="fa-indian-rupee-sign" title="Tariff & Savings" />
        <p className="text-sm text-[--color-text-dim]">
          Tariff not configured.
        </p>
        <a
          href="#"
          className="mt-2 inline-block text-sm text-[--color-cyan] underline decoration-cyan-500/40"
        >
          Configure tariff →
        </a>
      </section>
    );
  }
  return (
    <section className="mg-card p-5">
      <CardHead icon="fa-indian-rupee-sign" title="Tariff & Savings" sub={t.currency} />
      <KvGrid
        rows={[
          [
            "BESS savings today",
            <span key="st" className="text-[--color-good]">
              {fmtMoney(t.bess_savings_today, t.currency)} ↑
            </span>,
          ],
          [
            "BESS savings MTD",
            <span key="sm" className="text-[--color-good]">
              {fmtMoney(t.bess_savings_mtd, t.currency)} ↑
            </span>,
          ],
          [
            "Demand charge rate",
            `${fmtMoney(t.demand_charge_per_kva, t.currency)} /kVA/mo`,
          ],
          [
            "Breach cost if hit now",
            <span key="bc" className="text-[--color-bad]">
              {fmtMoney(t.breach_cost_if_hit, t.currency)}
            </span>,
          ],
        ]}
      />
    </section>
  );
}

function DgCard({ t }: { t: Telemetry }) {
  return (
    <section className="mg-card p-5">
      <CardHead
        icon="fa-gas-pump"
        title="DG Status"
        sub={t.dg_running ? "RUNNING" : "IDLE"}
      />
      <KvGrid
        rows={[
          ["Runtime today", `${t.dg_runtime_today_min} min`],
          [
            "Runtime MTD",
            `${Math.floor(t.dg_runtime_mtd_min / 60)}h ${t.dg_runtime_mtd_min % 60} min`,
          ],
        ]}
      />
    </section>
  );
}

function KvGrid({ rows }: { rows: [string, React.ReactNode][] }) {
  return (
    <dl className="mt-3 grid grid-cols-[1fr_auto] gap-x-3 gap-y-1.5 text-sm">
      {rows.map(([k, v]) => (
        <div key={k} className="contents">
          <dt className="text-[--color-text-dim]">{k}</dt>
          <dd className="mono font-semibold text-[--color-text]">{v}</dd>
        </div>
      ))}
    </dl>
  );
}

/* ------- Device strip ------- */
type Device = {
  id: string;
  type: "grid" | "solar" | "bess" | "dg";
  title: string;
  model: string;
  value: string;
  live: boolean;
  detail: { k: string; v: string }[];
};

function buildDevices(t: Telemetry): Device[] {
  return [
    {
      id: "m1",
      type: "grid",
      title: "Grid Meter",
      model: "Selec MFM384",
      value: `${fmt(t.grid_kw, 0)} kW`,
      live: true,
      detail: [
        { k: "Phase A Voltage", v: "232.4 V" },
        { k: "Phase B Voltage", v: "231.7 V" },
        { k: "Phase C Voltage", v: "230.9 V" },
        { k: "Phase A Current", v: "812 A" },
        { k: "Power Factor", v: "0.94" },
        { k: "Frequency", v: "49.98 Hz" },
      ],
    },
    {
      id: "s1",
      type: "solar",
      title: "Solar PV",
      model: "SMA Sunny 50kW",
      value: `${fmt(t.solar_kw, 0)} kW`,
      live: true,
      detail: [
        { k: "AC Power", v: `${fmt(t.solar_kw, 1)} kW` },
        { k: "DC Voltage", v: "612.3 V" },
        { k: "DC Current", v: "402.0 A" },
        { k: "Inverter Temp", v: "47.2 °C" },
        { k: "Yield Today", v: "1,420 kWh" },
      ],
    },
    {
      id: "b1",
      type: "bess",
      title: "BESS Stack",
      model: "BYD 200 kWh",
      value: `SOC ${fmt(t.soc, 0)}%`,
      live: true,
      detail: [
        { k: "SOC", v: `${fmt(t.soc, 0)} %` },
        { k: "Pack Voltage", v: "812.4 V" },
        { k: "Pack Current", v: "110.5 A" },
        { k: "Cell Min/Max", v: "3.30 / 3.34 V" },
        { k: "Temperature", v: "32.4 °C" },
      ],
    },
    {
      id: "dg1",
      type: "dg",
      title: "Diesel Genset",
      model: "Cummins 320 kVA",
      value: t.dg_running ? "RUNNING" : "IDLE",
      live: true,
      detail: [
        { k: "Runtime today", v: `${t.dg_runtime_today_min} min` },
        { k: "Runtime MTD", v: `${t.dg_runtime_mtd_min} min` },
        { k: "Fuel level", v: "78 %" },
      ],
    },
  ];
}

function DeviceStrip({
  devices,
  onOpen,
}: {
  devices: Device[];
  onOpen: (d: Device) => void;
}) {
  const iconMap = {
    grid: "fa-plug",
    solar: "fa-sun",
    bess: "fa-battery-half",
    dg: "fa-gas-pump",
  } as const;
  const colorMap = {
    grid: "text-[--color-cyan]",
    solar: "text-[--color-yellow]",
    bess: "text-[--color-purple]",
    dg: "text-[--color-warn]",
  } as const;
  return (
    <div className="mg-scroll flex gap-3 overflow-x-auto pb-2">
      {devices.map((d) => (
        <button
          key={d.id}
          onClick={() => onOpen(d)}
          className="mg-card flex w-[210px] shrink-0 flex-col items-start gap-1 p-3.5 text-left transition hover:-translate-y-px hover:border-[--color-border-strong]"
        >
          <div className="flex items-center gap-2 text-sm">
            <i className={`fa-solid ${iconMap[d.type]} ${colorMap[d.type]}`} />
            <span className="font-medium">{d.title}</span>
          </div>
          <div className="mono text-[11px] text-[--color-label]">{d.model}</div>
          <div className="mono text-lg font-bold text-[--color-text]">
            {d.value}
          </div>
          <div className="flex items-center gap-1.5 text-xs text-[--color-text-dim]">
            <span
              className={`h-1.5 w-1.5 rounded-full ${
                d.live ? "bg-[--color-good]" : "bg-[--color-bad]"
              }`}
            />
            {d.live ? "Live" : "Offline"}
          </div>
        </button>
      ))}
    </div>
  );
}

/* ------- Drawer ------- */
function Drawer({ device, onClose }: { device: Device | null; onClose: () => void }) {
  if (!device) return null;
  return (
    <>
      <div
        className="fixed inset-0 z-40 bg-black/55 backdrop-blur-sm"
        onClick={onClose}
        aria-hidden
      />
      <aside className="fixed inset-y-0 right-0 z-50 flex w-full max-w-[420px] flex-col border-l border-[--color-border] bg-[--color-surface]">
        <header className="flex items-center justify-between border-b border-[--color-border] px-5 py-4">
          <div className="flex items-center gap-2 text-sm font-semibold">
            <i className="fa-solid fa-microchip text-[--color-text-dim]" />
            <span>{device.title}</span>
            <span className="mono text-[11px] text-[--color-label]">
              · {device.model}
            </span>
          </div>
          <button
            onClick={onClose}
            aria-label="Close"
            className="grid h-8 w-8 place-items-center rounded-lg border border-[--color-border] text-[--color-text-dim] hover:border-[--color-border-strong] hover:text-[--color-text]"
          >
            <i className="fa-solid fa-xmark" />
          </button>
        </header>
        <div className="overflow-y-auto px-5 py-4">
          <h3 className="mg-label mb-3">Technical detail</h3>
          {device.detail.map((r) => (
            <div
              key={r.k}
              className="flex justify-between border-b border-dashed border-[--color-border] py-2 text-sm"
            >
              <span className="text-[--color-text-dim]">{r.k}</span>
              <span className="mono text-[--color-text]">{r.v}</span>
            </div>
          ))}
        </div>
      </aside>
    </>
  );
}

/* ------------------------------------------------------------------ */
/*  Page                                                               */
/* ------------------------------------------------------------------ */

function MDGuardDashboard() {
  const t = useMockTelemetry();
  const [drawer, setDrawer] = useState<Device | null>(null);
  const devices = buildDevices(t);

  return (
    <div className="min-h-screen">
      <NavBar siteName={t.site_name} fresh={t.telemetry_fresh} />

      <main className="mx-auto max-w-[1600px] px-5 py-5">
        <h1 className="sr-only">MDGuard live dashboard</h1>
        <AlarmBanner t={t} />

        <div className="grid gap-4 lg:grid-cols-[3fr_2fr]">
          <div className="flex flex-col gap-4">
            <DemandHeroCard t={t} />
            <PowerFlowCard t={t} />
          </div>
          <div className="flex flex-col gap-4">
            <MonthlyDemandCard t={t} />
            <BessCard t={t} />
            <TariffCard t={t} />
            <DgCard t={t} />
          </div>
        </div>

        <div className="mt-6 flex items-center justify-between">
          <h2 className="text-xs font-semibold uppercase tracking-wider text-[--color-text]">
            Devices
          </h2>
          <span className="text-xs text-[--color-text-dim]">
            Tap a card for technical detail
          </span>
        </div>
        <div className="mt-2">
          <DeviceStrip devices={devices} onOpen={setDrawer} />
        </div>

        <footer className="mt-8 border-t border-[--color-border] pt-4 text-xs text-[--color-label]">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <span>
              MDGuard by <span className="text-[--color-text]">ForrixGuard</span>{" "}
              · Maximum Demand Intelligence Layer
            </span>
            <span>
              Mode:{" "}
              <span className="text-[--color-warn]">
                {t.control_command_allowed ? "Closed-loop control" : "Advisory only"}
              </span>
            </span>
          </div>
        </footer>
      </main>

      <Drawer device={drawer} onClose={() => setDrawer(null)} />
    </div>
  );
}

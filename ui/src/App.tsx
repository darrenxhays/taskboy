import { useEffect, useState } from "react";
import { Navigate, NavLink, Route, Routes } from "react-router-dom";
import { api, ApiError, type Me } from "./api";
import { BrandingContext } from "./branding";
import { StreamContext, useStream, useStreamSource } from "./stream";
import { MissionControl } from "./pages/MissionControl";
import { Tasks } from "./pages/Tasks";
import { TaskDetailPage } from "./pages/TaskDetail";
import { FeedbackPage } from "./pages/Feedback";
import { Issues } from "./pages/Issues";
import { Scheduler } from "./pages/Scheduler";
import { Memory } from "./pages/Memory";
import { UsagePage } from "./pages/Usage";
import { ConfigPage } from "./pages/Config";

const NAV = [
  { to: "/", label: "Mission Control", icon: "◉" },
  { to: "/tasks", label: "Task Explorer", icon: "☰" },
  { to: "/issues", label: "Issues", icon: "✦" },
  { to: "/scheduler", label: "Scheduler", icon: "⏱" },
  { to: "/memory", label: "Memory", icon: "▤" },
  { to: "/usage", label: "Usage", icon: "▁▃▆" },
  { to: "/config", label: "Config", icon: "⚙" },
];

function LogoMark({ me, className }: { me: Me | null; className: string }) {
  // the agent's picture once /api/me has resolved; its initial while loading or if the image fails to load
  const [broken, setBroken] = useState(false);
  const botName = me?.bot_name || "Agent";
  const url = me?.agent_avatar_url;
  if (url && !broken) {
    return <img src={url} alt={botName} className={`${className} shrink-0 rounded-lg object-cover`} onError={() => setBroken(true)} />;
  }
  return (
    <span className={`${className} flex shrink-0 items-center justify-center rounded-lg font-black`} style={{ background: "var(--accent)", color: "#0a0f16" }}>
      {botName.charAt(0).toUpperCase()}
    </span>
  );
}

function Sidebar({ me, open, onClose }: { me: Me | null; open: boolean; onClose: () => void }) {
  const { connected, counts } = useStream();
  const active = counts ? (counts.running ?? 0) : 0;
  const queued = counts ? (counts.queued ?? 0) + (counts.received ?? 0) : 0;
  const botName = me?.bot_name || "Agent";
  return (
    <>
      {/* backdrop: tap to close the drawer on mobile */}
      <div className={`fixed inset-0 z-30 bg-black/60 transition-opacity duration-200 md:hidden ${open ? "opacity-100" : "pointer-events-none opacity-0"}`} onClick={onClose} aria-hidden="true" />
      <aside
        className={`fixed inset-y-0 left-0 z-40 flex w-64 max-w-[85vw] shrink-0 flex-col border-r px-4 py-5 transition-transform duration-200 md:static md:z-auto md:h-dvh md:w-56 md:translate-x-0 md:transition-none ${open ? "translate-x-0" : "-translate-x-full"}`}
        style={{ borderColor: "var(--hairline)", background: "var(--surface-1)" }}
      >
        <div className="mb-8 flex items-center gap-2.5">
          <LogoMark me={me} className="h-8 w-8 text-[15px]" />
          <div>
            <div className="text-[14px] font-bold tracking-wide">{botName.toUpperCase()}</div>
            <div className="text-[9px] font-semibold tracking-[0.22em]" style={{ color: "var(--text-muted)" }}>
              MISSION CONTROL
            </div>
          </div>
          <button onClick={onClose} aria-label="close menu" className="ml-auto flex h-8 w-8 items-center justify-center rounded-md text-[14px] md:hidden" style={{ color: "var(--text-muted)" }}>
            ✕
          </button>
        </div>
        <nav className="space-y-1">
          {NAV.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.to === "/"}
              onClick={onClose}
              className={({ isActive }) => `flex items-center gap-2.5 rounded-md px-3 py-2 text-[13px] font-medium transition-colors ${isActive ? "font-semibold" : ""}`}
              style={({ isActive }) => ({
                color: isActive ? "var(--accent)" : "var(--text-secondary)",
                background: isActive ? "var(--accent-dim)" : "transparent",
              })}
            >
              <span className="w-8 shrink-0 text-center text-[11px]">{item.icon}</span>
              {item.label}
            </NavLink>
          ))}
        </nav>
        <div className="mt-auto space-y-3">
          <div className="rounded-md border p-3" style={{ borderColor: "var(--hairline)" }}>
            <div className="panel-title mb-2">Swarm</div>
            <div className="flex items-center justify-between text-[12px]" style={{ color: "var(--text-secondary)" }}>
              <span>active</span>
              <span className="tnum font-semibold" style={{ color: "var(--accent)" }}>
                {active}
              </span>
            </div>
            <div className="flex items-center justify-between text-[12px]" style={{ color: "var(--text-secondary)" }}>
              <span>queued</span>
              <span className="tnum font-semibold" style={{ color: "var(--status-warning)" }}>
                {queued}
              </span>
            </div>
          </div>
          <div className="flex items-center gap-2 text-[11px]" style={{ color: "var(--text-muted)" }}>
            <span className={`h-1.5 w-1.5 rounded-full ${connected ? "glow-dot" : ""}`} style={{ background: connected ? "var(--status-good)" : "var(--status-critical)", color: connected ? "var(--status-good)" : "var(--status-critical)" }} />
            {connected ? "LIVE UPLINK" : "RECONNECTING…"}
          </div>
          {me && (
            <div className="truncate text-[11px]" style={{ color: "var(--text-muted)" }} title={me.email}>
              {me.email}
              {me.admin && (
                <span className="ml-1.5 rounded border px-1 py-px text-[9px] font-bold tracking-wider" style={{ borderColor: "var(--accent)", color: "var(--accent)" }}>
                  ADMIN
                </span>
              )}
            </div>
          )}
        </div>
      </aside>
    </>
  );
}

function MobileTopBar({ me, onOpen }: { me: Me | null; onOpen: () => void }) {
  const { connected } = useStream();
  const botName = me?.bot_name || "Agent";
  return (
    <header className="flex shrink-0 items-center gap-3 border-b px-3 py-2.5 md:hidden" style={{ borderColor: "var(--hairline)", background: "var(--surface-1)" }}>
      <button onClick={onOpen} aria-label="open menu" className="flex h-9 w-9 items-center justify-center rounded-md border text-[16px]" style={{ borderColor: "var(--hairline-strong)", color: "var(--text-secondary)" }}>
        ☰
      </button>
      <LogoMark me={me} className="h-7 w-7 text-[13px]" />
      <span className="text-[13px] font-bold tracking-wide">
        {botName.toUpperCase()}
        <span className="ml-2 text-[9px] font-semibold tracking-[0.22em]" style={{ color: "var(--text-muted)" }}>
          MISSION CONTROL
        </span>
      </span>
      <span className={`ml-auto h-1.5 w-1.5 rounded-full ${connected ? "glow-dot" : ""}`} style={{ background: connected ? "var(--status-good)" : "var(--status-critical)", color: connected ? "var(--status-good)" : "var(--status-critical)" }} />
    </header>
  );
}

function AccessDenied({ detail }: { detail: string }) {
  return (
    <div className="flex h-dvh w-full items-center justify-center px-4">
      <div className="panel max-w-md p-8 text-center">
        <div className="mb-2 text-[15px] font-bold" style={{ color: "var(--status-critical)" }}>
          ACCESS DENIED
        </div>
        <p className="text-[13px]" style={{ color: "var(--text-secondary)" }}>
          {detail}
        </p>
      </div>
    </div>
  );
}

export default function App() {
  const stream = useStreamSource();
  const [me, setMe] = useState<Me | null>(null);
  const [authError, setAuthError] = useState<string | null>(null);
  const [navOpen, setNavOpen] = useState(false);

  useEffect(() => {
    api
      .me()
      .then((viewer) => {
        setMe(viewer);
        if (viewer.bot_name) document.title = `${viewer.bot_name} — Mission Control`;
      })
      .catch((e: ApiError) => setAuthError(e.message || "sign-in required"));
  }, []);

  if (authError) return <AccessDenied detail={authError} />;

  const branding = { agentAvatarUrl: me?.agent_avatar_url ?? null, reviewerAvatarUrl: me?.reviewer_avatar_url ?? null };

  return (
    <StreamContext.Provider value={stream}>
      <BrandingContext.Provider value={branding}>
        <div className="md:flex">
          <Sidebar me={me} open={navOpen} onClose={() => setNavOpen(false)} />
          <div className="flex h-dvh flex-col md:min-w-0 md:flex-1">
            <MobileTopBar me={me} onOpen={() => setNavOpen(true)} />
            <main className="min-h-0 flex-1 overflow-y-auto px-3 py-4 sm:px-6 sm:py-6">
              <Routes>
                <Route path="/" element={<MissionControl />} />
                <Route path="/tasks" element={<Tasks />} />
                <Route path="/tasks/:taskId" element={<TaskDetailPage admin={me?.admin ?? false} />} />
                <Route path="/tasks/:taskId/feedback" element={<FeedbackPage email={me?.email ?? null} />} />
                <Route path="/issues" element={<Issues admin={me?.admin ?? false} email={me?.email ?? null} />} />
                <Route path="/improvements" element={<Navigate to="/issues" replace />} />
                <Route path="/scheduler" element={<Scheduler admin={me?.admin ?? false} />} />
                <Route path="/memory" element={<Memory />} />
                <Route path="/memory/:taskId" element={<Memory />} />
                <Route path="/usage" element={<UsagePage />} />
                <Route path="/config" element={<ConfigPage admin={me?.admin ?? false} />} />
              </Routes>
            </main>
          </div>
        </div>
      </BrandingContext.Provider>
    </StreamContext.Provider>
  );
}

import { useQuery } from '@tanstack/react-query';
import { useEffect, useState } from 'react';
import { Link, NavLink, Outlet } from 'react-router';
import { ApiError, api } from '../lib/api/client';
import { formatTime } from '../lib/format';

type Theme = 'system' | 'light' | 'dark';
const THEME_KEY = 'tasklane.theme';

function readTheme(): Theme {
  try {
    const stored = localStorage.getItem(THEME_KEY);
    return stored === 'light' || stored === 'dark' ? stored : 'system';
  } catch {
    return 'system';
  }
}

function ThemeSelect() {
  const [theme, setTheme] = useState<Theme>(readTheme);
  useEffect(() => {
    const root = document.documentElement;
    if (theme === 'system') delete root.dataset.theme;
    else root.dataset.theme = theme;
    try {
      if (theme === 'system') localStorage.removeItem(THEME_KEY);
      else localStorage.setItem(THEME_KEY, theme);
    } catch {
      // storage blocked: the choice lasts for this page only
    }
  }, [theme]);
  return (
    <label className="theme-select">
      <span className="visually-hidden">Color theme</span>
      <select value={theme} onChange={(e) => setTheme(e.target.value as Theme)}>
        <option value="system">System theme</option>
        <option value="light">Light theme</option>
        <option value="dark">Dark theme</option>
      </select>
    </label>
  );
}

/** "Is the system healthy?" in the top bar, from GET /health/ready every 5 s. */
function useHealth() {
  return useQuery({ queryKey: ['health'], queryFn: api.ready, refetchInterval: 5000, retry: false });
}

function HealthIndicator({ health }: { health: ReturnType<typeof useHealth> }) {
  let state: 'ok' | 'degraded' | 'down' | 'unknown' = 'unknown';
  let text = 'Checking the API';
  if (health.isError) {
    state = 'down';
    text = 'API unreachable';
  } else if (health.data) {
    if (health.data.checks.database !== 'ok') {
      state = 'down';
      text = 'Database unavailable';
    } else if (health.data.checks.queue !== 'ok') {
      state = 'degraded';
      text = 'Queue unavailable';
    } else {
      state = 'ok';
      text = 'API ready';
    }
  }
  return (
    <span className="health" data-state={state} role="status">
      <span className="health-dot" aria-hidden="true" />
      {text}
    </span>
  );
}

function StaleBanner({ health }: { health: ReturnType<typeof useHealth> }) {
  const unreachable = health.isError && health.error instanceof ApiError;
  const databaseDown = health.data?.checks.database === 'unavailable';
  if (!unreachable && !databaseDown) return null;
  const since = health.dataUpdatedAt ? formatTime(new Date(health.dataUpdatedAt).toISOString()) : null;
  return (
    <div className="banner" role="alert">
      {unreachable
        ? "Can't reach the Tasklane API. Data on this page is the last known state"
        : 'The API is up but its database is unavailable. Data on this page is the last known state'}
      {since ? ` (API last answered at ${since})` : ''}. Retrying every few seconds.
    </div>
  );
}

export function AppShell() {
  const health = useHealth();
  return (
    <div className="shell">
      <a className="skip-link" href="#main">
        Skip to content
      </a>
      <header className="topbar">
        <Link to="/" className="wordmark">
          Tasklane
        </Link>
        <nav aria-label="Main">
          <NavLink to="/" end>
            Overview
          </NavLink>
          <NavLink to="/jobs" end>
            Jobs
          </NavLink>
        </nav>
        <div className="topbar-end">
          <HealthIndicator health={health} />
          <ThemeSelect />
          <Link to="/jobs/new" className="button" data-variant="primary">
            New job
          </Link>
        </div>
      </header>
      <StaleBanner health={health} />
      <main id="main" className="page" tabIndex={-1}>
        <Outlet />
      </main>
    </div>
  );
}

export function NotFoundPage() {
  return (
    <>
      <h1>Page not found</h1>
      <p className="muted">There is no page at this address.</p>
      <p>
        <Link to="/">Go to the overview</Link>
      </p>
    </>
  );
}

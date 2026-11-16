/**
 * HD2Lab theme helper — Soft Iris Light (default) / Navy & Lavender Dark.
 * The choice is shared across every HD2Lab surface through the `hd2-theme` key.
 */
export type Theme = 'light' | 'dark';

export const THEME_KEY = 'hd2-theme';
const APP_THEME_KEY = 'agcore_demo_theme';

function readKey(key: string): Theme | null {
  try {
    const value = localStorage.getItem(key);
    return value === 'dark' || value === 'light' ? value : null;
  } catch {
    return null;
  }
}

export function readTheme(): Theme {
  return readKey(THEME_KEY) ?? readKey(APP_THEME_KEY) ?? 'light';
}

export function applyTheme(theme: Theme): void {
  const root = document.documentElement;
  root.setAttribute('data-theme', theme);
  root.style.colorScheme = theme;
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute('content', theme === 'dark' ? '#0B1020' : '#FAFAFD');
}

export function saveTheme(theme: Theme): void {
  try {
    localStorage.setItem(THEME_KEY, theme);
    localStorage.setItem(APP_THEME_KEY, theme);
  } catch {
    /* Storage may be unavailable; the session theme still applies. */
  }
}

/** Applies the stored theme right away; call once before rendering. */
export function initTheme(): Theme {
  const theme = readTheme();
  applyTheme(theme);
  return theme;
}

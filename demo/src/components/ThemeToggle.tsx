import { useEffect, useState } from 'react';
import { applyTheme, readTheme, saveTheme, THEME_KEY, type Theme } from '../theme';

/**
 * Floating Light/Dark toggle pinned to the bottom-right corner.
 * One monochrome sun/moon SVG — the same mark Web-HQ draws — with no text label.
 * Mirrors the shared `hd2-theme` choice used by every HD2Lab surface.
 */
export function ThemeToggle() {
  const [theme, setTheme] = useState<Theme>(() => readTheme());

  useEffect(() => {
    applyTheme(theme);
    saveTheme(theme);
  }, [theme]);

  useEffect(() => {
    const onStorage = (event: StorageEvent) => {
      if (event.key !== THEME_KEY) return;
      if (event.newValue === 'dark' || event.newValue === 'light') {
        setTheme(event.newValue);
      }
    };
    window.addEventListener('storage', onStorage);
    return () => window.removeEventListener('storage', onStorage);
  }, []);

  const next: Theme = theme === 'dark' ? 'light' : 'dark';
  const label = next === 'dark' ? 'Switch to dark theme' : 'Switch to light theme';

  return (
    <button
      type="button"
      id="themeFab"
      className="theme-fab"
      onClick={() => setTheme(next)}
      aria-label={label}
      aria-pressed={theme === 'dark'}
      title={label}
    >
      <svg className="theme-ic" data-icon="sun" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
        <circle cx="12" cy="12" r="4.1" />
        <path d="M12 2.7v2.3M12 19v2.3M2.7 12h2.3M19 12h2.3M5.4 5.4l1.6 1.6M17 17l1.6 1.6M18.6 5.4L17 7M7 17l-1.6 1.6" />
      </svg>
      <svg className="theme-ic" data-icon="moon" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
        <path d="M20.4 14.4A8.6 8.6 0 0 1 9.6 3.6a8.6 8.6 0 1 0 10.8 10.8z" />
      </svg>
    </button>
  );
}

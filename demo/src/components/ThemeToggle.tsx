import { useEffect, useState } from 'react';
import { applyTheme, readTheme, saveTheme, THEME_KEY, type Theme } from '../theme';

/**
 * Floating Light/Dark toggle pinned to the bottom-right corner.
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
  const label = next === 'dark' ? 'Dark' : 'Light';

  return (
    <button
      type="button"
      id="themeFab"
      className="theme-fab"
      onClick={() => setTheme(next)}
      aria-label={`Switch to ${label.toLowerCase()} theme`}
      aria-pressed={theme === 'dark'}
      title={`Switch to ${label.toLowerCase()} theme`}
    >
      <span className="theme-fab-icon" aria-hidden="true">
        {theme === 'dark' ? '☀️' : '🌙'}
      </span>
      <span className="theme-fab-label">{label}</span>
    </button>
  );
}

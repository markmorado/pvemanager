import { Outlet } from 'react-router';
import { Monitor } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { useThemeStore } from '@/stores/theme-store';
// WebP (~20 KB each) instead of the original 1.2 MB PNGs — the login page is
// the first thing every user loads.
import authBgDark from '@/assets/auth/auth-bg-dark.webp';
import authBgLight from '@/assets/auth/auth-bg-light.webp';

function LanguageSwitcher() {
  const { i18n } = useTranslation();
  const lang = i18n.language?.startsWith('ru') ? 'ru' : 'en';

  return (
    <select
      value={lang}
      onChange={(e) => i18n.changeLanguage(e.target.value)}
      aria-label="Language"
      className="cursor-pointer rounded-md bg-transparent py-1 pr-1 text-sm text-muted-foreground outline-none transition-colors hover:text-foreground focus-visible:text-foreground"
    >
      <option value="ru">Русский</option>
      <option value="en">English</option>
    </select>
  );
}

export default function AuthLayout() {
  const year = new Date().getFullYear();
  const theme = useThemeStore((s) => s.theme);
  const authBg = theme === 'light' ? authBgLight : authBgDark;

  return (
    <div className="grid min-h-dvh bg-background lg:grid-cols-[1.4fr_1fr]">
      {/* Левая декоративная панель со сгенерированным фоном */}
      <div className="relative hidden overflow-hidden bg-muted lg:block">
        <img
          src={authBg}
          alt=""
          aria-hidden="true"
          decoding="async"
          className="absolute inset-0 h-full w-full object-cover"
        />
        {/* Плавный бесшовный переход к панели формы (в обеих темах) */}
        <div className="absolute inset-0 bg-gradient-to-r from-transparent via-transparent to-background/70" />
      </div>

      {/* Правая панель с формой */}
      <div className="flex min-h-dvh flex-col px-6 py-8 sm:px-12">
        {/* Логотип */}
        <div className="flex items-center gap-3">
          <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-primary">
            <Monitor className="h-5 w-5 text-primary-foreground" />
          </div>
          <span className="text-lg font-semibold">PVEmanager</span>
        </div>

        {/* Форма по центру */}
        <div className="flex flex-1 items-center">
          <div className="w-full max-w-sm">
            <Outlet />
          </div>
        </div>

        {/* Футер */}
        <div className="flex items-center gap-4 text-sm text-muted-foreground">
          <LanguageSwitcher />
          <span>PVEmanager © {year}</span>
        </div>
      </div>
    </div>
  );
}

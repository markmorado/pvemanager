import i18n, { type BackendModule, type CallbackError, type ReadCallback } from 'i18next';
import { initReactI18next } from 'react-i18next';
import LanguageDetector from 'i18next-browser-languagedetector';

// Each locale is a separate chunk fetched on demand. Both dictionaries used
// to be bundled into the entry (~210 KB), although a session only ever shows
// one language; switching languages now loads the other one when asked for.
const loaders: Record<string, () => Promise<{ default: Record<string, unknown> }>> = {
  en: () => import('@/locales/en.json'),
  ru: () => import('@/locales/ru.json'),
};

const lazyLocales: BackendModule = {
  type: 'backend',
  init() {},
  read(language: string, _namespace: string, callback: ReadCallback) {
    const load = loaders[language];
    if (!load) {
      callback(null, {});
      return;
    }
    load().then(
      (mod) => callback(null, mod.default),
      (err: unknown) => callback(err as CallbackError, false),
    );
  },
};

/** Resolves once the detected language is loaded — render after it (main.tsx). */
export const i18nReady = i18n
  .use(lazyLocales)
  .use(LanguageDetector)
  .use(initReactI18next)
  .init({
    supportedLngs: ['ru', 'en'],
    // 'ru-RU' → 'ru': one dictionary per language, no region variants.
    load: 'languageOnly',
    // ru.json covers every key of en.json (checked in CI), so Russian sessions
    // do not download the English dictionary as a fallback.
    fallbackLng: { ru: [], default: ['en'] },
    interpolation: {
      escapeValue: false,
    },
    detection: {
      order: ['localStorage', 'navigator'],
      lookupLocalStorage: 'pve-lang',
      caches: ['localStorage'],
    },
    // The first render waits for i18nReady; later language switches resolve
    // before 'languageChanged' fires, so nothing needs to suspend.
    react: { useSuspense: false },
  });

export default i18n;

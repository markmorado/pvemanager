import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
// Self-hosted fonts (font-display: swap built in) — no render-blocking
// external Google Fonts request. Family names: "Inter Variable" /
// "JetBrains Mono Variable" (see --font-sans / --font-mono in index.css).
import '@fontsource-variable/inter'
import '@fontsource-variable/jetbrains-mono'
import { i18nReady } from './lib/i18n'
import './index.css'
import App from './App'

// Translations are loaded lazily (see lib/i18n.ts): render once the active
// language is in, so the first paint is not a screen of raw keys. A failed
// load still renders — keys are better than a blank page.
void i18nReady
  .catch(() => undefined)
  .then(() => {
    createRoot(document.getElementById('root')!).render(
      <StrictMode>
        <App />
      </StrictMode>,
    )
  })

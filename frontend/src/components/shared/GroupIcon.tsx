/**
 * Иконка группы ОС-шаблонов.
 *
 * Иконки хранятся строкой: либо эмодзи/текст, либо разметка вида
 * `<i class="fa-brands fa-ubuntu os-icon"></i>`. Раньше строка вставлялась через
 * dangerouslySetInnerHTML, а её может задать любой пользователь с template:manage
 * — это был stored XSS против всех, кто открывает шаблоны (токен сессии лежит в
 * браузере). Теперь из разметки берётся только тег i/span и его классы.
 */
const TAG_RE = /^\s*<(i|span)\s+class="([\w\s-]*)"\s*>\s*<\/\1>\s*$/;

export function GroupIcon({ icon, className = 'text-lg' }: { icon: string; className?: string }) {
  const m = TAG_RE.exec(icon);
  if (m) {
    const Tag = m[1] as 'i' | 'span';
    return (
      <span className={className}>
        <Tag className={m[2]} aria-hidden="true" />
      </span>
    );
  }
  // Не разметка — показываем как текст (эмодзи). Любые теги React экранирует.
  return <span className={className}>{icon.includes('<') ? '' : icon}</span>;
}

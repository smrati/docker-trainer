/** Shared page chrome. With no build step there is no template engine, so
 *  each page carries an empty <header data-nav> and this fills it in. */

import { markActiveNav, el } from './dom.js';

const LINKS = [
  ['/', 'Home'],
  ['/learn.html', 'Learn'],
  ['/practice.html', 'Practise'],
  ['/review.html', 'Review'],
  ['/playground.html', 'Playground'],
  ['/progress.html', 'Progress'],
];

export function mountNav() {
  const header = document.querySelector('[data-nav]');
  if (!header) return;

  header.className = 'topbar';
  header.append(
    el('div', { class: 'topbar-inner' }, [
      el('a', { class: 'brand', href: '/' }, [
        el('span', { class: 'logo', text: '🐳' }),
        'Docker Trainer',
      ]),
      el(
        'nav',
        { class: 'nav' },
        LINKS.map(([href, label]) => el('a', { href, text: label })),
      ),
    ]),
  );
  markActiveNav();
}

export function mountFooter(note) {
  const footer = document.querySelector('[data-footer]');
  if (!footer) return;

  footer.className = 'page-foot';
  footer.append(
    note ||
      'Your files are built into a throwaway image and run in a container with no network, a memory cap and a process limit. Nothing you write can reach the exercises or your progress.',
  );
}

/** Wire up an element as a tab button toggling a set of panels. */
export function tabs(buttonSelector, panelSelector, onChange) {
  const buttons = [...document.querySelectorAll(buttonSelector)];
  const panels = [...document.querySelectorAll(panelSelector)];

  function select(key) {
    for (const button of buttons) {
      button.classList.toggle('active', button.dataset.tab === key);
    }
    for (const panel of panels) {
      panel.classList.toggle('hidden', panel.dataset.panel !== key);
    }
    if (onChange) onChange(key);
  }

  for (const button of buttons) {
    button.addEventListener('click', () => select(button.dataset.tab));
  }
  const initial = buttons.find((b) => b.classList.contains('active')) || buttons[0];
  if (initial) select(initial.dataset.tab);
  return select;
}

const INDENT = '    ';

/** A textarea that behaves like a source editor.
 *
 *  Two things make a plain textarea unusable for code:
 *
 *  1. **Tab moves the focus.** It has to insert indentation instead.
 *  2. **Shift+Tab does not outdent.** It has to remove one level.
 *
 *  Ctrl/Cmd+Enter runs, Ctrl/Cmd+Shift+Enter submits.
 */
export function enhanceEditor(textarea, onRun, onSubmit) {
  textarea.addEventListener('keydown', (event) => {
    if (event.key === 'Tab') {
      event.preventDefault();
      const { selectionStart: start, selectionEnd: end, value } = textarea;
      if (!event.shiftKey && start === end) {
        textarea.value = `${value.slice(0, start)}${INDENT}${value.slice(end)}`;
        textarea.selectionStart = textarea.selectionEnd = start + INDENT.length;
        return;
      }
      if (event.shiftKey) {
        // Outdent every line the selection touches.
        const before = value.slice(0, start);
        const lineStart = before.lastIndexOf('\n') + 1;
        const block = value.slice(lineStart, end);
        const outdented = block
          .split('\n')
          .map((line) => (line.startsWith(INDENT) ? line.slice(INDENT.length) : line))
          .join('\n');
        textarea.value = `${value.slice(0, lineStart)}${outdented}${value.slice(end)}`;
        textarea.selectionStart = lineStart;
        textarea.selectionEnd = lineStart + outdented.length;
      }
      return;
    }

    if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') {
      event.preventDefault();
      (event.shiftKey ? onSubmit : onRun)?.();
    }
  });
}

/** Replace a node's children with a spinner while work is in flight. */
export function loading(node, message = 'Loading...') {
  node.innerHTML = '';
  node.append(
    el('div', { class: 'empty' }, [el('span', { class: 'spinner' }), ` ${message}`]),
  );
  return node;
}

export function failure(node, error) {
  node.innerHTML = '';
  node.append(
    el('div', { class: 'banner error' }, [
      el('span', { class: 'kind', text: 'error' }),
      error.message || String(error),
    ]),
  );
  return node;
}

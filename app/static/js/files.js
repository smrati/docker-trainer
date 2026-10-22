/** A multi-file editor.
 *
 *  A Docker exercise is not one file. It is a Dockerfile plus the sources it
 *  copies, a `.dockerignore`, sometimes a compose file. So the editor is a
 *  set of tabs over one textarea: the tabs pick the file, the textarea
 *  edits it.
 *
 *  One textarea rather than one per file, deliberately. Several textareas
 *  means several scroll positions, several focus states, and a browser that
 *  has to keep them all in sync; one textarea with a content map has none
 *  of that, and the tab bar is the only extra state.
 */

import { el, mount } from './dom.js';

const INDENT = '    ';

export function fileEditor(container, files, { onChange } = {}) {
  const contents = new Map(Object.entries(files || {}));
  let active = contents.keys().next().value || null;

  const tabs = el('div', { class: 'file-tabs' });
  const textarea = el('textarea', {
    class: 'code-editor',
    spellcheck: 'false',
    autocapitalize: 'off',
    autocomplete: 'off',
    autocorrect: 'off',
  });
  const bar = el('div', { class: 'editor-bar' });
  // The tabs sit between the bar and the textarea: they label what is
  // being edited, so they belong directly above it.
  const shell = el('div', { class: 'editor-shell' }, [bar, tabs, textarea]);

  container.append(shell);

  function show(name) {
    if (!contents.has(name)) return;
    active = name;
    textarea.value = contents.get(name) || '';
    renderTabs();
    textarea.focus();
  }

  function renderTabs() {
    mount(tabs);
    for (const name of contents.keys()) {
      const changed = contents.get(name) !== (files[name] ?? null);
      tabs.append(
        el('button', {
          class: `file-tab${name === active ? ' active' : ''}${changed ? ' changed' : ''}`,
          type: 'button',
          title: name,
          text: name,
          onclick: () => {
            contents.set(active, textarea.value);
            show(name);
          },
        }),
      );
    }
    mount(bar, [
      el('span', { class: 'editor-path', text: active || 'no file' }),
      el('span', { class: 'spacer' }),
    ]);
  }

  textarea.addEventListener('input', () => {
    if (active) {
      contents.set(active, textarea.value);
      renderTabs();
    }
    onChange?.(getFiles());
  });

  // Tab inserts spaces rather than moving the focus, and Shift+Tab
  // outdents. A Dockerfile has no block syntax to continue, so there is
  // nothing to auto-indent -- but the focus behaviour is still wrong in a
  // plain textarea.
  textarea.addEventListener('keydown', (event) => {
    if (event.key === 'Tab') {
      event.preventDefault();
      const { selectionStart: start, selectionEnd: end, value } = textarea;
      if (!event.shiftKey && start === end) {
        textarea.value = `${value.slice(0, start)}${INDENT}${value.slice(end)}`;
        textarea.selectionStart = textarea.selectionEnd = start + INDENT.length;
      } else if (event.shiftKey) {
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
      if (active) contents.set(active, textarea.value);
      onChange?.(getFiles());
    }
  });

  function getFiles() {
    if (active) contents.set(active, textarea.value);
    return Object.fromEntries(contents);
  }

  function setFiles(next) {
    contents.clear();
    for (const [name, content] of Object.entries(next || {})) {
      contents.set(name, content);
    }
    active = contents.keys().next().value || null;
    textarea.value = active ? contents.get(active) : '';
    renderTabs();
  }

  function focus() {
    textarea.focus();
  }

  renderTabs();
  if (active) textarea.value = contents.get(active) || '';

  return { getFiles, setFiles, focus, activeName: () => active, element: shell };
}

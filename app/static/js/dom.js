/** Minimal DOM helpers. Building elements through the DOM rather than
 *  innerHTML keeps result values inert -- a cell containing "<script>" is
 *  text, never markup. */

export function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === 'class') node.className = value;
    else if (key === 'text') node.textContent = value;
    else if (key === 'html') node.innerHTML = value;
    else if (key.startsWith('on') && typeof value === 'function') {
      node.addEventListener(key.slice(2).toLowerCase(), value);
    } else node.setAttribute(key, value);
  }
  for (const child of [].concat(children)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

export function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
  return node;
}

export function mount(node, ...children) {
  clear(node);
  node.append(...children.flat().filter(Boolean));
  return node;
}

export const $ = (selector, scope = document) => scope.querySelector(selector);
export const $$ = (selector, scope = document) => [...scope.querySelectorAll(selector)];

/** Mark the current page in the nav without hardcoding it per page. */
export function markActiveNav() {
  const here = location.pathname.replace(/index\.html$/, '') || '/';
  for (const link of $$('.nav a')) {
    const href = link.getAttribute('href').replace(/index\.html$/, '') || '/';
    if (href === here) link.classList.add('active');
  }
}

/** Status -> label + badge class, shared by every list that shows progress. */
export const STATUS = {
  solved: { label: 'Solved', cls: 'solved' },
  revealed: { label: 'Revealed', cls: 'revealed' },
  unsolved: { label: 'Not started', cls: '' },
};

export function statusBadge(status) {
  const meta = STATUS[status] || STATUS.unsolved;
  return el('span', { class: `badge ${meta.cls}`.trim(), text: meta.label });
}

/** A button that shows a spinner and disables itself while work is in flight. */
export function busy(button, fn) {
  return async (...args) => {
    const label = button.textContent;
    button.disabled = true;
    button.textContent = 'Working...';
    try {
      return await fn(...args);
    } finally {
      button.disabled = false;
      button.textContent = label;
    }
  };
}

export function show(node, visible) {
  node.classList.toggle('hidden', !visible);
}

/** Plain-text rendering of a cell value, distinguishing NULL and empty. */
export function cell(value) {
  if (value === null) return el('span', { class: 'null', text: 'NULL' });
  if (value === '') return el('span', { class: 'null', text: "''" });
  return document.createTextNode(String(value));
}
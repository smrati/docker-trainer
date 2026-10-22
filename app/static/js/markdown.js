/** A small CommonMark subset, enough for the lesson text.
 *
 *  Covers: headings, fenced code, blockquotes, ordered/unordered lists,
 *  tables, hr, paragraphs, and inline code / bold / italic / links.
 *
 *  All output is built from escaped text. The only raw HTML it ever emits is
 *  the tags below, so curriculum markdown cannot inject markup -- a lesson
 *  containing `<script>` renders as the literal characters.
 */

const escapeHtml = (text) =>
  String(text).replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c],
  );

/** Inline spans. Code spans are extracted first so their contents are not
 *  re-processed for emphasis. The placeholder is a NUL character, written
 *  as an escape so this file stays plain text. */
function renderInline(text) {
  const codes = [];
  let working = text.replace(/`([^`]+)`/g, (_, code) => {
    codes.push(code);
    return `\u0000${codes.length - 1}\u0000`;
  });

  working = escapeHtml(working)
    .replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (_, label, href) =>
      /^(https?:|#|\/)/.test(href)
        ? `<a href="${href}">${label}</a>`
        : label,
    )
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/(^|[\s(])\*([^*\n]+)\*/g, '$1<em>$2</em>')
    .replace(/(^|[\s(])_([^_\n]+)_/g, '$1<em>$2</em>');

  return working.replace(/\u0000(\d+)\u0000/g, (_, i) => `<code>${escapeHtml(codes[Number(i)])}</code>`);
}

export function renderMarkdown(source) {
  if (!source) return '';
  const lines = String(source).replace(/\r\n/g, '\n').split('\n');
  const out = [];
  let i = 0;

  const flushParagraph = (buffer) => {
    if (buffer.length) out.push(`<p>${renderInline(buffer.join(' '))}</p>`);
    buffer.length = 0;
  };
  const paragraph = [];

  while (i < lines.length) {
    const line = lines[i];

    // fenced code
    const fence = line.match(/^```\s*(\w*)\s*$/);
    if (fence) {
      flushParagraph(paragraph);
      const lang = fence[1] ? ` class="language-${escapeHtml(fence[1])}"` : '';
      const body = [];
      i += 1;
      while (i < lines.length && !/^```\s*$/.test(lines[i])) body.push(lines[i++]);
      i += 1;
      out.push(`<pre><code${lang}>${escapeHtml(body.join('\n'))}</code></pre>`);
      continue;
    }

    // blank
    if (!line.trim()) {
      flushParagraph(paragraph);
      i += 1;
      continue;
    }

    // heading
    const heading = line.match(/^(#{1,6})\s+(.*)$/);
    if (heading) {
      flushParagraph(paragraph);
      const level = heading[1].length;
      out.push(`<h${level}>${renderInline(heading[2].trim())}</h${level}>`);
      i += 1;
      continue;
    }

    // hr
    if (/^(\s*[-*_]){3,}\s*$/.test(line)) {
      flushParagraph(paragraph);
      out.push('<hr>');
      i += 1;
      continue;
    }

    // blockquote (consumes consecutive '>' lines)
    if (/^>\s?/.test(line)) {
      flushParagraph(paragraph);
      const quoted = [];
      while (i < lines.length && (/^>\s?/.test(lines[i]) || (lines[i].trim() && quoted.length))) {
        if (!/^>\s?/.test(lines[i])) {
          // a non-blank line without '>' ends the quote unless it is indented
          if (!/^\s{2,}\S/.test(lines[i])) break;
          quoted.push(lines[i].trim());
        } else {
          quoted.push(lines[i].replace(/^>\s?/, ''));
        }
        i += 1;
      }
      out.push(`<blockquote>${renderMarkdown(quoted.join('\n'))}</blockquote>`);
      continue;
    }

    // table: a header row plus a |---|---| separator
    if (line.includes('|') && i + 1 < lines.length && /^\s*\|?[\s:-]*-[-\s|:]*$/.test(lines[i + 1])) {
      flushParagraph(paragraph);
      const header = splitRow(line);
      i += 2;
      const rows = [];
      while (i < lines.length && lines[i].includes('|') && lines[i].trim()) {
        rows.push(splitRow(lines[i]));
        i += 1;
      }
      const head = header.map((c) => `<th>${renderInline(c)}</th>`).join('');
      const body = rows
        .map((r) => `<tr>${r.map((c) => `<td>${renderInline(c)}</td>`).join('')}</tr>`)
        .join('');
      out.push(`<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`);
      continue;
    }

    // list
    const bullet = line.match(/^(\s*)([-*+]|\d+\.)\s+(.*)$/);
    if (bullet) {
      flushParagraph(paragraph);
      const ordered = /\d/.test(bullet[2]);
      const items = [];
      while (i < lines.length) {
        const item = lines[i].match(/^(\s*)([-*+]|\d+\.)\s+(.*)$/);
        if (!item) {
          // continuation of the previous item
          const cont = lines[i].match(/^\s{2,}(\S.*)$/);
          if (cont && items.length) {
            items[items.length - 1] += ' ' + cont[1].trim();
            i += 1;
            continue;
          }
          break;
        }
        items.push(item[3]);
        i += 1;
      }
      const tag = ordered ? 'ol' : 'ul';
      const html = items.map((t) => `<li>${renderInline(t)}</li>`).join('');
      out.push(`<${tag}>${html}</${tag}>`);
      continue;
    }

    paragraph.push(line.trim());
    i += 1;
  }

  flushParagraph(paragraph);
  return out.join('\n');
}

function splitRow(row) {
  return row
    .trim()
    .replace(/^\||\|$/g, '')
    .split('|')
    .map((c) => c.trim());
}

/** Render into a node, as HTML. */
export function mountMarkdown(node, source) {
  node.innerHTML = renderMarkdown(source);
  return node;
}

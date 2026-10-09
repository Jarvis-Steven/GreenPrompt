/* GreenPrompt markdown renderer — PROPOSAL for the frontend member.

   SAFETY: model output is never assigned to innerHTML. Every piece of text
   reaches the page through document.createTextNode / textContent, so a model
   that returns "<script>alert(1)</script>" or "<img onerror=...>" is displayed
   as literal characters and can never execute.

   No dependencies, no CDN, no build step. Plain DOM only.
*/

const MD_INLINE = /(\*\*[^*]+\*\*|\*[^*]+\*|`[^`]+`)/;

function mdEl(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text; // textContent, never innerHTML
  return node;
}

/* Inline spans: **bold**, *italic*, `code`. Anything else stays literal text. */
function mdInline(target, text) {
  for (const piece of String(text).split(MD_INLINE)) {
    if (!piece) continue;
    if (piece.length > 4 && piece.startsWith('**') && piece.endsWith('**')) {
      target.append(mdEl('strong', '', piece.slice(2, -2)));
    } else if (piece.length > 2 && piece.startsWith('*') && piece.endsWith('*')) {
      target.append(mdEl('em', '', piece.slice(1, -1)));
    } else if (piece.length > 2 && piece.startsWith('`') && piece.endsWith('`')) {
      target.append(mdEl('code', 'md-code-inline', piece.slice(1, -1)));
    } else {
      target.append(document.createTextNode(piece));
    }
  }
  return target;
}

function copyText(value, button) {
  const done = () => {
    const previous = button.textContent;
    button.textContent = 'Copied';
    button.classList.add('copied');
    setTimeout(() => { button.textContent = previous; button.classList.remove('copied'); }, 1500);
  };
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(value).then(done, () => fallbackCopy(value, done));
  } else {
    fallbackCopy(value, done);
  }
}

function fallbackCopy(value, done) {
  const area = document.createElement('textarea');
  area.value = value;
  area.setAttribute('readonly', '');
  area.style.cssText = 'position:fixed;top:-1000px;opacity:0';
  document.body.append(area);
  area.select();
  try { document.execCommand('copy'); done(); } catch (_) { /* clipboard unavailable */ }
  area.remove();
}

/* Fenced code block: header bar with language label + Copy button. */
function mdCodeBlock(language, code) {
  const box = mdEl('div', 'md-code');
  const bar = mdEl('div', 'md-code-bar');
  bar.append(mdEl('span', 'md-code-lang', language || 'code'));

  const button = mdEl('button', 'md-copy', 'Copy');
  button.type = 'button';
  button.setAttribute('aria-label',
    language ? `Copy ${language} code to clipboard` : 'Copy code to clipboard');
  button.addEventListener('click', () => copyText(code, button));
  bar.append(button);

  const pre = mdEl('pre', 'md-pre');
  pre.append(mdEl('code', '', code));   // textContent — never parsed as HTML
  box.append(bar, pre);
  return box;
}

function splitRow(line) {
  return line.replace(/^\s*\|/, '').replace(/\|\s*$/, '').split('|').map(c => c.trim());
}

const isSeparator = (line) => /^\s*\|?[\s:|-]*-[\s:|-]*\|?\s*$/.test(line) && line.includes('-');

/* GitHub-style table; wrapped so it scrolls inside itself on small screens. */
function mdTable(lines) {
  const wrap = mdEl('div', 'md-table-wrap');
  const table = mdEl('table', 'md-table');
  const head = document.createElement('thead');
  const headRow = document.createElement('tr');
  for (const cell of splitRow(lines[0])) headRow.append(mdInline(mdEl('th'), cell));
  head.append(headRow);
  const body = document.createElement('tbody');
  for (const line of lines.slice(2)) {
    const row = document.createElement('tr');
    for (const cell of splitRow(line)) row.append(mdInline(mdEl('td'), cell));
    body.append(row);
  }
  table.append(head, body);
  wrap.append(table);
  return wrap;
}

/* Public entry point: returns a DOM fragment for one markdown string. */
function renderMarkdown(source) {
  const out = document.createDocumentFragment();
  const lines = String(source ?? '').replace(/\r\n/g, '\n').split('\n');
  let i = 0;

  while (i < lines.length) {
    const line = lines[i];

    // Fenced code block
    const fence = line.match(/^\s*```+\s*([\w+#.-]*)\s*$/);
    if (fence) {
      const language = fence[1];
      const code = [];
      i += 1;
      while (i < lines.length && !/^\s*```+\s*$/.test(lines[i])) { code.push(lines[i]); i += 1; }
      i += 1; // closing fence
      out.append(mdCodeBlock(language, code.join('\n')));
      continue;
    }

    if (!line.trim()) { i += 1; continue; }

    // Horizontal rule
    if (/^\s*([-*_])\s*(\1\s*){2,}$/.test(line)) { out.append(mdEl('hr', 'md-hr')); i += 1; continue; }

    // Heading
    const heading = line.match(/^\s*(#{1,4})\s+(.*)$/);
    if (heading) {
      out.append(mdInline(mdEl('h' + (heading[1].length + 2), 'md-h'), heading[2]));
      i += 1;
      continue;
    }

    // Table: a header row followed by a --- separator row
    if (line.includes('|') && i + 1 < lines.length && isSeparator(lines[i + 1])) {
      const block = [line, lines[i + 1]];
      i += 2;
      while (i < lines.length && lines[i].includes('|') && lines[i].trim()) { block.push(lines[i]); i += 1; }
      out.append(mdTable(block));
      continue;
    }

    // Blockquote
    if (/^\s*>\s?/.test(line)) {
      const quote = mdEl('blockquote', 'md-quote');
      const parts = [];
      while (i < lines.length && /^\s*>\s?/.test(lines[i])) { parts.push(lines[i].replace(/^\s*>\s?/, '')); i += 1; }
      mdInline(quote, parts.join(' '));
      out.append(quote);
      continue;
    }

    // Lists
    const bullet = /^\s*[-*+]\s+(.*)$/;
    const numbered = /^\s*\d+[.)]\s+(.*)$/;
    if (bullet.test(line) || numbered.test(line)) {
      const ordered = numbered.test(line);
      const list = mdEl(ordered ? 'ol' : 'ul', 'md-list');
      const pattern = ordered ? numbered : bullet;
      while (i < lines.length && pattern.test(lines[i])) {
        list.append(mdInline(mdEl('li'), lines[i].match(pattern)[1]));
        i += 1;
      }
      out.append(list);
      continue;
    }

    // Paragraph: consecutive plain lines
    const paragraph = [];
    while (i < lines.length && lines[i].trim()
           && !/^\s*```/.test(lines[i])
           && !/^\s*#{1,4}\s/.test(lines[i])
           && !/^\s*>\s?/.test(lines[i])
           && !bullet.test(lines[i]) && !numbered.test(lines[i])
           && !(lines[i].includes('|') && isSeparator(lines[i + 1] || ''))) {
      paragraph.push(lines[i]);
      i += 1;
    }
    if (paragraph.length) out.append(mdInline(mdEl('p', 'md-p'), paragraph.join(' ')));
    else i += 1;
  }
  return out;
}

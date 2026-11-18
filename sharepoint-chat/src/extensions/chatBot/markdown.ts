/**
 * Renders the assistant's replies (a small Markdown subset) as DOM nodes.
 *
 * Security: replies are untrusted text (they quote SharePoint content), so
 * this never uses innerHTML. Every piece of text goes through
 * createTextNode/textContent, and links are created only for https: URLs,
 * so a reply can never inject markup or a javascript: link.
 *
 * Supported: paragraphs, "- "/"* " and "1. " lists (nested by indentation),
 * "#" headings (shown bold), ``` code blocks, **bold**, `code`,
 * [text](https://...) links, and [n] citations, which link to source n.
 */

export type CitationUrl = (sourceNumber: number) => string | undefined;

const INLINE_PATTERN: RegExp = /(\*\*[^*\n]+\*\*|`[^`\n]+`|\[\d+\]|\[[^\]\n]+\]\(https:\/\/[^)\s]+\))/g;
const LIST_ITEM: RegExp = /^(\s*)([-*]|\d+[.)])\s+(.*)$/;
const HEADING: RegExp = /^#{1,6}\s+(.*)$/;

export function safeHttpsUrl(url: string | undefined): string | undefined {
  if (!url) {
    return undefined;
  }
  try {
    const parsed: URL = new URL(url);
    return parsed.protocol === 'https:' ? parsed.href : undefined;
  } catch {
    return undefined;
  }
}

function link(doc: Document, href: string, text: string): HTMLAnchorElement {
  const a: HTMLAnchorElement = doc.createElement('a');
  a.href = href;
  a.textContent = text;
  a.target = '_blank';
  a.rel = 'noopener noreferrer';
  return a;
}

export function appendInline(doc: Document, parent: HTMLElement, text: string, citationUrl: CitationUrl): void {
  let last: number = 0;
  text.replace(INLINE_PATTERN, (token: string, _group: string, offset: number): string => {
    if (offset > last) {
      parent.appendChild(doc.createTextNode(text.slice(last, offset)));
    }
    last = offset + token.length;

    if (token.startsWith('**')) {
      const strong: HTMLElement = doc.createElement('strong');
      strong.textContent = token.slice(2, -2);
      parent.appendChild(strong);
    } else if (token.startsWith('`')) {
      const code: HTMLElement = doc.createElement('code');
      code.textContent = token.slice(1, -1);
      parent.appendChild(code);
    } else if (/^\[\d+\]$/.test(token)) {
      const href: string | undefined = safeHttpsUrl(citationUrl(parseInt(token.slice(1, -1), 10)));
      const sup: HTMLElement = doc.createElement('sup');
      sup.className = 'acb-cite';
      if (href) {
        sup.appendChild(link(doc, href, token));
      } else {
        sup.textContent = token;
      }
      parent.appendChild(sup);
    } else {
      const close: number = token.indexOf('](');
      const href: string | undefined = safeHttpsUrl(token.slice(close + 2, -1));
      const label: string = token.slice(1, close);
      parent.appendChild(href ? link(doc, href, label) : doc.createTextNode(label));
    }
    return token;
  });
  if (last < text.length) {
    parent.appendChild(doc.createTextNode(text.slice(last)));
  }
}

interface IListLevel {
  indent: number;
  list: HTMLElement;
}

export function renderMarkdown(doc: Document, text: string, citationUrl: CitationUrl): DocumentFragment {
  const fragment: DocumentFragment = doc.createDocumentFragment();
  const lines: string[] = text.replace(/\r\n/g, '\n').split('\n');
  let paragraph: string[] = [];
  let lists: IListLevel[] = [];

  const flushParagraph = (): void => {
    if (paragraph.length) {
      const p: HTMLElement = doc.createElement('p');
      paragraph.forEach((line: string, i: number): void => {
        if (i > 0) {
          p.appendChild(doc.createElement('br'));
        }
        appendInline(doc, p, line, citationUrl);
      });
      fragment.appendChild(p);
      paragraph = [];
    }
  };
  const closeLists = (): void => {
    lists = [];
  };

  for (let i: number = 0; i < lines.length; i++) {
    const line: string = lines[i];

    if (line.trim().startsWith('```')) {
      flushParagraph();
      closeLists();
      const code: string[] = [];
      i++;
      while (i < lines.length && !lines[i].trim().startsWith('```')) {
        code.push(lines[i]);
        i++;
      }
      const pre: HTMLElement = doc.createElement('pre');
      const codeEl: HTMLElement = doc.createElement('code');
      codeEl.textContent = code.join('\n');
      pre.appendChild(codeEl);
      fragment.appendChild(pre);
      continue;
    }

    if (!line.trim()) {
      flushParagraph();
      closeLists();
      continue;
    }

    const item: RegExpMatchArray | null = line.match(LIST_ITEM);
    if (item) {
      flushParagraph();
      const indent: number = item[1].replace(/\t/g, '  ').length;
      const ordered: boolean = /\d/.test(item[2]);
      while (lists.length && indent < lists[lists.length - 1].indent) {
        lists.pop();
      }
      const top: IListLevel | undefined = lists[lists.length - 1];
      if (!top || indent > top.indent) {
        const list: HTMLElement = doc.createElement(ordered ? 'ol' : 'ul');
        const parentItem: Element | null = top ? top.list.lastElementChild : null;
        (parentItem || fragment).appendChild(list);
        lists.push({ indent, list });
      }
      const li: HTMLElement = doc.createElement('li');
      appendInline(doc, li, item[3], citationUrl);
      lists[lists.length - 1].list.appendChild(li);
      continue;
    }

    closeLists();
    const heading: RegExpMatchArray | null = line.match(HEADING);
    if (heading) {
      flushParagraph();
      const p: HTMLElement = doc.createElement('p');
      const strong: HTMLElement = doc.createElement('strong');
      appendInline(doc, strong, heading[1], citationUrl);
      p.appendChild(strong);
      fragment.appendChild(p);
      continue;
    }
    paragraph.push(line.trim());
  }
  flushParagraph();
  return fragment;
}

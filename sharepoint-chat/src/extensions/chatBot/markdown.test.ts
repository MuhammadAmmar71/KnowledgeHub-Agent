import { renderMarkdown, safeHttpsUrl } from './markdown';

// eslint-disable-next-line no-script-url -- deliberately testing that script URLs are refused
const URLS: { [n: number]: string } = { 1: 'https://sp.example/guide', 2: 'javascript:alert(1)' };

function render(text: string): HTMLDivElement {
  const div: HTMLDivElement = document.createElement('div');
  div.appendChild(renderMarkdown(document, text, (n: number) => URLS[n]));
  return div;
}

describe('renderMarkdown security', () => {
  it('never turns reply text into markup', () => {
    const div: HTMLDivElement = render('<img src=x onerror="alert(1)"> and <script>alert(2)</script>');
    expect(div.querySelector('img')).toBeNull();
    expect(div.querySelector('script')).toBeNull();
    expect(div.textContent).toContain('<script>alert(2)</script>');
  });

  it('only links https URLs', () => {
    const div: HTMLDivElement = render('See [docs](https://ok.example/a) and [x](javascript:alert(1)) and [2].');
    const links: HTMLAnchorElement[] = Array.from(div.querySelectorAll('a'));
    expect(links.map((a: HTMLAnchorElement) => a.href)).toEqual(['https://ok.example/a']);
    expect(links[0].rel).toBe('noopener noreferrer');
  });

  it('rejects non-https schemes', () => {
    // eslint-disable-next-line no-script-url -- deliberately testing that script URLs are refused
    expect(safeHttpsUrl('javascript:alert(1)')).toBeUndefined();
    expect(safeHttpsUrl('http://insecure.example')).toBeUndefined();
    expect(safeHttpsUrl('data:text/html,hi')).toBeUndefined();
    expect(safeHttpsUrl(undefined)).toBeUndefined();
    expect(safeHttpsUrl('https://sp.example/a b')).toBe('https://sp.example/a%20b');
  });
});

describe('renderMarkdown formatting', () => {
  it('links citations to their source', () => {
    const div: HTMLDivElement = render('Use docker push [1].');
    const cite: HTMLElement = div.querySelector('sup.acb-cite') as HTMLElement;
    expect((cite.querySelector('a') as HTMLAnchorElement).href).toBe('https://sp.example/guide');
    expect(cite.textContent).toBe('[1]');
  });

  it('renders bold, code and nested lists', () => {
    const div: HTMLDivElement = render('Steps:\n\n- **Login**: run `az login`\n  - then wait\n- Push [1]\n\n1. one\n2. two');
    expect(div.querySelector('strong')!.textContent).toBe('Login');
    expect(div.querySelector('code')!.textContent).toBe('az login');
    const ul: HTMLUListElement = div.querySelector('ul') as HTMLUListElement;
    expect(ul.children.length).toBe(2);
    expect(ul.querySelector('li ul li')!.textContent).toBe('then wait');
    expect(div.querySelectorAll('ol li').length).toBe(2);
  });

  it('renders code blocks verbatim', () => {
    const div: HTMLDivElement = render('Run:\n```bash\naz acr login --name **x**\n```');
    expect(div.querySelector('pre code')!.textContent).toBe('az acr login --name **x**');
  });

  it('keeps line breaks inside a paragraph', () => {
    expect(render('line one\nline two').querySelectorAll('p br').length).toBe(1);
  });
});

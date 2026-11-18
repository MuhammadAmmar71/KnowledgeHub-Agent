import { Log } from '@microsoft/sp-core-library';
import { BaseApplicationCustomizer } from '@microsoft/sp-application-base';
import { AadHttpClient, HttpClientResponse } from '@microsoft/sp-http';

import * as strings from 'ChatBotApplicationCustomizerStrings';
import { friendlyError, IChatResponse, IChatSource, IJsonResponse, PostJson, sendChat, ChatApiError } from './chatApi';
import { renderMarkdown, safeHttpsUrl } from './markdown';

const LOG_SOURCE: string = 'ChatBotApplicationCustomizer';
const ROOT_ID: string = 'auximo-chatbot-root';
const STORAGE_KEY: string = 'auximo-knowledge-chat';
const MAX_STORED_MESSAGES: number = 40;
// Must match CHAT_MAX_MESSAGE_CHARS on the backend.
const MAX_MESSAGE_CHARS: number = 2000;

/**
 * Set via ClientSideComponentProperties: config/serve.json when debugging,
 * sharepoint/assets/*.xml (or the Tenant Wide Extensions list) when deployed.
 */
export interface IChatBotApplicationCustomizerProperties {
  /** Backend endpoint, e.g. https://<backend-host>/chat. */
  apiUrl?: string;
  /** Header text shown in the chat panel. */
  title?: string;
  /**
   * Entra resource of the backend API, e.g. api://<client-id>. When set,
   * requests carry the signed-in user's token (AadHttpClient). Leave unset
   * only for local debugging against a dev-mode backend.
   */
  apiResource?: string;
}

interface IStoredMessage {
  kind: 'user' | 'bot' | 'error';
  text: string;
  sources?: IChatSource[];
  note?: string;
}

interface IStoredChat {
  conversationId?: string;
  messages: IStoredMessage[];
}

const CSS: string = `
#${ROOT_ID} { position: fixed; right: 24px; bottom: 24px; z-index: 10000; font-family: "Segoe UI", sans-serif; }
#${ROOT_ID} .acb-bubble { width: 56px; height: 56px; border-radius: 50%; border: none; cursor: pointer;
  background: #0f6cbd; color: #fff; font-size: 26px; box-shadow: 0 4px 12px rgba(0,0,0,.25); }
#${ROOT_ID} .acb-panel { display: none; flex-direction: column; position: absolute; right: 0; bottom: 68px;
  width: 340px; height: 460px; max-width: calc(100vw - 48px); max-height: calc(100vh - 120px); background: #fff;
  border-radius: 8px; box-shadow: 0 8px 24px rgba(0,0,0,.25); overflow: hidden; }
#${ROOT_ID}.acb-open .acb-panel { display: flex; }
#${ROOT_ID} .acb-header { background: #0f6cbd; color: #fff; padding: 12px 14px; font-weight: 600;
  display: flex; justify-content: space-between; align-items: center; gap: 8px; }
#${ROOT_ID} .acb-header-buttons { display: flex; gap: 4px; }
#${ROOT_ID} .acb-icon-btn { background: none; border: none; color: #fff; font-size: 16px; cursor: pointer;
  padding: 2px 6px; border-radius: 4px; }
#${ROOT_ID} .acb-icon-btn:hover { background: rgba(255,255,255,.15); }
#${ROOT_ID} .acb-messages { flex: 1; overflow-y: auto; padding: 12px; background: #f5f5f5; }
#${ROOT_ID} .acb-msg { max-width: 80%; padding: 8px 12px; margin: 6px 0; border-radius: 12px;
  font-size: 14px; line-height: 1.4; word-wrap: break-word; }
#${ROOT_ID} .acb-user, #${ROOT_ID} .acb-error { white-space: pre-wrap; }
#${ROOT_ID} .acb-bot { background: #fff; color: #242424; border: 1px solid #e0e0e0; max-width: 88%; }
#${ROOT_ID} .acb-bot p { margin: 0 0 6px; }
#${ROOT_ID} .acb-bot p:last-child { margin-bottom: 0; }
#${ROOT_ID} .acb-bot ul, #${ROOT_ID} .acb-bot ol { margin: 4px 0 6px; padding-left: 20px; }
#${ROOT_ID} .acb-bot code { background: #f0f0f0; border-radius: 3px; padding: 0 3px; font-size: 13px; }
#${ROOT_ID} .acb-bot pre { background: #f0f0f0; border-radius: 6px; padding: 6px 8px; overflow-x: auto; margin: 4px 0; }
#${ROOT_ID} .acb-bot pre code { background: none; padding: 0; }
#${ROOT_ID} .acb-bot a { color: #0f6cbd; }
#${ROOT_ID} .acb-cite { font-size: 11px; }
#${ROOT_ID} .acb-cite a { text-decoration: none; }
#${ROOT_ID} .acb-note { font-size: 12px; color: #616161; font-style: italic; margin-bottom: 6px; }
#${ROOT_ID} .acb-sources { border-top: 1px solid #e0e0e0; margin-top: 8px; padding-top: 6px; font-size: 12px; }
#${ROOT_ID} .acb-sources-title { font-weight: 600; color: #616161; margin-bottom: 2px; }
#${ROOT_ID} .acb-sources ol { margin: 0; padding-left: 0; list-style: none; }
#${ROOT_ID} .acb-sources li { margin: 2px 0; }
#${ROOT_ID} .acb-user { background: #0f6cbd; color: #fff; margin-left: auto; }
#${ROOT_ID} .acb-error { background: #fde7e9; color: #a4262c; border: 1px solid #f1bbbc; }
#${ROOT_ID} .acb-typing span { display: inline-block; width: 6px; height: 6px; margin: 0 2px; border-radius: 50%;
  background: #8a8a8a; animation: acb-blink 1.2s infinite ease-in-out; }
#${ROOT_ID} .acb-typing span:nth-child(2) { animation-delay: .2s; }
#${ROOT_ID} .acb-typing span:nth-child(3) { animation-delay: .4s; }
@keyframes acb-blink { 0%, 80%, 100% { opacity: .25; } 40% { opacity: 1; } }
#${ROOT_ID} .acb-form { display: flex; border-top: 1px solid #e0e0e0; }
#${ROOT_ID} .acb-input { flex: 1; border: none; padding: 12px; font-size: 14px; outline: none; }
#${ROOT_ID} .acb-send { border: none; background: #0f6cbd; color: #fff; padding: 0 16px; cursor: pointer; }
#${ROOT_ID} .acb-send:disabled { background: #a0a0a0; cursor: default; }
`;

/** Floating Knowledge Hub chat bubble rendered on every page of the site. */
export default class ChatBotApplicationCustomizer
  extends BaseApplicationCustomizer<IChatBotApplicationCustomizerProperties> {

  private _root!: HTMLDivElement;
  private _messages!: HTMLDivElement;
  private _input!: HTMLInputElement;
  private _send!: HTMLButtonElement;
  private _chat: IStoredChat = { messages: [] };
  private _post: PostJson | undefined;

  public onInit(): Promise<void> {
    Log.info(LOG_SOURCE, `Initialized ${strings.Title}`);
    // The customizer survives SharePoint's in-site (SPA) navigation, so render only once.
    if (!document.getElementById(ROOT_ID)) {
      this._render();
    }
    return Promise.resolve();
  }

  private get _apiUrl(): string {
    return this.properties.apiUrl || 'http://localhost:8000/chat';
  }

  private async _getPost(): Promise<PostJson> {
    if (this._post) {
      return this._post;
    }
    const resource: string | undefined = this.properties.apiResource;
    if (resource) {
      // Production: AadHttpClient gets an Entra token for the signed-in user
      // and the API, and attaches it as "Authorization: Bearer ...". Requires
      // the package's API permission request to be approved by an admin.
      const client: AadHttpClient = await this.context.aadHttpClientFactory.getClient(resource);
      this._post = async (url: string, body: string): Promise<IJsonResponse> => {
        const res: HttpClientResponse = await client.post(url, AadHttpClient.configurations.v1, {
          headers: { 'Content-Type': 'application/json' },
          body
        });
        return { status: res.status, json: (): Promise<unknown> => res.json() };
      };
    } else {
      this._post = async (url: string, body: string): Promise<IJsonResponse> => {
        const res: Response = await fetch(url, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body
        });
        return { status: res.status, json: (): Promise<unknown> => res.json() };
      };
    }
    return this._post;
  }

  private _render(): void {
    const style: HTMLStyleElement = document.createElement('style');
    style.textContent = CSS;
    document.head.appendChild(style);

    const root: HTMLDivElement = document.createElement('div');
    root.id = ROOT_ID;
    // Static markup only; nothing user- or server-provided is ever put in innerHTML.
    root.innerHTML = `
      <div class="acb-panel" role="dialog" aria-label="Chat">
        <div class="acb-header">
          <span class="acb-title"></span>
          <span class="acb-header-buttons">
            <button class="acb-icon-btn acb-new" aria-label="New chat" title="New chat">↺</button>
            <button class="acb-icon-btn acb-close" aria-label="Close" title="Close">✕</button>
          </span>
        </div>
        <div class="acb-messages" aria-live="polite"></div>
        <form class="acb-form">
          <input class="acb-input" type="text" placeholder="Ask a question…" autocomplete="off" />
          <button class="acb-send" type="submit">Send</button>
        </form>
      </div>
      <button class="acb-bubble" aria-label="Open chat">💬</button>`;
    document.body.appendChild(root);
    this._root = root;

    (root.querySelector('.acb-title') as HTMLElement).textContent = this.properties.title || 'Knowledge Hub Assistant';
    this._messages = root.querySelector('.acb-messages') as HTMLDivElement;
    this._input = root.querySelector('.acb-input') as HTMLInputElement;
    this._input.maxLength = MAX_MESSAGE_CHARS;
    this._send = root.querySelector('.acb-send') as HTMLButtonElement;

    const toggle = (): void => {
      root.classList.toggle('acb-open');
      if (root.classList.contains('acb-open')) {
        this._input.focus();
      }
    };
    (root.querySelector('.acb-bubble') as HTMLElement).addEventListener('click', toggle);
    (root.querySelector('.acb-close') as HTMLElement).addEventListener('click', toggle);
    (root.querySelector('.acb-new') as HTMLElement).addEventListener('click', () => this._newChat());
    root.addEventListener('keydown', (e: KeyboardEvent) => {
      if (e.key === 'Escape' && root.classList.contains('acb-open')) {
        toggle();
      }
    });
    (root.querySelector('.acb-form') as HTMLFormElement).addEventListener('submit', (e: Event) => {
      e.preventDefault();
      this._onSend().catch((err: Error) => Log.error(LOG_SOURCE, err));
    });

    this._chat = this._loadChat();
    if (this._chat.messages.length) {
      this._chat.messages.forEach((m: IStoredMessage) => this._renderMessage(m));
    } else {
      this._greet();
    }
  }

  private _greet(): void {
    const firstName: string = (this.context.pageContext.user.displayName || '').split(' ')[0];
    this._renderMessage({
      kind: 'bot',
      text: `Hi ${firstName}! Ask me anything about the Knowledge Hub. I answer from its pages and documents and link the sources.`
    });
  }

  private _newChat(): void {
    this._chat = { messages: [] };
    this._saveChat();
    this._messages.textContent = '';
    this._greet();
    this._input.focus();
  }

  private async _onSend(): Promise<void> {
    const text: string = this._input.value.trim();
    if (!text) {
      return;
    }
    this._input.value = '';
    this._addMessage({ kind: 'user', text });
    this._send.disabled = true;
    const typing: HTMLElement = this._showTyping();

    try {
      let response: IChatResponse;
      try {
        response = await this._ask(text, this._chat.conversationId);
      } catch (err) {
        // The server no longer knows this conversation: start a fresh one
        // with the same question instead of making the user retype it.
        if (err instanceof ChatApiError && err.status === 404 && this._chat.conversationId) {
          this._chat.conversationId = undefined;
          response = await this._ask(text, undefined);
        } else {
          throw err;
        }
      }
      this._chat.conversationId = response.conversation_id;
      this._addMessage({
        kind: 'bot',
        text: response.reply,
        sources: response.sources,
        note: response.project_scope_fell_back && response.project_numbers.length
          ? `No content is tagged with project ${response.project_numbers.join(', ')}, so these are general results.`
          : undefined
      });
    } catch (err) {
      Log.error(LOG_SOURCE, err as Error);
      this._addMessage({ kind: 'error', text: friendlyError(err) });
    } finally {
      typing.remove();
      this._send.disabled = false;
      this._input.focus();
    }
  }

  private async _ask(text: string, conversationId: string | undefined): Promise<IChatResponse> {
    const post: PostJson = await this._getPost();
    return sendChat(post, this._apiUrl, text, conversationId, window.location.href);
  }

  private _showTyping(): HTMLElement {
    const el: HTMLDivElement = document.createElement('div');
    el.className = 'acb-msg acb-bot acb-typing';
    el.setAttribute('aria-label', 'The assistant is typing');
    for (let i: number = 0; i < 3; i++) {
      el.appendChild(document.createElement('span'));
    }
    this._messages.appendChild(el);
    this._messages.scrollTop = this._messages.scrollHeight;
    return el;
  }

  private _addMessage(message: IStoredMessage): void {
    this._renderMessage(message);
    this._chat.messages.push(message);
    this._chat.messages = this._chat.messages.slice(-MAX_STORED_MESSAGES);
    this._saveChat();
  }

  private _renderMessage(message: IStoredMessage): void {
    const el: HTMLDivElement = document.createElement('div');
    el.className = `acb-msg acb-${message.kind}`;

    if (message.kind === 'bot') {
      if (message.note) {
        const note: HTMLDivElement = document.createElement('div');
        note.className = 'acb-note';
        note.textContent = message.note;
        el.appendChild(note);
      }
      const sources: IChatSource[] = message.sources || [];
      const urlFor = (n: number): string | undefined => {
        const source: IChatSource | undefined = sources.filter((s: IChatSource) => s.number === n)[0];
        return source && source.url ? source.url : undefined;
      };
      el.appendChild(renderMarkdown(document, message.text, urlFor));
      this._appendSources(el, sources.filter((s: IChatSource) => s.cited));
    } else {
      // textContent, never innerHTML: user input and error text are plain text.
      el.textContent = message.text;
    }

    this._messages.appendChild(el);
    this._messages.scrollTop = this._messages.scrollHeight;
  }

  private _appendSources(container: HTMLElement, cited: IChatSource[]): void {
    if (!cited.length) {
      return;
    }
    const box: HTMLDivElement = document.createElement('div');
    box.className = 'acb-sources';
    const title: HTMLDivElement = document.createElement('div');
    title.className = 'acb-sources-title';
    title.textContent = 'Sources';
    box.appendChild(title);
    const list: HTMLOListElement = document.createElement('ol');
    cited.forEach((source: IChatSource) => {
      const li: HTMLLIElement = document.createElement('li');
      const label: string = `[${source.number}] ${source.title}${source.sections.length ? ` › ${source.sections.join(', ')}` : ''}`;
      const href: string | undefined = safeHttpsUrl(source.url);
      if (href) {
        const a: HTMLAnchorElement = document.createElement('a');
        a.href = href;
        a.textContent = label;
        a.target = '_blank';
        a.rel = 'noopener noreferrer';
        li.appendChild(a);
      } else {
        li.textContent = label;
      }
      list.appendChild(li);
    });
    box.appendChild(list);
    container.appendChild(box);
  }

  private _loadChat(): IStoredChat {
    // sessionStorage: the conversation survives full page loads within the
    // browser tab, and is gone when the tab closes.
    try {
      const raw: string | null = window.sessionStorage.getItem(STORAGE_KEY);
      const parsed: IStoredChat | null = raw ? (JSON.parse(raw) as IStoredChat) : null;
      if (parsed && Array.isArray(parsed.messages)) {
        return parsed;
      }
    } catch {
      // Storage blocked or corrupt -- just start fresh.
    }
    return { messages: [] };
  }

  private _saveChat(): void {
    try {
      window.sessionStorage.setItem(STORAGE_KEY, JSON.stringify(this._chat));
    } catch {
      // Storage full or blocked: the chat still works, it just won't survive a reload.
    }
  }
}

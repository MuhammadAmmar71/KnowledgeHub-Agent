/**
 * Talks to the Knowledge Agent backend's POST /chat
 * (knowledge-agent README, "Chat API").
 *
 * The transport is injected: AadHttpClient in production (attaches an
 * Entra ID token for the signed-in user), plain fetch for local debugging
 * against a dev-mode backend. Both are reduced to `PostJson`, which keeps
 * this file free of SPFx imports and easy to test.
 */

export interface IChatSource {
  number: number;
  title: string;
  url?: string;
  sections: string[];
  source_type: string;
  cited: boolean;
}

export interface IChatResponse {
  reply: string;
  conversation_id: string;
  sources: IChatSource[];
  project_numbers: string[];
  project_scope_fell_back: boolean;
}

export interface IJsonResponse {
  status: number;
  json(): Promise<unknown>;
}

export type PostJson = (url: string, body: string) => Promise<IJsonResponse>;

export class ChatApiError extends Error {
  public readonly status: number | undefined;

  public constructor(message: string, status?: number) {
    super(message);
    // The bundle targets ES5, where extending Error breaks the prototype
    // chain -- without this, `err instanceof ChatApiError` is always false.
    Object.setPrototypeOf(this, ChatApiError.prototype);
    this.name = 'ChatApiError';
    this.status = status;
  }
}

function isChatResponse(value: unknown): value is IChatResponse {
  const v: Partial<IChatResponse> = value as Partial<IChatResponse>;
  return !!v && typeof v.reply === 'string' && typeof v.conversation_id === 'string' && Array.isArray(v.sources);
}

export async function sendChat(
  post: PostJson,
  apiUrl: string,
  message: string,
  conversationId: string | undefined,
  pageUrl: string
): Promise<IChatResponse> {
  const body: string = JSON.stringify({ message, conversation_id: conversationId, page_url: pageUrl });
  let response: IJsonResponse;
  try {
    response = await post(apiUrl, body);
  } catch (err) {
    // Network failure, CORS rejection, or (AadHttpClient) no token -- the
    // browser deliberately hides which, so the message stays generic.
    throw new ChatApiError((err as Error).message || 'network error');
  }
  if (response.status < 200 || response.status >= 300) {
    throw new ChatApiError(`HTTP ${response.status}`, response.status);
  }
  const data: unknown = await response.json();
  if (!isChatResponse(data)) {
    throw new ChatApiError('unexpected response from the chat service', response.status);
  }
  return data;
}

/** What to show the user for a failed request. */
export function friendlyError(err: unknown): string {
  const status: number | undefined = err instanceof ChatApiError ? err.status : undefined;
  switch (status) {
    case 401:
      return 'Your sign-in could not be verified. Please reload the page and try again.';
    case 403:
      return "You don't have access to the Knowledge Hub Assistant. Please ask the Knowledge Hub owner for access.";
    case 404:
      return 'This conversation has expired. Please start a new chat.';
    case 422:
      return 'That message could not be sent. It may be too long; please shorten it and try again.';
    case 502:
    case 503:
    case 504:
      return 'The assistant is temporarily unavailable. Please try again in a moment.';
    case undefined:
      return "Couldn't reach the assistant. Check your connection and try again.";
    default:
      return `Something went wrong (HTTP ${status}). Please try again.`;
  }
}

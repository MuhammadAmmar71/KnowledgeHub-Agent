import { ChatApiError, friendlyError, IJsonResponse, sendChat } from './chatApi';

const OK: object = { reply: 'hi [1]', conversation_id: 'c1', sources: [], project_numbers: [], project_scope_fell_back: false };

function respond(status: number, body: unknown): () => Promise<IJsonResponse> {
  return () => Promise.resolve({ status, json: () => Promise.resolve(body) });
}

describe('sendChat', () => {
  it('posts message, conversation id and page url', async () => {
    const calls: string[][] = [];
    const post = (url: string, body: string): Promise<IJsonResponse> => {
      calls.push([url, body]);
      return respond(200, OK)();
    };
    const result = await sendChat(post, 'https://api/chat', 'q?', 'c0', 'https://sp/page');
    expect(result.reply).toBe('hi [1]');
    expect(calls[0][0]).toBe('https://api/chat');
    expect(JSON.parse(calls[0][1])).toEqual({ message: 'q?', conversation_id: 'c0', page_url: 'https://sp/page' });
  });

  it('raises with the HTTP status on errors', async () => {
    await expect(sendChat(respond(404, {}), 'u', 'q', 'c', 'p')).rejects.toMatchObject({ status: 404 });
  });

  it('rejects malformed responses', async () => {
    await expect(sendChat(respond(200, { nope: true }), 'u', 'q', undefined, 'p')).rejects.toBeInstanceOf(ChatApiError);
  });

  it('wraps network failures without a status', async () => {
    const post = (): Promise<IJsonResponse> => Promise.reject(new TypeError('Failed to fetch'));
    await expect(sendChat(post, 'u', 'q', undefined, 'p')).rejects.toMatchObject({ status: undefined });
  });
});

describe('friendlyError', () => {
  it('maps statuses to user-facing text', () => {
    expect(friendlyError(new ChatApiError('x', 401))).toContain('sign-in');
    expect(friendlyError(new ChatApiError('x', 403))).toContain("don't have access");
    expect(friendlyError(new ChatApiError('x', 503))).toContain('temporarily unavailable');
    expect(friendlyError(new ChatApiError('x'))).toContain("Couldn't reach");
    expect(friendlyError(new Error('boom'))).toContain("Couldn't reach");
  });
});

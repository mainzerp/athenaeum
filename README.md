# Athenaeum

**Your personal knowledge base that organizes itself.**

Athenaeum is a self-hosted memory for you and your AI tools. Instead of
scattering notes across apps and chat histories, you tell Athenaeum what to
remember — and a built-in librarian keeps everything filed, linked, and tidy.
Your knowledge lives as plain Markdown files on your own server: readable and
editable with any tool, forever yours.

## How it works

1. **Connect your AI assistant.** Athenaeum speaks MCP, the open standard for
   AI tools — point your assistant at your Athenaeum server with a personal
   access token and you're set.

2. **Ask.** When you ask a question, your assistant consults Athenaeum's
   librarian, who searches your library and answers with sources from *your*
   knowledge — not generic web text.

3. **Remember.** Say "remember this" and the librarian decides where the
   information belongs, writes it down properly, and links it to related
   notes. No folders to manage, no tags to invent.

4. **Stay tidy.** A second agent, the curator, keeps the library in shape:
   it fixes stale links, merges duplicates, and flags outdated documents —
   on request, or automatically every night. You never organize anything by
   hand.

5. **Find by meaning.** Optional semantic search finds notes by what they
   *mean*, not just by the words they contain. It can run entirely on your
   own hardware, so nothing leaves the host.

Everything your agents do is visible: the web interface lets you browse
your library as an interactive graph, read every document, and review a full
activity log of each change and why it happened.

Every library write is also a git commit. Every document page carries a
History card: a slider over that file's commits, a read-only historical view
with its diff, and "Restore this version" for that one file. The Library
page carries the full history — per-commit diffs, revert for any commit, and
an undoable reset slider; an optional remote in Library settings adds
push/pull (remote auth uses the git client's own credentials).

## Multi-user

One Athenaeum server serves your whole household or team: every user gets
their own private library and their own librarian, with their own AI provider
keys and access tokens. Admins manage users in the web interface.

## Your data stays yours

- Self-hosted: one container, one data volume — that's it.
- Plain files: your library is ordinary Markdown you can copy, back up, or
  take elsewhere at any time — the web interface downloads the whole library
  as a zip archive and restores it back. The archive holds content only
  (git history and legacy snapshot data are excluded); an import
  re-initializes the history.
- Encrypted keys: AI provider credentials are stored encrypted, never in
  plain text.
- Local option: semantic search can run fully offline on your own machine.
- Web security: every mutating web form carries a per-session CSRF token and
  session cookies are `SameSite=Lax`. The app serves plain HTTP — when you
  expose it beyond localhost, put it behind a TLS-terminating reverse proxy.

## Getting started

Requires Docker.

```bash
export ATHENAEUM_SECRET_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
docker compose up -d
```

To skip the local build and use the prebuilt image from GHCR instead
(published by the `ci` GitHub workflow):

```bash
docker compose -f docker-compose.prebuilt.yml up -d
# pin a release: ATHENAEUM_IMAGE_TAG=0.20.0 docker compose -f docker-compose.prebuilt.yml up -d
```

Then:

1. Open <http://localhost:8000/> and create your owner account.
2. Under **Settings**, add a connection to your AI provider (API key).
3. Under **Tokens**, create an access token — it is shown exactly once.
4. In your AI assistant, add Athenaeum as an MCP server:

```json
{
  "mcpServers": {
    "athenaeum": {
      "url": "http://localhost:8000/mcp",
      "headers": { "Authorization": "Bearer <your token>" }
    }
  }
}
```

Start a conversation: "What do you know about X?" — or "Remember that …".

---

Developer documentation (architecture, roadmap, contribution internals)
lives in `docs/project/`.

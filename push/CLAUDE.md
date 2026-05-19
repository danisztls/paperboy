# push/

Target implementations. Each implements `pipeline.Target.push(ctx, cfg, session) → set[str]` returning IDs of items that failed.

## `discord.py` — Discord webhook targets

- `DiscordEmbedTarget` — each item as a Discord embed. The embed image is `Item.image` (set during pull from the feed entry or during summarize from the article's `og:image`); `item.meta["skip_image"]` (from task/feed `image.skip`) suppresses it.
- `DiscordTextTarget` — each item's body as plain text (truncated to 2000 chars).
- `DiscordMarkdownTarget` — each item as `### [title](<url>) source\nbody` (markdown, no embed).
- `DiscordDigestTarget` — renders each `MemoryParagraph` to plain text with source links appended (`[[Source](<url>)]`), posts as ≤2000-char chunks.

All use `_post_webhook` which retries once on 429.

## `file.py` — file-based targets

Path is expanded (`~`, env vars) and parent dirs are created on first write.

- `FileEmbedTarget` — appends each item as `## [Title](url)\n*source · date*\n\nbody\n\n---` blocks.
- `FileDigestTarget` — renders each `MemoryParagraph` to markdown with source links appended (`[Source](url)`), then appends `## YYYY-MM-DD\n\ndigest text\n\n---`.

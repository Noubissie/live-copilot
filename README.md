# TikTok LIVE Copilot

Claude reads your TikTok LIVE chat as it happens. It sorts the comments, writes short replies for you, and keeps notes. You choose which answers appear on the stream.

| Page | URL | Where it goes |
|---|---|---|
| **Control panel** (private) | http://localhost:8765/panel | A browser window on your PC or a second monitor. **Don't** add it to LIVE Studio. |
| **Overlay** (viewers see it) | http://localhost:8765/overlay | A link/browser source in TikTok LIVE Studio |

## What it does
- **Live chat feed**: each comment gets a label (question, request, feedback, greeting, spam or toxic). Spam and toxic comments are greyed out.
- **Suggested replies**: Claude drafts a short reply for each question or request. When several viewers ask the same thing, they're grouped into one card ("3 asked"). You can edit the text, then:
  - **Show on stream**: the question and your answer appear on the overlay for 20 seconds.
  - **Answered out loud**: marks the card done without putting it on screen.
  - **Copy** or **Dismiss**.
- **Auto-show** (optional): puts the top safe answer on screen automatically, at most one every 10 seconds.
- **Your own text on screen**: for example "Code link in bio 👆". You can show it once or pin it.
- **Stream context**: tell Claude what you're doing (topic, gear, links). Its replies get much better with this.
- **Notes**: recurring questions, content ideas and promises you made are saved to `notes/session-<date>.md`, along with every question and its suggested answer.
- **Safety**: spam, toxic comments and anything Claude flags (links, personal data and so on) can never go on the overlay. Nothing is ever posted in your TikTok chat.

## Setup (Windows, once)
1. Install **Python 3.10+** from python.org. Tick "Add Python to PATH".
2. Get an API key at https://console.anthropic.com.
3. Unzip this folder and double-click **`start.bat`**. The first time, it installs everything and opens `.env`. Fill in `TIKTOK_USERNAME` and `ANTHROPIC_API_KEY`, save, and run `start.bat` again.

Manual alternative: `pip install -r requirements.txt`, copy `.env.example` to `.env`, then `python app.py`.

## Test before going live
```
python app.py --simulate              # fake viewers, real Claude replies
python app.py --simulate --mock-llm   # fake viewers, no API key needed
```
(or `start.bat --simulate`)

## Add the overlay to TikTok LIVE Studio
1. Start the app.
2. In LIVE Studio, go to **Add source**, choose the web/link source (shown as "Link" in recent versions), and paste `http://localhost:8765/overlay`.
3. Size it to the full canvas. The background is transparent, so only the cards show.
4. Overlay options, added to the end of the URL:
   - `?pos=bottom` moves the cards to the bottom. By default they sit near the top, away from TikTok's own comment area.
   - `?top=300` sets your own distance from the top, in pixels.
   - `?topic=0` hides the "Chat: …" topic pill.
   - Options can be combined, e.g. `http://localhost:8765/overlay?top=250&topic=0`

If your LIVE Studio version has no web/link source, you can run OBS with this overlay as a Browser Source and bring it into LIVE Studio through OBS Virtual Camera.

## How it works
`TikTokLive` (an unofficial library that receives live chat as it happens) → batches every 8s → Claude (one forced tool call returns categories, replies, notes and the topic) → FastAPI + WebSockets → panel and overlay.

## Notes and limits
- TikTokLive is unofficial. If TikTok changes something and it stops connecting, run `pip install -U TikTokLive`.
- The app connects once you're live. Before that it checks every 20 seconds, so you can start it before you go live.
- Cost: Haiku with 8-second batches costs pennies per stream for most chats. Raise `BATCH_SECONDS` if chat is very busy.
- Replies are never posted in your TikTok chat automatically. That would mean automating your logged-in account, which goes against TikTok's terms and risks the account.

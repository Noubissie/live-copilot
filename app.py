"""
TikTok LIVE Copilot
-------------------
Reads your TikTok LIVE chat in real time, uses Claude to sort comments,
take notes and draft replies, and serves two pages:

  /panel    private control panel (open in a browser on your PC; do NOT add to LIVE Studio)
  /overlay  public overlay (add to TikTok LIVE Studio as a link/browser source)

Run:
  python app.py                      # live mode (uses TIKTOK_USERNAME from .env)
  python app.py --simulate           # fake comments, real Claude (test before going live)
  python app.py --simulate --mock-llm  # fake comments, no API key needed
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import logging
import os
import random
import time
from contextlib import asynccontextmanager
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel

load_dotenv()

BASE = Path(__file__).parent
STATIC = BASE / "static"
NOTES_DIR = BASE / "notes"
NOTES_DIR.mkdir(exist_ok=True)

TIKTOK_USERNAME = os.getenv("TIKTOK_USERNAME", "").lstrip("@")
MODEL = os.getenv("CLAUDE_MODEL", "claude-haiku-5-5")
BATCH_SECONDS = float(os.getenv("BATCH_SECONDS", "8"))
OVERLAY_SECONDS = float(os.getenv("OVERLAY_SECONDS", "20"))
AUTO_GAP_SECONDS = float(os.getenv("AUTO_GAP_SECONDS", "10"))
PORT = int(os.getenv("PORT", "8765"))
STREAM_CONTEXT = os.getenv("STREAM_CONTEXT", "")

log = logging.getLogger("copilot")
logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")

# --------------------------------------------------------------------------- state

_ids = itertools.count(1)


class State:
    def __init__(self) -> None:
        self.comments: deque[dict] = deque(maxlen=300)
        self.pending: list[dict] = []          # comments not yet sent to Claude
        self.suggestions: list[dict] = []
        self.notes: list[str] = []
        self.topic: str = ""
        self.featured: dict | None = None      # what the overlay shows
        self.featured_at: float = 0
        self.last_auto: float = 0
        self.auto_mode: bool = False
        self.context: str = STREAM_CONTEXT
        self.status: str = "starting"
        self.stats = {"comments": 0, "questions": 0, "llm_calls": 0}
        self.notes_file = NOTES_DIR / f"session-{datetime.now():%Y%m%d-%H%M}.md"

    def snapshot(self, public: bool) -> dict:
        if public:
            # The overlay only ever receives what you chose to put on screen.
            return {"featured": self.featured, "topic": self.topic}
        return {
            "comments": list(self.comments)[-120:],
            "suggestions": [s for s in self.suggestions if s["status"] == "pending"][-40:],
            "notes": self.notes[-30:],
            "topic": self.topic,
            "featured": self.featured,
            "auto_mode": self.auto_mode,
            "context": self.context,
            "status": self.status,
            "stats": self.stats,
            "notes_file": str(self.notes_file.name),
        }


S = State()

# --------------------------------------------------------------------------- websockets


class Hub:
    def __init__(self) -> None:
        self.clients: dict[WebSocket, bool] = {}  # ws -> is_public

    async def add(self, ws: WebSocket, public: bool) -> None:
        await ws.accept()
        self.clients[ws] = public
        await ws.send_json(S.snapshot(public))

    def remove(self, ws: WebSocket) -> None:
        self.clients.pop(ws, None)

    async def broadcast(self) -> None:
        dead = []
        for ws, public in list(self.clients.items()):
            try:
                await ws.send_json(S.snapshot(public))
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.remove(ws)


hub = Hub()

# --------------------------------------------------------------------------- comments in


async def add_comment(user: str, nickname: str, text: str) -> None:
    text = (text or "").strip()
    if not text:
        return
    c = {
        "id": next(_ids),
        "user": user or "viewer",
        "nickname": nickname or user or "viewer",
        "text": text[:300],
        "time": datetime.now().strftime("%H:%M:%S"),
        "category": None,
    }
    S.comments.append(c)
    S.pending.append(c)
    S.stats["comments"] += 1
    await hub.broadcast()


# --------------------------------------------------------------------------- Claude

SYSTEM_PROMPT = """You are a live-stream co-pilot for a TikTok LIVE host.
You receive batches of viewer comments. For each batch:
1. Categorise every comment: question, request, feedback, greeting, spam, toxic, other.
2. For comments worth responding to (mainly questions and requests, sometimes great feedback),
   draft a reply the HOST can say out loud or show on screen. Replies: short (max ~25 words),
   friendly, in the host's voice, in the same language as the comment. If several viewers ask
   the same thing, make ONE suggestion that covers them and list all their comment ids.
   Never invent facts about the host; if the answer depends on info you don't have, suggest a
   reply that hands it back to the host (e.g. "Good question, I'll show that in a sec").
3. Set safe_for_overlay=false for anything containing personal data, insults, sexual content,
   politics baiting, links, or anything you would not want displayed on screen.
4. Add short notes only when something is worth remembering after the stream (recurring
   questions, content ideas, feedback, follow-ups promised). Do not repeat existing notes.
5. topic: one short neutral line (max 10 words) on what chat is currently talking about,
   suitable for showing to viewers. Empty string if nothing stands out.
Always respond by calling the report tool."""

REPORT_TOOL = {
    "name": "report",
    "description": "Report categories, suggested replies, notes and the current chat topic.",
    "input_schema": {
        "type": "object",
        "properties": {
            "categories": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer"},
                        "category": {
                            "type": "string",
                            "enum": ["question", "request", "feedback", "greeting", "spam", "toxic", "other"],
                        },
                    },
                    "required": ["id", "category"],
                },
            },
            "suggestions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "comment_ids": {"type": "array", "items": {"type": "integer"}},
                        "reply": {"type": "string"},
                        "priority": {"type": "integer", "minimum": 1, "maximum": 3,
                                     "description": "3 = answer now, 1 = optional"},
                        "safe_for_overlay": {"type": "boolean"},
                    },
                    "required": ["comment_ids", "reply", "priority", "safe_for_overlay"],
                },
            },
            "notes": {"type": "array", "items": {"type": "string"}},
            "topic": {"type": "string"},
        },
        "required": ["categories", "suggestions", "notes", "topic"],
    },
}


class Brain:
    def __init__(self, mock: bool) -> None:
        self.mock = mock
        self.client = None
        if not mock:
            from anthropic import AsyncAnthropic
            if not os.getenv("ANTHROPIC_API_KEY"):
                raise SystemExit("ANTHROPIC_API_KEY missing (put it in .env, or run with --mock-llm)")
            self.client = AsyncAnthropic()

    async def analyse(self, batch: list[dict]) -> dict:
        if self.mock:
            return self._mock(batch)
        lines = "\n".join(f'[{c["id"]}] @{c["user"]} ({c["nickname"]}): {c["text"]}' for c in batch)
        user_msg = (
            f"STREAM CONTEXT (from the host):\n{S.context or '(none given)'}\n\n"
            f"EXISTING NOTES:\n" + ("\n".join(f"- {n}" for n in S.notes[-15:]) or "(none)") +
            f"\n\nNEW COMMENTS:\n{lines}"
        )
        resp = await self.client.messages.create(
            model=MODEL,
            max_tokens=1500,
            system=SYSTEM_PROMPT,
            tools=[REPORT_TOOL],
            tool_choice={"type": "tool", "name": "report"},
            messages=[{"role": "user", "content": user_msg}],
        )
        S.stats["llm_calls"] += 1
        for block in resp.content:
            if getattr(block, "type", None) == "tool_use":
                return block.input
        return {}

    @staticmethod
    def _mock(batch: list[dict]) -> dict:
        out: dict[str, Any] = {"categories": [], "suggestions": [], "notes": [], "topic": ""}
        for c in batch:
            t = c["text"].lower()
            if "?" in t:
                cat = "question"
                out["suggestions"].append({
                    "comment_ids": [c["id"]],
                    "reply": f"Great question {c['nickname']}! (mock reply: set ANTHROPIC_API_KEY for real ones)",
                    "priority": 2, "safe_for_overlay": True,
                })
            elif any(w in t for w in ("hi", "hello", "hey")):
                cat = "greeting"
            elif "http" in t or "follow4follow" in t:
                cat = "spam"
            else:
                cat = "other"
            out["categories"].append({"id": c["id"], "category": cat})
        if batch:
            out["topic"] = "Chat is warming up"
        return out


def apply_analysis(result: dict, batch: list[dict]) -> None:
    by_id = {c["id"]: c for c in batch}
    for item in result.get("categories", []):
        c = by_id.get(item.get("id"))
        if c:
            c["category"] = item.get("category")
            if c["category"] == "question":
                S.stats["questions"] += 1

    new_suggestions = []
    for sug in result.get("suggestions", []):
        ids = [i for i in sug.get("comment_ids", []) if i in by_id]
        if not ids or not sug.get("reply"):
            continue
        src = [by_id[i] for i in ids]
        # Never put toxic/spam on screen, whatever the model said.
        safe = bool(sug.get("safe_for_overlay")) and not any(c["category"] in ("toxic", "spam") for c in src)
        s = {
            "id": next(_ids),
            "comment_ids": ids,
            "users": [c["user"] for c in src],
            "nicknames": [c["nickname"] for c in src],
            "question": src[0]["text"],
            "count": len(src),
            "reply": sug["reply"].strip(),
            "priority": int(sug.get("priority", 2)),
            "safe": safe,
            "status": "pending",
            "time": datetime.now().strftime("%H:%M:%S"),
        }
        S.suggestions.append(s)
        new_suggestions.append(s)

    new_notes = [n.strip() for n in result.get("notes", []) if n and n.strip() and n.strip() not in S.notes]
    S.notes.extend(new_notes)
    if result.get("topic") is not None:
        S.topic = result["topic"].strip()

    write_notes(batch, new_suggestions, new_notes)


def write_notes(batch: list[dict], suggestions: list[dict], notes: list[str]) -> None:
    first = not S.notes_file.exists()
    with S.notes_file.open("a", encoding="utf-8") as f:
        if first:
            f.write(f"# TikTok LIVE notes — {datetime.now():%A %d %B %Y, %H:%M}\n\n")
            if S.context:
                f.write(f"_Stream context: {S.context}_\n\n")
        for n in notes:
            f.write(f"- 📝 {n}\n")
        for s in suggestions:
            who = ", ".join("@" + u for u in s["users"])
            f.write(f"- ❓ [{s['time']}] {who}: \"{s['question']}\"\n    → suggested: {s['reply']}\n")


async def brain_loop(brain: Brain) -> None:
    while True:
        await asyncio.sleep(BATCH_SECONDS)
        if not S.pending:
            continue
        batch, S.pending = S.pending[:60], S.pending[60:]
        try:
            result = await brain.analyse(batch)
            apply_analysis(result, batch)
        except Exception as e:  # keep the stream running whatever happens
            log.warning("Claude call failed: %s", e)
        await hub.broadcast()


# --------------------------------------------------------------------------- overlay control


def feature(payload: dict | None) -> None:
    S.featured = payload
    S.featured_at = time.time()


async def overlay_loop() -> None:
    """Auto-hide the overlay card, and run auto-mode."""
    while True:
        await asyncio.sleep(1)
        changed = False
        if S.featured and not S.featured.get("pinned") and time.time() - S.featured_at > OVERLAY_SECONDS:
            S.featured = None
            changed = True
        if S.auto_mode and not S.featured and time.time() - S.last_auto > AUTO_GAP_SECONDS:
            candidates = [s for s in S.suggestions if s["status"] == "pending" and s["safe"] and s["priority"] >= 2]
            if candidates:
                best = max(candidates, key=lambda s: (s["priority"], s["count"], s["id"]))
                show_suggestion(best)
                S.last_auto = time.time()
                changed = True
        if changed:
            await hub.broadcast()


def show_suggestion(s: dict, reply_override: str | None = None) -> None:
    s["status"] = "shown"
    extra = f" +{s['count'] - 1} more" if s["count"] > 1 else ""
    feature({
        "kind": "qa",
        "who": s["nicknames"][0] + extra,
        "question": s["question"],
        "answer": reply_override or s["reply"],
    })


# --------------------------------------------------------------------------- TikTok


async def tiktok_loop() -> None:
    from TikTokLive import TikTokLiveClient
    from TikTokLive.events import CommentEvent, ConnectEvent, DisconnectEvent, LiveEndEvent

    while True:
        client = TikTokLiveClient(unique_id=f"@{TIKTOK_USERNAME}")

        @client.on(ConnectEvent)
        async def _on_connect(_: ConnectEvent) -> None:
            S.status = f"connected to @{TIKTOK_USERNAME}"
            log.info(S.status)
            await hub.broadcast()

        @client.on(CommentEvent)
        async def _on_comment(event: CommentEvent) -> None:
            u = event.user
            handle = (getattr(u, "display_id", None) or getattr(u, "unique_id", None) or "") if u else ""
            nick = (getattr(u, "nick_name", None) or getattr(u, "nickname", None) or handle) if u else ""
            await add_comment(handle, nick, event.content)

        @client.on(DisconnectEvent)
        async def _on_disconnect(_: DisconnectEvent) -> None:
            S.status = "disconnected, retrying"
            await hub.broadcast()

        @client.on(LiveEndEvent)
        async def _on_end(_: LiveEndEvent) -> None:
            S.status = "live ended"
            await hub.broadcast()

        try:
            if not await client.is_live():
                S.status = f"@{TIKTOK_USERNAME} is not live yet, checking every 20s"
                await hub.broadcast()
                await asyncio.sleep(20)
                continue
            await client.connect()
        except Exception as e:
            S.status = f"connection error: {type(e).__name__}, retrying"
            log.warning("TikTok connection error: %r", e)
            await hub.broadcast()
        await asyncio.sleep(10)


SIM_COMMENTS = [
    ("sarah_k", "Sarah", "hi from Manchester!!"),
    ("dev_mike", "Mike", "what mic are you using?"),
    ("tobi.codes", "Tobi", "can you explain RAG again? I joined late"),
    ("lena", "Lena", "love this stream 🔥"),
    ("x_promo", "promo", "follow4follow check my page http://spam.example"),
    ("amir", "Amir", "which mic is that? sounds clean"),
    ("jo_builds", "Jo", "how long did it take you to learn LangGraph?"),
    ("kwame", "Kwame", "Hello from Accra 🇬🇭"),
    ("priya.ai", "Priya", "will you share the code on github?"),
    ("ben", "Ben", "do a tutorial on fine-tuning next time pls"),
    ("nina", "Nina", "is this beginner friendly?"),
    ("leo", "Leo", "lol"),
]


async def simulate_loop() -> None:
    S.status = "SIMULATION — fake comments"
    await hub.broadcast()
    while True:
        await asyncio.sleep(random.uniform(1.5, 4))
        await add_comment(*random.choice(SIM_COMMENTS))


# --------------------------------------------------------------------------- web app

app = FastAPI(title="TikTok LIVE Copilot")


@app.get("/")
async def root():
    return RedirectResponse("/panel")


@app.get("/panel")
async def panel():
    return FileResponse(STATIC / "panel.html")


@app.get("/overlay")
async def overlay():
    return FileResponse(STATIC / "overlay.html")


@app.websocket("/ws/panel")
async def ws_panel(ws: WebSocket):
    await hub.add(ws, public=False)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        hub.remove(ws)


@app.websocket("/ws/overlay")
async def ws_overlay(ws: WebSocket):
    await hub.add(ws, public=True)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        hub.remove(ws)


class ShowReq(BaseModel):
    id: int
    reply: str | None = None


class TextReq(BaseModel):
    text: str
    pinned: bool = False


class ToggleReq(BaseModel):
    on: bool


@app.post("/api/show")
async def api_show(req: ShowReq):
    s = next((x for x in S.suggestions if x["id"] == req.id), None)
    if s:
        show_suggestion(s, req.reply)
        await hub.broadcast()
    return {"ok": bool(s)}


@app.post("/api/dismiss")
async def api_dismiss(req: ShowReq):
    for s in S.suggestions:
        if s["id"] == req.id:
            s["status"] = "dismissed"
    await hub.broadcast()
    return {"ok": True}


@app.post("/api/answered")
async def api_answered(req: ShowReq):
    """You answered it out loud — mark done without putting it on screen."""
    for s in S.suggestions:
        if s["id"] == req.id:
            s["status"] = "answered"
    await hub.broadcast()
    return {"ok": True}


@app.post("/api/announce")
async def api_announce(req: TextReq):
    feature({"kind": "announce", "text": req.text[:200], "pinned": req.pinned})
    await hub.broadcast()
    return {"ok": True}


@app.post("/api/clear")
async def api_clear():
    feature(None)
    await hub.broadcast()
    return {"ok": True}


@app.post("/api/auto")
async def api_auto(req: ToggleReq):
    S.auto_mode = req.on
    await hub.broadcast()
    return {"ok": True}


@app.post("/api/context")
async def api_context(req: TextReq):
    S.context = req.text[:1000]
    await hub.broadcast()
    return {"ok": True}


# --------------------------------------------------------------------------- main


def main() -> None:
    p = argparse.ArgumentParser(description="TikTok LIVE Copilot")
    p.add_argument("--simulate", action="store_true", help="generate fake comments instead of connecting to TikTok")
    p.add_argument("--mock-llm", action="store_true", help="don't call Claude (no API key needed)")
    p.add_argument("--user", help="TikTok username (overrides TIKTOK_USERNAME)")
    args = p.parse_args()

    global TIKTOK_USERNAME
    if args.user:
        TIKTOK_USERNAME = args.user.lstrip("@")
    if not args.simulate and not TIKTOK_USERNAME:
        raise SystemExit("Set TIKTOK_USERNAME in .env (or pass --user yourname, or use --simulate)")

    brain = Brain(mock=args.mock_llm)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        tasks = [
            asyncio.create_task(brain_loop(brain)),
            asyncio.create_task(overlay_loop()),
            asyncio.create_task(simulate_loop() if args.simulate else tiktok_loop()),
        ]
        log.info("Control panel:  http://localhost:%s/panel", PORT)
        log.info("Overlay (add to LIVE Studio):  http://localhost:%s/overlay", PORT)
        log.info("Notes file:  %s", S.notes_file)
        yield
        for t in tasks:
            t.cancel()

    app.router.lifespan_context = lifespan

    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")


if __name__ == "__main__":
    main()

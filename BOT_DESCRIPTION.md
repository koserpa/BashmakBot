# BashmakBot - Telegram AI Assistant

## Overview
BashmakBot is a feature-rich Telegram bot designed for a group chat of friends/students. It uses Google's Gemini AI as its core intelligence, augmented with real-time web search, personalized user contexts, and various automated features to create an engaging, helpful group chat participant.

## Core Purpose
- Act as a conversational AI assistant in a group Telegram chat
- Provide informative, contextual responses using Gemini AI
- Enhance group dynamics with proactive features (idle messages, drama intervention, absence pokes)
- Offer utility functions (reminders, web search, weather, currency rates)
- Maintain chat-aware memory and user-specific interaction styles

## Technical Architecture

### Main Components
1. **bot.py** - Entry point
   - Initializes bot, dispatcher, and services
   - Validates configuration
   - Starts health-check server (for deployment platforms like Koyeb)
   - Launches background watchers (date sync, idle chat, reminders)
   - Begins polling for updates
   - **Loads plugins** from `plugins/` folder (see below)

2. **config.py** - Configuration & Constants
   - Loads environment variables via python-dotenv
   - Defines bot tokens, API keys, model names
   - Contains `ENABLED_PLUGINS` — list of plugin names to load on startup
   - Contains `USER_CONTEXT` - personalized profiles for known users (style + sensitive notes)
   - Holds `SYSTEM_PROMPT` - core behavioral instructions for the AI
   - Manages feature flags (via features.py)

3. **bot_state.py** - Runtime State Management

2. **config.py** - Configuration & Constants
   - Loads environment variables via python-dotenv
   - Defines bot tokens, API keys, model names
   - Contains `USER_CONTEXT` - personalized profiles for known users (style + sensitive notes)
   - Holds `SYSTEM_PROMPT` - core behavioral instructions for the AI
   - Manages feature flags (via features.py)

3. **bot_state.py** - Runtime State Management
   - Tracks bot identity (ID, username, full name)
   - Maintains per-chat conversation history (limited size)
   - Records last human activity per chat (for idle detection)
   - Tracks idle message sending state
   - Stores bot start time for uptime calculation

4. **gemini_client.py** - AI Interface Layer
   - Wrapper around Google Gemini API
   - Handles text generation, speech synthesis (TTS), media transcription
   - Implements request rate limiting and quota awareness
   - Manages reaction parsing (REACTION:/VOICE: markers)
   - Includes fallback model support and retry logic

5. **search_utils.py** - Information Retrieval
   - Tavily web search integration (general & news topics)
   - Currency rates via NBP (Polish National Bank) API
   - Weather data via Open-Meteo API
   - Date synchronization service (via timeapi.io)
   - Two-stage search filtering: coarse triggers → LLM classification → Tavily
   - Caching mechanism for search results

6. **handlers.py** - Message Processing Core
   - Dispatches different Telegram message types (text, photo, sticker, voice, document, etc.)
   - Implements prepared pattern: gathers context → calls AI → processes response
   - Features:
     - User context injection (personal style + sensitive notes)
     - Mentioned users context extraction
     - Web search integration (when needed)
     - Media handling (downloads images/voice/documents for AI analysis)
     - Voice response generation (when enabled/appropriate)
     - Reaction emoji handling (unprompted reactions)
     - Special command processing (/start, /help, /remind, /gadalka, etc.)
     - Admin-only commands (/reset, /status, /model, /features)
     - Background task spawning for drama detection and absence pokes

7. **Utility Modules** (imported by handlers):
   - `absence_utils.py` - Tracks user silence periods for absence pokes
   - `drama_utils.py` - Detects heated conversations ("drama") for intervention
   - `reminder_utils.py` - Manages chat-based reminder system
   - `timetable_utils.py` - Provides school schedule information (2TC)
   - `poland_utils.py` - Polish calendar/context information
   - `media_utils.py` - Handles media downloading, extraction, and preparation
   - `text_utils.py` - Text processing helpers (mention extraction, trigger stripping)
   - `features.py` - Feature flag management (voice replies, unprompted reactions, etc.)

### Persistent State (`state_store.py`)
- On Koyeb Free the container disk is ephemeral, so `data/*.json` is only a cache and disappears on redeploy/cold start.
- The bot keeps a single `bashmak-state.json` pinned in its private chat with the owner (`STATE_CHAT_ID`): VULCAN baseline + lesson signatures + notified event ids, and the reminder list.
- On start it reads `getChat(STATE_CHAT_ID).pinned_message.document` → `getFile` → `download_file` and applies known sections; modules register sections via `state_store.register(name, getter, setter)`.
- Updates use `editMessageMedia` on the same message (no new messages, no notifications, throttled); if the pinned message was deleted/unpinned, a new document is sent and pinned.
- Network/broken-JSON errors never overwrite a good snapshot; the background loop retries. Without `STATE_CHAT_ID` the store is a no-op.
- The snapshot contains no tokens/passwords/cookies; its status is exposed in `/health` under `state`.
- Chat history (`bot_state.py`) is also persisted via `state_store`: per chat `{summary, recent}`; when `recent` exceeds `HISTORY_SUMMARY_THRESHOLD` (40 messages), older messages are compressed into `summary` by Gemini. Config: `HISTORY_PERSIST_ENABLED`, `HISTORY_SUMMARY_THRESHOLD`, `HISTORY_RECENT_KEEP`, `HISTORY_SAVE_MIN_SEC`.

## Key Features & Behaviors

### Conversational Intelligence
- Uses Gemini AI with custom `SYSTEM_PROMPT` defining:
  - Tone: laconic, friendly, light humor (no offensiveness)
  - Language matching: responds in same language as query (UA/RU/PL)
  - Length adaptation: 1-2 sentences for simple queries, structured for complex
  - Neutral stance: presents pros/cons when asked for opinions
  - Media understanding: describes/answers about images, stickers, GIFs, voice, files
  - Voice responses: optional TTS responses via VOICE: marker
  - Secret rule: special handling for messages ending with "пж" (do literally)

### Context Awareness
- **Sender Context**: Personalized style notes from `USER_CONTEXT` (with sensitive flags)
- **Mentioned Users**: Extracts @username references and adds their public profiles
- **Chat History**: Maintains rolling conversation history (configurable size)
- **Web Search**: Augments responses with current info when needed (via Tavily)
- **Specialized Data**: Currency rates, weather, Polish events, school timetables

### Proactive & Automated Features
1. **Idle Chat Watcher** - Sends periodic messages when chat is quiet (configurable hours, active hours only, skips weekends)
2. **Drama Detector** - Uses keyword analysis to detect heated exchanges, responds with neutral humor/intervention
3. **Absence Poke** - Notices when known users are silent long-term in active chat, gently mentions their absence
4. **Reminder System** - /remind command to set time-based reminders in chat
5. **Date Synchronization** - Periodically updates current date from internet source
6. **Health Server** - Minimal HTTP endpoint for platforms requiring web service (Koyeb)
7. **VULCAN Schedule Notifications** - Every 15 min polls the student's VULCAN portal and posts schedule changes to the chat: cancellations, teacher/room substitutions, moved lessons, group changes and class outings. Detects changes by diffing lesson signatures against a snapshot; on a fresh deploy it still reports current changes instead of silently seeding a baseline, while the baseline and notified ids are restored from the pinned Telegram snapshot so redeploys don't resend cards. Also sends a morning digest and day-before exam/homework reminders (`vulcan_notify.py`). Change cards list the teacher (and room) next to each lesson, show only the new value in the change line (no duplicated old value or lesson name), and group all of a lesson's changes into one bullet.

### Media Handling
- **Photos**: Downloads, sends to Gemini Vision for description/analysis
- **Stickers/GIFs**: Processes emoji or downloads for AI analysis (non-animated only for vision)
- **Voice/Video Notes**: Transcribes audio, then processes transcript as text (can respond with voice)
- **Documents**: Extracts text from PDF, DOCX, XLSX, PPTX, TXT, CSV, JSON, MD for AI analysis
- **File Size Limits**: Rejects files >20MB to prevent overload

### Commands
- User: `/start`, `/help`, `/rozklad [day]`, `/remind <time> <text>`, `/reminders`, `/remind_del <id>`, `/gadalka`
- Admin: `/reset` (clear chat memory), `/status` (bot stats), `/model` (AI usage), `/features` (flag status)

### Configuration & Deployment
- Requires `.env` with: `BOT_TOKEN`, `GEMINI_API_KEY`, `GEMINI_MODEL`, `STATE_CHAT_ID` (private chat id for the pinned state snapshot), optional `TAVILY_API_KEY`, `ADMIN_USERNAME`, etc.
- Deployable to platforms like Koyeb (including free-tier; health server satisfies port requirement)
- Uses background asyncio tasks for watchers (non-blocking)
- Rate limiting aware: skips non-essential searches when approaching API limits
- Fallback model support via `GEMINI_FALLBACK_MODEL`

## Data Flow Example
1. User sends message: "@BashmakBot погода в Би"
## Плагины / Команды (добавлено)
- Plugin system: плагины в `plugins/` автономно загружаются через `load_plugins()`.
- `/fill <текст>` — ставит напоминание через `reminder_utils` (реально отправляет в чат через `reminders_watcher`).
- `/joke` — случайная шутка (`plugins/funny.py`).
- `/hello` — привет (`plugins/hello.py`).
- `ENABLED_PLUGINS` в `config.py` активирует нужные.

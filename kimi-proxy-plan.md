# Kimi Proxy Web App - Plan

## Overview

A Flask-based web application that acts as a proxy to Kimi AI. Users login through the web app, which captures and stores authentication cookies, then enables chatting with Kimi via the web interface.

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    Web App (Flask)                       │
├─────────────────────────────────────────────────────────┤
│  Frontend (HTML/JS)                                      │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────┐ │
│  │ Login Panel  │  │ Chat Panel  │  │ Cookie Manager  │ │
│  │ [Login Kimi] │  │ [Input]     │  │ [Status]        │ │
│  │              │  │ [Response]  │  │ [Save/Load]     │ │
│  └─────────────┘  └─────────────┘  └─────────────────┘ │
├─────────────────────────────────────────────────────────┤
│  Backend (Python)                                        │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────┐ │
│  │ Route /      │  │ Route /chat │  │ KimiProxy       │ │
│  │ Login Page   │  │ API Proxy   │  │ Cookie Storage  │ │
│  └─────────────┘  └─────────────┘  └─────────────────┘ │
└─────────────────────────────────────────────────────────┘
         │                │                │
         ▼                ▼                ▼
    User Browser    Kimi API        kimi_cookies.json
```

## Workflow

### Login Phase
1. User opens web app → sees "Login to Kimi" button
2. Button opens Kimi in new tab/popup
3. User logs in manually on Kimi
4. User clicks "I've logged in" on web app
5. Web app captures cookies via Playwright (or user pastes from DevTools)

### Search Phase
1. User types query in web app chat interface
2. Web app proxies request to Kimi API with saved cookies
3. Response displayed in web app

## File Structure

```
kimi-proxy/
├── app.py              # Flask backend
├── templates/
│   └── index.html      # Frontend page
├── static/
│   └── style.css       # Styling
├── kimi_cookies.json   # Saved cookies
└── requirements.txt    # Dependencies
```

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Main page with chat UI |
| `/api/status` | GET | Check login status |
| `/api/login` | POST | Save cookies from user input |
| `/api/login/playwright` | POST | Capture cookies via Playwright |
| `/api/chat` | POST | Proxy chat to Kimi |

## Implementation Steps

1. Create Flask app with routes
2. Build frontend with login/chat UI
3. Implement Kimi API proxy using requests/cookies
4. Add cookie persistence
5. Test login flow
6. Test search functionality

## Dependencies

- Flask
- Playwright
- Requests

## Notes

- Cookies are stored locally in `kimi_cookies.json`
- User must manually login the first time for security
- No passwords are stored, only session cookies

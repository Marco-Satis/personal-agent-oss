# Talk to your agent from the iPhone (Shortcuts + Siri)

No app to install from a store, no developer account: the built-in Shortcuts app sends what you say
to your own agent and reads the answer aloud. The phone only relays; everything runs on your server.

## Before you start

1. Tailscale on the server and on the iPhone, both in the same tailnet. On the iPhone, turn on
   **VPN On Demand** in the Tailscale app, so the shortcut can reach the server without opening the app.
2. On the server:
   ```
   personal-agent devices add iphone      # copy the printed token (shown only once)
   ```
   Set `PA_CHAT_DEVICES=on`, restart `serve`, then share it in your tailnet:
   ```
   tailscale serve --bg 8787
   tailscale serve status                 # shows https://<machine>.<tailnet>.ts.net
   ```
3. Check from a device in your tailnet: `https://<machine>.<tailnet>.ts.net/chat` must answer
   **401** without a token (404 means `PA_CHAT_DEVICES` is off or the request did not come through
   `tailscale serve`).

## Build the shortcut

In the Shortcuts app, new shortcut, name it what you want to say after "Hey Siri" (for example
"Agent"). Add these actions:

1. **Dictate Text** (language: yours).
2. **Get Contents of URL**
   - URL: `https://<machine>.<tailnet>.ts.net/chat`
   - Method: `POST`
   - Headers: `Authorization` = `Bearer <your device token>`, `Content-Type` = `application/json`
   - Request Body: JSON, one field `text` = *Dictated Text*
3. **Get Dictionary Value** for key `reply` from *Contents of URL*.
4. **Speak Text**: *Dictionary Value* (or **Show Result** to read it).

When you share the shortcut, mark the token field as an **import question**: Apple then clears it,
so the token does not travel with the shortcut.

## Long answers

The agent waits up to 25 seconds. If the answer takes longer, the reply has no `reply` but an `id`.
Add an **If** after step 3: when there is no `reply`, call **Get Contents of URL** again with
`GET https://<machine>.<tailnet>.ts.net/chat/<id>?wait=25` and the same `Authorization` header.

## Safety

- The token can only chat. It cannot approve anything: actions that need an approval are refused and
  the answer says where to approve (chat, Telegram or the approval page).
- Lost the phone? `personal-agent devices revoke iphone` and the token stops working at once.
- Never use Tailscale Funnel for `/chat`: without the tailnet identity header the agent answers 404.
- Not yet verified on a real iPhone: running while the phone is locked, when dictation stops, and the
  longest wait the shortcut accepts. Test these once before relying on it.

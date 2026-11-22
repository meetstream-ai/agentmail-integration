# Set up calendar sign-in

To connect a calendar, the app signs people in to Google or Microsoft and hands MeetStream a
read-only grant to their calendar. That sign-in runs through an OAuth app that **you**
register once: MeetStream works the same way, so every MeetStream customer brings their own.
After this one-time setup, anyone connects a calendar by clicking **Connect** and **Accept**.

Do only the parts you need: Google, Microsoft, or both.

## Google Calendar

1. Open [Google Cloud console](https://console.cloud.google.com) and create a project (or pick one).
2. **APIs & Services → Library**: enable the **Google Calendar API**.
3. **APIs & Services → OAuth consent screen**:
   - User type **External** (or **Internal** if everyone is in your Google Workspace).
   - Add the scopes `calendar.events.readonly`, `calendar.readonly`, `userinfo.email` and
     `userinfo.profile`.
   - While the app is in **Testing**, add every Google account that will connect under
     **Test users**. Testing-mode connections expire after 7 days; publish the app to keep
     them.
4. **APIs & Services → Credentials → Create credentials → OAuth client ID**:
   - Application type **Web application**.
   - Authorised redirect URI: `http://localhost:8000/calendar/callback`.
5. Put the client ID and secret in `.env`:
   ```
   GOOGLE_CLIENT_ID=...
   GOOGLE_CLIENT_SECRET=...
   ```

## Outlook / Microsoft 365

Registering the app needs a **Microsoft Entra** directory: a work or school account allowed
to register apps, or a free Azure account (signing up with a personal Microsoft account
creates a directory). People who only *connect* a calendar can use any account, including a
personal `@outlook.com` one.

1. Open [Microsoft Entra admin center](https://entra.microsoft.com) → **App registrations**
   → **New registration**.
   - **Supported account types**: *Accounts in any organizational directory and personal
     Microsoft accounts*. This lets both work and personal Outlook accounts connect.
   - **Redirect URI**: platform **Web**, `http://localhost:8000/calendar/outlook/callback`.
2. **Certificates & secrets → New client secret**. Copy its **Value** (not the Secret ID);
   it is shown once.
3. **API permissions → Add a permission → Microsoft Graph → Delegated permissions**:
   `offline_access`, `openid`, `profile`, `email`, `User.Read`, `Calendars.Read`.
4. Put the app's ID and secret in `.env`:
   ```
   MICROSOFT_CLIENT_ID=...          # "Application (client) ID" on the app's Overview page
   MICROSOFT_CLIENT_SECRET=...
   ```

**Already using the app with MeetStream's dashboard?** Keep it. An app can have several
redirect URIs: add this one under **Authentication → Web → Add URI**, next to
`https://app.meetstream.ai/calendar/callback`.

## Connect a calendar

Start the server, open `http://localhost:8000/calendar` and click **Connect Google Calendar**
or **Connect Outlook Calendar**. Sign in, accept, and the page lists the account. Meetings
tagged `#notetaker` get a bot from then on.

## On a hosted deployment

Use your own address instead of `localhost` in both places, the provider's console and the
environment, for example:

| Provider | Redirect URI | Variable |
|---|---|---|
| Google | `https://notes.example.com/calendar/callback` | `GOOGLE_REDIRECT_URI` |
| Microsoft | `https://notes.example.com/calendar/outlook/callback` | `MICROSOFT_REDIRECT_URI` |

See the [deploy guide](deploy.md) for the rest.

## If sign-in fails

| Message | Fix |
|---|---|
| `redirect_uri` *is not valid* / *redirect_uri_mismatch* | The redirect URI in the console must match exactly: `http` vs `https`, the port, no trailing slash. For Microsoft, it must sit under the **Web** platform. |
| *Selected user account does not exist in tenant* | The Microsoft app accepts only its own organisation. Set **Supported account types** as in step 1. |
| *Need admin approval* | The organisation blocks users from granting permissions. An admin clicks **Grant admin consent** under **API permissions**. |
| *Access blocked: app has not completed verification* (Google) | Add the account as a **Test user**, or publish the consent screen. |

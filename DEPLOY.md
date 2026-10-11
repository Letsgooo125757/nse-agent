# Deploying NSE desk for $0/month

| Piece | Service | Free-tier limits that matter |
|---|---|---|
| Web app | **Render** (Docker web service) | Sleeps after 15 min idle, so the first visit after a break takes about a minute. 750 instance-hours a month, enough for one app running all month. |
| Database | **Neon** (serverless Postgres) | 1 GB storage per project (years of NSE data fit easily). Compute suspends after 5 min idle and wakes in about a second. No card needed. |
| Daily update | **GitHub Actions** | Runs `nse-agent daily` on weekdays at 15:30 Nairobi time. |
| Assistant | Anthropic API | Pay-as-you-go from your credits. Capped by `CHAT_DAILY_LIMIT`. |

All three are tied to your GitHub login. Pick **Frankfurt** for both Render and Neon; it's the closest region to Nairobi and keeps them next to each other.

## 1. Run it locally first

```bash
pip install -e ".[dev]"
nse-agent web-setup          # choose a password; prints two lines
```
Paste both printed lines into `.env`, keeping the single quotes. Then:
```bash
nse-agent web                # open http://127.0.0.1:8000 and sign in
```

## 2. Create the database (Neon)

1. Sign up at [neon.com](https://neon.com) with GitHub and create a project called `nse-agent` in **AWS Europe Central (Frankfurt)**.
2. On the project dashboard, click **Connect** and copy the connection string. It looks like
   `postgresql://user:password@ep-xxx.eu-central-1.aws.neon.tech/neondb?sslmode=require`.
3. Copy everything you already have (profile, trades, prices, news, fundamentals) into it:
   ```bash
   nse-agent db-copy --to "postgresql://...neon.tech/neondb?sslmode=require"
   ```
4. Point your local `.env` at Neon too, so the CLI, the website and the daily job share one database:
   `DATABASE_URL='postgresql://...neon.tech/neondb?sslmode=require'`.
   Your old local data stays in `.pgdata/` as a backup.

## 3. Deploy the website (Render)

1. Sign up at [render.com](https://render.com) with GitHub.
2. Click **New > Blueprint**, pick your `nse-agent` repo, and Render reads `render.yaml`.
3. Fill in the values it asks for:
   - `DATABASE_URL`: the Neon string.
   - `APP_PASSWORD_HASH`: the hash from `nse-agent web-setup`, **without** the surrounding quotes.
   - `ANTHROPIC_API_KEY`: your key.

   `SESSION_SECRET` is generated for you.
4. Click **Apply**. The first build takes a few minutes. Your site will be at `https://nse-desk-XXXX.onrender.com`; sign in with your password.

Every `git push` to `main` redeploys automatically.

## 4. Turn on the daily update (GitHub Actions)

1. In your GitHub repo, go to **Settings > Secrets and variables > Actions > New repository secret**. Name it `DATABASE_URL` and paste the Neon string.
2. Open **Actions > daily-update > Run workflow** to test it now. A green tick means prices and news landed in Neon.
3. If you set up Windows Task Scheduler earlier, you can turn it off. If you leave it running, it's harmless, because every write is idempotent.

GitHub pauses scheduled workflows in public repos after 60 days without commits. If that happens, Actions shows a banner; click **Enable workflow**.

## Security checklist

- **Password:** use a long, unique one (10+ characters; a short sentence works well). The app stores only a salted scrypt hash.
- **Login throttling:** 5 failed attempts from one IP locks that IP out for 15 minutes.
- **Secrets:** never commit `.env`; it's git-ignored. Render and GitHub keep their own copies encrypted.
- **Sign everyone out:** change `SESSION_SECRET` in Render.
- **Built-in protections:** HTTPS is automatic on Render. The app sends a strict Content-Security-Policy, HSTS, `X-Frame-Options: DENY` and `no-store` caching, and every form is CSRF-protected.
- **API spend:** `CHAT_DAILY_LIMIT` (default 40 questions a day) caps it, even if someone got in.

## Costs and upgrades

- **Cost:** $0/month for hosting, plus your Anthropic usage (about $0.02–0.05 per question).
- **Cold starts:** if the one-minute wake-up bothers you, upgrade the Render service to a paid instance (check render.com/pricing). Nothing else changes.
- **Database size:** watch it in the Neon dashboard. Prices and news use a few MB a year.

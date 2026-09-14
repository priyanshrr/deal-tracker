# Deployment guide

Written for someone who has never deployed anything. Follow it in order.
Every command is run in **Terminal** (press `Cmd+Space`, type `Terminal`, Enter).

You will need three accounts, all free to create:

- **Anthropic** — for the extraction model (costs money per use, ~$12/month here)
- **Google** — for the Sheet (free)
- **GitHub** — to run the pipeline on a schedule (free tier is enough)

Total time: about 45 minutes, most of it clicking through Google Cloud.

---

## Part 0 — Already done for you

The project lives at `~/deal-tracker`. Its Python environment is installed, its
tests pass, and it is already a git repository with one commit.

To confirm, paste this into Terminal:

```bash
cd ~/deal-tracker && ls && git log --oneline
```

You should see the project files and one commit. If you see
`No such file or directory`, stop — the project is missing and nothing below
will work.

---

## Part 1 — Anthropic API key, and the review you still owe

### 1.1 Create the key

1. Go to **https://console.anthropic.com**
2. Sign up or log in.
3. Click **Settings** (bottom left) → **Billing**. Add a payment method and buy
   credits. **$20 is plenty to start** — see the cost section at the bottom.
   Without credits every API call fails.
4. Click **API keys** in the left sidebar → **Create Key**.
5. Name it `deal-tracker`. Click **Create**.
6. **Copy the key now.** It starts with `sk-ant-`. Google's page will not show it
   again. Paste it somewhere safe for the next few minutes.

### 1.2 Put the key in your Terminal session

```bash
export ANTHROPIC_API_KEY=sk-ant-paste-your-real-key-here
```

This lasts only until you close the Terminal window. That is fine for testing.

### 1.3 Run the review you have not done yet

This is the step the whole build was gated on. Twenty real articles are already
cached in the project, waiting.

```bash
cd ~/deal-tracker
.venv/bin/python -m dealtracker extract --from-cache data/articles_cache.json --limit 20
```

This makes 20 API calls and costs about 8 cents. It prints one JSON record per
article.

**Read all twenty.** You are checking whether the prompt is any good, which
nobody has verified yet. Specifically:

- The VCCircle article titled *"Carrum Mobility, Circolife, ARC, QNu Labs raise
  early-stage funding"* covers **four** deals. It **must** come back as
  `"deal_type": "none"`. If it comes back as one deal, the prompt has the exact
  failure mode you warned about, and I need to fix it before you go further.
- *"Kuku to build 1,000-member AI team ahead of Rs 3,500 Cr IPO"* is about hiring,
  not a transaction. Should be `none`.
- *"ESDS Hits Upper Circuit For Fourth Straight Day"* is share-price movement.
  Should be `none`.
- Amounts should never be invented. Where an article says nothing, expect `null`.

If something looks wrong, tell me what and I will change the prompt. **Do not
continue to Part 2 until you are happy with these outputs** — everything
downstream is built on them.

---

## Part 2 — Google Sheet and service account

This is the fiddliest part. A "service account" is a robot Google account that
your pipeline logs in as. You create it, download its password file, and share
your Sheet with it.

### 2.1 Create the Sheet

1. Go to **https://sheets.google.com** and create a **Blank spreadsheet**.
2. Name it `Deal tracker` (top left).
3. Look at the address bar. The URL looks like:
   `https://docs.google.com/spreadsheets/d/`**`1a2B3cD4eF5gH6iJ7kL8mN9oP`**`/edit`
   The **bold middle part** is your Sheet ID. Copy it somewhere safe.

### 2.2 Create a Google Cloud project

1. Go to **https://console.cloud.google.com**
2. Accept the terms if prompted. You will **not** be charged — the Sheets API is
   free at this volume.
3. At the very top of the page there is a project dropdown (it may say
   "Select a project"). Click it → **New Project**.
4. Name it `deal-tracker`. Click **Create**. Wait ~15 seconds.
5. Click the dropdown again and **select your new project**. Make sure the top
   bar says `deal-tracker` before continuing — everything below applies to the
   selected project.

### 2.3 Turn on the Sheets API

1. In the search bar at the top, type **Google Sheets API** and click the result.
2. Click the blue **Enable** button. Wait for it to finish.

### 2.4 Create the service account

1. In the left sidebar: **APIs & Services** → **Credentials**.
   (If you don't see a sidebar, click the ☰ hamburger menu, top left.)
2. Click **+ Create Credentials** (top of page) → **Service account**.
3. **Service account name**: `deal-tracker-bot`. Click **Create and Continue**.
4. "Grant this service account access to project" — **skip it**, click
   **Continue**. (It needs no project roles; access comes from sharing the Sheet.)
5. "Grant users access" — **skip it**, click **Done**.

### 2.5 Download its key file

1. You are back on the Credentials page. Under **Service Accounts**, click the
   `deal-tracker-bot` entry.
2. Click the **Keys** tab.
3. **Add Key** → **Create new key** → choose **JSON** → **Create**.
4. A `.json` file downloads to your Downloads folder. **This file is a password.**
   Anyone with it can edit your Sheet. Never email it or commit it to GitHub.
5. Move it into the project with a predictable name:

```bash
mv ~/Downloads/deal-tracker-*.json ~/deal-tracker/service_account.json
```

If that errors with "No such file", run `ls ~/Downloads/*.json` to see the real
filename and adjust.

The project's `.gitignore` already excludes `service_account.json`, so it will
not be uploaded to GitHub by accident.

### 2.6 Share the Sheet with the robot

1. Find the robot's email address:

```bash
cd ~/deal-tracker
.venv/bin/python -c "import json;print(json.load(open('service_account.json'))['client_email'])"
```

It looks like `deal-tracker-bot@deal-tracker-123456.iam.gserviceaccount.com`.

2. Open your `Deal tracker` Sheet, click **Share** (top right).
3. Paste that email address.
4. Set the permission dropdown to **Editor**. This matters — Viewer will fail.
5. **Untick "Notify people"** (robots don't read email). Click **Share**.

---

## Part 3 — Test the whole thing locally

Set your three values. Replace the placeholder text with your real Sheet ID;
the other two lines are correct as written.

```bash
cd ~/deal-tracker
export ANTHROPIC_API_KEY=sk-ant-paste-your-real-key-here
export GSHEET_ID=paste-your-sheet-id-here
export GOOGLE_SERVICE_ACCOUNT_JSON="$(cat service_account.json)"
```

Create the header row:

```bash
.venv/bin/python -m dealtracker initsheet
```

Go look at your Sheet. You should see 20 column headings, ending in `read_flag`.
If you get `403` or `PERMISSION_DENIED`, the sharing in step 2.6 didn't take —
recheck the email and that it's set to **Editor**.

Now a real run, writing real deals:

```bash
.venv/bin/python -m dealtracker run --tier 1 --limit 10
```

This fetches live articles, extracts them, deduplicates, and writes rows. It
takes a few minutes and costs a few cents. Refresh your Sheet — there should be
deal rows.

Run it a second time immediately:

```bash
.venv/bin/python -m dealtracker run --tier 1 --limit 10
```

The summary should show most articles as **"seen in an earlier run, skipped"**
and almost nothing newly extracted. That proves it won't duplicate rows or
re-pay for the same articles.

---

## Part 4 — Put it on GitHub

### 4.1 Create the repository

1. Go to **https://github.com** and sign in (or sign up).
2. Click the **+** in the top right → **New repository**.
3. **Repository name**: `deal-tracker`
4. **Select Private.** This matters: the pipeline commits its state file, which
   contains your extracted deal data. A public repo would publish it.
5. Do **not** tick "Add a README" or any other initialise option — your project
   already has files, and those options would conflict.
6. Click **Create repository**.

### 4.2 Push your code

GitHub now shows a page of setup commands. Ignore them and use these instead,
replacing `YOUR-USERNAME` with your actual GitHub username:

```bash
cd ~/deal-tracker
git branch -M main
git remote add origin https://github.com/YOUR-USERNAME/deal-tracker.git
git push -u origin main
```

When prompted for a password, GitHub will **not** accept your account password.
It wants a token:

1. Go to **https://github.com/settings/tokens** → **Generate new token** →
   **Generate new token (classic)**.
2. Note: `deal-tracker`. Expiration: 90 days. Tick the **`repo`** checkbox.
3. **Generate token**, copy it, and paste it as the password in Terminal.

Refresh your GitHub repo page — your files should be there. Confirm
`service_account.json` is **not** among them. If it is, stop and tell me.

### 4.3 Add the three secrets

In your GitHub repo: **Settings** (repo settings, not account) → **Secrets and
variables** → **Actions** → **New repository secret**. Add these three, one at a
time, matching the names exactly:

| Name | Value |
|---|---|
| `ANTHROPIC_API_KEY` | your `sk-ant-...` key |
| `GSHEET_ID` | your Sheet ID from step 2.1 |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | the **entire contents** of `service_account.json` |

For the third one, copy the file contents to your clipboard with:

```bash
cat ~/deal-tracker/service_account.json | pbcopy
```

Then paste into the secret's value box. It must include the opening `{` and
closing `}`.

---

## Part 5 — Turn it on

1. In your repo, click the **Actions** tab.
2. If it asks, click **I understand my workflows, go ahead and enable them**.
3. In the left sidebar click **deal-tracker**.
4. Click **Run workflow** (right side) → **Run workflow**. This runs it once,
   immediately, rather than waiting for the schedule.
5. Wait ~2 minutes, refresh, and click into the run. A green tick means success.
   Click the run to see a summary of what it fetched and wrote.
6. Check your Sheet for new rows.

From now on it runs by itself **every 30 minutes**. You do nothing.

**Note:** GitHub's scheduler is best-effort. Runs can be delayed 10–15 minutes
when GitHub is busy, and schedules on private repos pause after 60 days of no
activity in the repo. Pushing any commit resumes them.

---

## What it costs

**Anthropic**, at $1.00 per million input tokens and $5.00 per million output:
roughly **$0.004 per article** extracted. Expect very roughly 100 new articles a
day reaching the model, so about **$0.40/day — $12/month**. The number that
drives this is articles extracted, not runs: a run that finds nothing new costs
nothing. Watch actuals at console.anthropic.com → Usage, and set a spend limit
under Billing if you want a hard ceiling.

**GitHub Actions** on a private repo: 2,000 free minutes/month. At 48 runs a day
and ~1–2 minutes per run, you may exceed that. If you do, either:

- open `.github/workflows/tracker.yml` and change `*/30 * * * *` to
  `0 * * * *` (hourly — comfortably inside the free tier), or
- pay GitHub's overage (~$0.008/minute), or
- make the repo public — unlimited free minutes, but your deal data becomes
  public. Not recommended.

Check usage at **github.com/settings/billing**.

**Google Sheets**: free.

---

## If something breaks

| Symptom | Cause and fix |
|---|---|
| `ANTHROPIC_API_KEY is not set` | The `export` line only lasts for one Terminal window. Re-run it. |
| `401` / `authentication_error` | Key is wrong or was revoked. Make a new one. |
| `credit balance is too low` | Buy credits at console.anthropic.com → Billing. |
| `403` from Google | Sheet not shared with the robot email, or shared as Viewer instead of Editor. Redo step 2.6. |
| `no spreadsheet id` | `GSHEET_ID` not exported, or not set as a GitHub secret. |
| Action fails at "Commit state" | Usually a permissions issue: repo **Settings → Actions → General → Workflow permissions** → select **Read and write permissions**. |
| Rows appear twice | Expected for undisclosed-amount rounds until stage-2 dedup is calibrated. See the deduplication section of `README.md`. |
| Some sources always error | Five are blocked by the publishers. Expected — see `README.md`. |

To see what the pipeline decided without spending anything:

```bash
cd ~/deal-tracker
.venv/bin/python -m dealtracker filter --tier 1 --limit 20
```

That fetches and shows what passed and what was dropped, with no API calls.

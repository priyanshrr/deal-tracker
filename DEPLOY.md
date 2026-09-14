# Setting this up, starting from zero

No prior knowledge assumed. Read Part A once — it explains what the pieces
*are*. Then do Parts 1 to 6 in order.

Take your time. Nothing here can break your computer.

---

# PART A — What these things actually are

## What you are building

A robot that, **once every hour, forever**, reads Indian business news websites,
finds stories about companies raising money / buying each other / going public,
and writes one row per deal into a Google spreadsheet. You don't press anything.
You just open the spreadsheet when you feel like it.

Four pieces have to talk to each other:

| Piece | What it is | Where it lives |
|---|---|---|
| **The program** | Already written. Does the actual work. | Your Mac, folder `deal-tracker` |
| **Claude** | The AI that reads each article and pulls the facts out | Anthropic's computers |
| **The Sheet** | Where the rows land | Google |
| **GitHub** | A computer in the cloud that runs the program every hour | GitHub |

Why GitHub? Because if the program only ran on your Mac, it would stop every
time you closed your laptop. GitHub runs it on their machine, always.

## Terminal

Terminal is an app already on your Mac. Instead of clicking buttons, you type
instructions. It looks plain and slightly scary. It is fine.

**To open it:** hold `Cmd` and press `Space`, type `Terminal`, press `Enter`.

**How to use it in this guide:** every grey box below is one instruction. Copy
it, click on the Terminal window, paste with `Cmd+V`, press `Enter`.

Three things nobody tells beginners:

- **Most commands print nothing when they succeed.** Silence is good news. You
  only need to worry when you see the word `error` or `failed`.
- **`~` means your home folder.** So `~/deal-tracker` is the project folder.
- **When you type a password, nothing appears.** No dots, no stars. It *is*
  registering. Just type it and press Enter.

## API key

An **API key** is a very long password that proves "this program is allowed to
spend Priyansh's money on AI". It looks like `sk-ant-api03-xxxxxxxxxxxx`.

Think of it as a credit card number for the AI. Anyone who gets it can run up
your bill. So: never screenshot it, never paste it in a chat, never put it in
your code.

## Service account

Your program needs to write into your Google Sheet. But it can't "log in as
you" — there's no human to type your Google password.

So Google lets you create a **robot user**. It gets its own email address (a
long ugly one) and its own key file. You then **share your spreadsheet with the
robot**, exactly the way you'd share it with a colleague.

That's all a service account is: a colleague who is a robot.

## GitHub

GitHub is two things at once, which is why it's confusing:

1. **A place that stores copies of code.** Like Google Drive, but for programs.
2. **GitHub Actions** — it will run your program on their computers on a
   schedule. This is the part you actually want.

Some words you'll see:

- **Repository** (or **repo**) — one project's folder on GitHub. You'll make one
  called `deal-tracker`.
- **Push** — upload your files from your Mac to GitHub.
- **Secret** — GitHub's safe. You paste your passwords in once; the running
  program can read them, but nobody looking at your code can see them.
- **Workflow** — the instruction "run this program every hour". Already written
  for you, in the project.

## What it will cost

- **Claude**: about **$12 a month**, charged by how many articles it reads.
  You pre-pay credits, so it can never surprise you with a bill.
- **GitHub**: **free**. The program is set to run once an hour, which fits
  inside GitHub's free allowance.
- **Google Sheets**: free.

---

# PART 1 — Check the project is there

The program is already on your Mac. Let's confirm.

Open Terminal and paste this:

```bash
cd ~/deal-tracker && ls
```

**What this does:** `cd` moves into the folder. `ls` lists what's inside.

✅ **You should see** a list including `README.md`, `config.yaml`,
`sources.yaml`, `dealtracker`.

❌ **If you see `No such file or directory`**, the folder is missing. Stop and
tell me — nothing below will work.

---

# PART 2 — Get your Claude API key

## 2.1 — Make an account and add credits

1. Go to **https://console.anthropic.com** in your browser.
2. Sign up or log in.
3. On the left, click **Settings**, then **Billing**.
4. Add a card and buy credits. **$20 is plenty** — that's more than a month.

   Without credits, every attempt below fails with "credit balance is too low".

5. *(Optional but reassuring)* On the same Billing page, set a **spend limit**
   of say $30/month. Then it can never cost more than that, whatever happens.

## 2.2 — Create the key

1. On the left, click **API keys**.
2. Click **Create Key**.
3. Name it `deal-tracker`. Click **Create**.
4. **A long key appears starting with `sk-ant-`. Copy it right now.**
   Anthropic will never show it to you again. If you lose it, you just make a
   new one — no harm done.
5. Paste it somewhere temporary — a Notes window is fine for the next 20
   minutes. Delete it afterwards.

## 2.3 — Tell Terminal about the key

Paste this into Terminal, but **replace the fake key with your real one**:

```bash
export ANTHROPIC_API_KEY=sk-ant-paste-your-real-key-here
```

**What this does:** puts the key into Terminal's memory so the program can find
it.

⚠️ **This is forgotten the moment you close the Terminal window.** If you close
it and come back later, you must paste this line again. That's normal, not a
mistake.

✅ **Check it worked:**

```bash
echo ${ANTHROPIC_API_KEY:0:12}
```

Should print `sk-ant-api03` or similar. If it prints nothing, the export didn't
take — do 2.3 again.

---

# PART 3 — The one review you must do

This is the most important step in the whole guide, and it costs about **8
cents**.

The program uses AI to read each article. Nobody has ever checked whether it
reads them *well*. Twenty real articles are already saved in the project,
waiting for exactly this test.

Paste:

```bash
cd ~/deal-tracker
.venv/bin/python -m dealtracker extract --from-cache data/articles_cache.json --limit 20
```

It will churn for a minute, then print twenty blocks of text. Each block is one
article and what the AI decided about it.

## What you're looking for

Scroll through all twenty. In each block, the line that matters most is
`"deal_type"`.

**The critical one.** Find the article titled:

> *Carrum Mobility, Circolife, ARC, QNu Labs raise early-stage funding*

That article is about **four different companies at once**. It is a summary
article, not a single deal.

- ✅ **Correct:** `"deal_type": "none"` — the AI recognised it's a summary and
  refused to make a row.
- ❌ **Wrong:** anything else. If it picked one of those four companies and made
  a deal out of it, **stop and tell me**. That's the exact failure you were
  worried about, and I'll fix the instructions we give the AI before you go on.

**Two more that should say `"none"`:**

- *Kuku to build 1,000-member AI team ahead of Rs 3,500 Cr IPO* — that's about
  hiring people, not a deal.
- *ESDS Hits Upper Circuit For Fourth Straight Day* — that's a share price
  moving, not a deal.

**And check:** where an article doesn't mention a money amount, you should see
`"amount_usd_mn": null`. `null` means "not stated". The AI should never invent
a number.

**Don't move on until you're happy with what you read.** Everything after this
is built on top of it.

---

# PART 4 — The Google Sheet and the robot

This is the longest part. It's all clicking, no thinking.

## 4.1 — Make the spreadsheet

1. Go to **https://sheets.google.com**
2. Click **Blank spreadsheet**.
3. Name it `Deal tracker` (click "Untitled spreadsheet", top left).

Now look at your browser's address bar. It says something like:

```
https://docs.google.com/spreadsheets/d/1a2B3cD4eF5gH6iJ7kL8mN9oP/edit
```

The gibberish in the middle — between `/d/` and `/edit` — is your **Sheet ID**.
In this example it's `1a2B3cD4eF5gH6iJ7kL8mN9oP`.

**Copy yours into your Notes window.** You'll need it twice.

## 4.2 — Make a Google Cloud project

Google keeps robots in a separate system called Google Cloud. You need a
"project" there to hold yours.

1. Go to **https://console.cloud.google.com**
2. Accept any terms it shows. **You will not be charged** — what we're using is
   free.
3. At the very top of the page there's a dropdown, probably saying
   *Select a project*. Click it.
4. Click **New Project** (top right of the popup).
5. Name: `deal-tracker`. Click **Create**. Wait about 15 seconds.
6. **Click the dropdown again and choose `deal-tracker`.**

⚠️ Before continuing, check the top bar says **deal-tracker**. If it says
something else, everything below goes into the wrong place.

## 4.3 — Switch on the Sheets connection

1. In the search bar at the top of the page, type `Google Sheets API`.
2. Click the result called **Google Sheets API**.
3. Click the blue **Enable** button. Wait for it.

## 4.4 — Create the robot

1. On the left, click **APIs & Services**, then **Credentials**.

   *(No left menu? Click the ☰ three-lines icon at the top left.)*

2. Near the top, click **+ Create Credentials** → **Service account**.
3. **Service account name:** `deal-tracker-bot`. Click **Create and Continue**.
4. Next screen says "Grant this service account access to project" —
   **skip it**, click **Continue**.
5. Next screen says "Grant users access" — **skip it**, click **Done**.

## 4.5 — Download the robot's key file

1. You're back on the Credentials page. Under **Service Accounts**, click
   **deal-tracker-bot**.
2. Click the **Keys** tab (along the top).
3. Click **Add Key** → **Create new key**.
4. Choose **JSON**. Click **Create**.
5. A file downloads to your Downloads folder.

⚠️ **That file is a password.** Anyone who has it can edit your Sheet. Don't
email it or post it anywhere.

Now move it into the project, with a name the program expects:

```bash
mv ~/Downloads/deal-tracker-*.json ~/deal-tracker/service_account.json
```

❌ If that says `No such file`, run this to see the real filename:

```bash
ls ~/Downloads/*.json
```

then redo the `mv` line using the name you see.

✅ **Check it landed:**

```bash
ls ~/deal-tracker/service_account.json
```

Should print the path back to you.

## 4.6 — Share the Sheet with the robot

First, find out the robot's email address:

```bash
cd ~/deal-tracker
.venv/bin/python -c "import json;print(json.load(open('service_account.json'))['client_email'])"
```

It prints something long and ugly like:

```
deal-tracker-bot@deal-tracker-482913.iam.gserviceaccount.com
```

**Copy that.**

Now:

1. Open your `Deal tracker` spreadsheet.
2. Click the blue **Share** button, top right.
3. Paste the robot's email address.
4. ⚠️ Change the dropdown on the right from *Viewer* to **Editor**.
   If you leave it as Viewer, the program cannot write and you'll get a
   "permission denied" error later.
5. **Untick "Notify people"** — robots don't read email.
6. Click **Share**.

---

# PART 5 — Test everything on your Mac first

Before involving GitHub, let's prove it works here.

## 5.1 — Tell Terminal the three things it needs

Paste these three lines one at a time. **Replace the fake values in lines 1 and
2** with your real key and your real Sheet ID. Line 3 is correct exactly as
written.

```bash
export ANTHROPIC_API_KEY=sk-ant-paste-your-real-key-here
```

```bash
export GSHEET_ID=paste-your-sheet-id-here
```

```bash
export GOOGLE_SERVICE_ACCOUNT_JSON="$(cat ~/deal-tracker/service_account.json)"
```

## 5.2 — Put the column headings in the Sheet

```bash
cd ~/deal-tracker
.venv/bin/python -m dealtracker initsheet
```

✅ Go look at your spreadsheet. **It should now have 20 column headings**,
starting with `deal_id` and ending with `read_flag`.

❌ If you see `403` or `PERMISSION_DENIED`: step 4.6 didn't take. Recheck that
you pasted the right robot email and set it to **Editor**.

## 5.3 — Do a real run

```bash
.venv/bin/python -m dealtracker run --tier 1 --limit 10
```

This fetches live news, reads it with AI, removes duplicates, and writes rows.
Takes a few minutes. Costs a few cents.

✅ Refresh your spreadsheet — there should be real deal rows in it.

## 5.4 — Run it a second time (this proves a lot)

```bash
.venv/bin/python -m dealtracker run --tier 1 --limit 10
```

✅ This time the summary should say most articles were
**"seen in an earlier run, skipped"**, and hardly anything new was extracted.

That proves two things: it won't write the same deal twice, and it won't charge
you twice for the same article.

**If you got here, the hard part is over.** Everything works. All that's left is
moving it to the cloud so it runs without you.

---

# PART 6 — Put it on GitHub so it runs by itself

## 6.1 — Make a GitHub account

Go to **https://github.com** and sign up (free). Remember your username.

## 6.2 — Make a repository

1. Click the **+** at the top right → **New repository**.
2. **Repository name:** `deal-tracker`
3. ⚠️ **Choose Private.** Not Public. The program saves your collected deal data
   into the repo, and Public means the whole internet can read it.
4. ⚠️ **Do not tick** "Add a README file" or any other checkbox. Your project
   already has files; ticking these causes a clash that's annoying to undo.
5. Click **Create repository**.

## 6.3 — Get a GitHub password-for-programs

GitHub won't let a program log in with your normal password. You need a special
one called a **token**.

1. Go to **https://github.com/settings/tokens**
2. Click **Generate new token** → **Generate new token (classic)**.
3. **Note:** `deal-tracker`
4. **Expiration:** 90 days
5. ⚠️ Tick the box labelled **`repo`** (the first big one). Without it, the
   upload is rejected.
6. Scroll down, click **Generate token**.
7. **Copy the token now** — it starts with `ghp_`. GitHub won't show it again.
   Put it in your Notes window next to the other bits.

## 6.4 — Upload your files

Paste these three lines, **replacing `YOUR-USERNAME`** with your GitHub username:

```bash
cd ~/deal-tracker
```

```bash
git branch -M main
```

```bash
git remote add origin https://github.com/YOUR-USERNAME/deal-tracker.git
```

Then:

```bash
git push -u origin main
```

It will ask for:

- **Username:** your GitHub username
- **Password:** ⚠️ **paste the `ghp_` token**, not your real password.
  Remember: nothing appears on screen while you paste. That's normal. Press
  Enter.

✅ Refresh your GitHub repo page in the browser. Your files should be there.

⚠️ **Look for `service_account.json` in that file list. It must NOT be there.**
The project is set up to exclude it. If you do see it, stop and tell me.

## 6.5 — Put your three passwords in GitHub's safe

The program running on GitHub's computer needs the same three values you gave
Terminal in step 5.1.

In your repo on the website:

1. Click **Settings** (the repo's own Settings tab, not your account settings).
2. On the left: **Secrets and variables** → **Actions**.
3. Click **New repository secret**. Do this three times:

| Name (type exactly) | Value to paste |
|---|---|
| `ANTHROPIC_API_KEY` | your `sk-ant-...` key |
| `GSHEET_ID` | your Sheet ID |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | the **whole contents** of the robot key file |

For the third one, get the contents onto your clipboard with:

```bash
cat ~/deal-tracker/service_account.json | pbcopy
```

Then click into the value box and press `Cmd+V`. It must start with `{` and end
with `}`.

## 6.6 — Let GitHub write back

The program saves its memory (what it has already seen) back into the repo.

1. Still in repo **Settings** → on the left, **Actions** → **General**.
2. Scroll to **Workflow permissions**.
3. Select **Read and write permissions**. Click **Save**.

## 6.7 — Switch it on

1. Click the **Actions** tab at the top of your repo.
2. If it asks, click **I understand my workflows, go ahead and enable them**.
3. On the left, click **deal-tracker**.
4. On the right, click **Run workflow** → **Run workflow**.

   This runs it once right now, instead of waiting for the next hour.

5. Wait about 2 minutes. Refresh the page.

✅ A **green tick** means it worked. Click into the run to see what it fetched
and wrote.

❌ A **red X** means it failed. Click into it, click the failed step to read the
error, and tell me what it says.

6. Check your spreadsheet for new rows.

**That's it. It now runs every hour, forever, on its own.**

---

# Living with it

## How do I know it's working?

Open your spreadsheet. New rows appear. That's the whole answer.

For detail, the **Actions** tab on GitHub shows every run, hourly, with a green
tick or red X.

## The read_flag column is yours

The last column is never touched by the program. Use it however you like — tick
off deals you've looked at. It won't be overwritten.

## How do I stop it?

**Actions** tab → **deal-tracker** on the left → the **`...`** menu on the
right → **Disable workflow**. It stops immediately. Re-enable the same way.

## How do I change what it watches?

Everything adjustable is in two files, `sources.yaml` (which websites) and
`config.yaml` (settings). Edit, then push the change:

```bash
cd ~/deal-tracker
git add -A
git commit -m "changed settings"
git push
```

## Things that are normal, not broken

- **Five news sites always show errors.** Moneycontrol, Business Standard,
  Businesswire, ANI and NDTV Profit block programs from reading them. Nothing to
  fix; it's their choice, not a bug.
- **Some runs write zero rows.** If there's no deal news that hour, there's
  nothing to write.
- **A run starts 10–15 minutes late.** GitHub's scheduler is best-effort. It
  catches up.
- **The same deal occasionally appears twice**, when the amount wasn't disclosed
  by either outlet. This is a known, deliberate trade-off — the alternative
  risks silently merging two *different* deals, which is worse because you'd
  never spot it.

## Things to actually worry about

- **Schedules pause after 60 days of no activity** on a private repo. Push any
  small change to wake it up.
- **Your GitHub token expires in 90 days.** You only need it to push changes
  from your Mac, not for the hourly runs. Make a new one when it expires.
- **Watch your spend** at console.anthropic.com → Usage, for the first week.

---

# When something goes wrong

| What you see | What it means | What to do |
|---|---|---|
| `ANTHROPIC_API_KEY is not set` | Terminal forgot the key when you closed the window | Paste the `export` line from 2.3 again |
| `credit balance is too low` | Out of AI credits | Buy more at console.anthropic.com → Billing |
| `401` or `authentication_error` | Key is wrong or deleted | Make a new key, redo 2.2 and 2.3 |
| `403` or `PERMISSION_DENIED` from Google | Sheet not shared with the robot, or shared as Viewer | Redo step 4.6, set **Editor** |
| `no spreadsheet id` | `GSHEET_ID` missing | Redo 5.1, or check the GitHub secret name is spelled exactly right |
| `Authentication failed` when pushing | You typed your GitHub password instead of the token | Use the `ghp_` token from 6.3 |
| Action fails at **Commit state** | GitHub isn't allowed to write back | Do step 6.6 |
| Rows appear twice | Usually the known undisclosed-amount case | See "normal, not broken" above |

**A free way to see what it's doing, with no AI cost at all:**

```bash
cd ~/deal-tracker
.venv/bin/python -m dealtracker filter --tier 1 --limit 20
```

This fetches today's articles and shows which ones it would keep and which it
would throw away — without calling the AI, so it costs nothing. Good for
checking it isn't missing deals you care about.

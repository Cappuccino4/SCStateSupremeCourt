# SC Advance Sheet Digest: operating guide

This guide is for whoever runs the newsletter, whether this copy or one of your own: setup, costs, configuration, and week-to-week maintenance. If you only want to read the digest, you need none of it; the [README](README.md) describes what subscribers get and how to sign up.

SC Advance Sheet Digest is a free weekly email that summarizes every newly published opinion of the Supreme Court of South Carolina and the South Carolina Court of Appeals for practicing attorneys. It runs by itself on GitHub Actions: it finds the week's new opinions, has Claude summarize each one, and creates the email in Buttondown. The email stays a draft for you to review unless you turn on `AUTO_SEND`. The signup page lives in `docs/`.

**AI-generated summaries for informational purposes only; not legal advice; always read the full opinion.** See the [Disclaimer](#disclaimer).

**Contents:** [What it is](#what-it-is) · [Data sources](#data-sources) · [Billing](#billing) · [Model and cost plan](#model-and-cost-plan) · [Setup](#setup-step-by-step) · [Configuration](#configuration-reference) · [Local development](#local-development) · [Project layout](#project-layout) · [Maintenance](#maintenance-cheat-sheet) · [Disclaimer](#disclaimer)

**Common tasks:** [Pause the job](#pause-the-job) · [Change the model](#change-the-model) · [Raise or lower the spend limit](#raise-or-lower-the-spend-limit) · [Rotate the API key](#rotate-the-api-key) · [If a source changes its format](#if-a-source-changes-its-format) · [Re-enable after 60 days](#re-enable-after-60-days-without-activity) · [Reprocess an opinion](#reprocess-an-opinion)

## What it is

### At a glance

| | |
|---|---|
| **Runs** | GitHub Actions, every hour from 11:17 AM to 9:17 PM Eastern on Wednesdays and Thursdays, or by hand with "Run workflow" |
| **Opinions** | Published opinions, from CourtListener (Free Law Project) |
| **Summaries** | Claude API, model `claude-sonnet-5-5` by default |
| **Email** | Buttondown. Created as a draft; sent automatically only when `AUTO_SEND=true` |
| **Memory** | `data/processed.json`, committed by the workflow so nothing is summarized or emailed twice |
| **Signup page** | GitHub Pages, served from `docs/` |
| **Typical cost** | About $1–2 a month of Claude API usage. Everything else is free at this scale |

### The weekly flow

```text
 GitHub Actions schedule (hourly on Wed + Thu, Eastern), or "Run workflow" by hand
     |
     v
 1. Find new opinions .... CourtListener: API search with your token, or the public
     |                     feeds without one. Skip anything in data/processed.json
     v
 2. Get the text ......... download each opinion PDF, extract its text,
     |                     shorten very long opinions
     v
 3. Summarize ............ Claude API: one structured summary per opinion
     |                     (capped, at most one retry, cached)
     v
 4. Build the digest ..... the email (Markdown) plus an HTML preview,
     |                     saved as a downloadable artifact
     v
 5. Buttondown ........... create a DRAFT; send it only if AUTO_SEND=true
     |
     v
 6. Record ............... commit data/processed.json so nothing repeats

 A dry run stops after step 4: no email, and data/processed.json is not changed.
```

### What subscribers receive

Every issue has the same parts, in this order:

1. **Rule changes this week**: each opinion that changes or clarifies a rule, with a one-line note and a PDF link, or a line saying none did. Opinions that weren't summarized (a failed summary, or over the per-run cap) are named there as not checked for rule changes, so the section never claims that an opinion nobody read changes nothing.
2. **Supreme Court** and **Court of Appeals** (plus **Supreme Court orders** when `SOURCE=sccourts`): one entry per opinion with its number, filing date, author, and disposition, then *Takeaway*, *Holding*, *What changed*, *Why it matters*, *Practice areas*, and a link to the full opinion. An opinion that was withdrawn, substituted, and refiled says so next to its filing date.
3. **Also filed this week (not summarized)**: only in a week with more items than the per-run cap.
4. A footer with the disclaimer, the source, and Buttondown's unsubscribe link.

Subjects look like `SC Advance Sheet Digest — November 12, 2026 — 3 opinions`. If a second email goes out for the same week (an opinion posted late, or a summary that wasn't ready for the first issue), it is a **supplement**: its subject ends in "(supplement)", it names the week's earlier issue, and it lists only what it adds. The signup page (`docs/index.html`) shows a sample issue.

Claude works from the opinion's own text only. It is told not to use outside knowledge, to cite only authorities the opinion itself cites, and to write "unclear from the opinion" rather than guess. Facts the program already knows (case name, opinion number, court, filing date, and author) overwrite whatever the model wrote, and the change label (*Changes*, *Clarifies*, *Reaffirms*) always matches the "What changed" text. A very long opinion is shortened first, and its entry says how much of the text the summary is based on. If part of an opinion's PDF can't be read, it isn't summarized from the rest. If a summary can't be made, the opinion is still listed with its link: in that week's email, or, if no email goes out that run, in the next one.

## Data sources

### CourtListener (the default)

The court's own website, sccourts.org, has a `robots.txt` file that asks automated agents to stay away (`User-agent: *` / `Disallow: /`; only Google's and Bing's crawlers are allowed). So by default the job reads **CourtListener** instead: the free legal-research site of the nonprofit Free Law Project, which collects the opinions both courts publish. In this mode the job never contacts sccourts.org; it downloads CourtListener's stored copy of each PDF. Readers still get a link to the court's own PDF whenever CourtListener knows its address, with CourtListener's copy as a second link ("CourtListener copy").

### What the free API token adds

| | Without a token: public feeds | With a free token: search API |
|---|---|---|
| What the job reads | Each court's CourtListener feed, which lists only its 20 newest decisions, published and unpublished together | Every published opinion filed in the lookback window |
| In a busy week | Opinions can drop off a feed before the job sees them. The log warns when a feed doesn't reach back far enough | Complete |
| Reader links | CourtListener's copy of the PDF | The court's own PDF, plus CourtListener's copy |
| Log | A warning on every run that no token is set | No warning |

You get the token in [step 6](#6-get-a-courtlistener-api-token). It is sent only to CourtListener's API, usually in one search request per run; the PDFs come from CourtListener's public file storage. The free account limits (5 requests a minute, 50 an hour, 125 a day, as of May 2026) are far more than the job needs. If CourtListener hasn't stored an opinion's PDF yet, the opinion is listed with a link and tried again on the next run (in at most `MAX_ATTEMPTS_PER_ITEM` runs in all, as for any failed summary).

CourtListener's [terms of service](https://wiki.free.law/c/terms/courtlistener/courtlistenercom-terms-of-service-and-policies) ask for one account per person or project and no shared tokens. Its [allowed-use policy](https://free.law/membership/allowed-api-usage/) addresses commercial use and welcomes solo practitioners and firms of five or fewer attorneys. If you're unsure whether your use fits, ask Free Law Project.

### Optional: SOURCE=sccourts for orders and rule amendments

CourtListener carries opinions only, so in the default mode each digest says that Supreme Court orders and rule amendments aren't included and links to the court's orders page. With `SOURCE=sccourts`, the job reads sccourts.org directly:

- each court's monthly "published opinions" page;
- the Advance Sheets page and the PDF of each advance sheet in the lookback window. The sheet's CONTENTS lists the Supreme Court's orders, such as rule amendments. Orders printed before the cover page are found too;
- the All Court Orders page, for each order's official number and its own PDF.

Orders then get a **Supreme Court orders** section and appear under **Rule changes this week** when they change a rule. `INCLUDE_ORDERS` (on by default) controls this. If the week's advance sheet can't be read (or isn't posted yet), the email says that orders may be missing instead of saying that none changed a rule.

**Use this mode only after you have obtained the court's permission**, because its robots.txt disallows automated agents. Then set the `SOURCE` Variable to `sccourts` and set `CONTACT_EMAIL`, which this mode requires: without it the run stops before contacting the site. Every request identifies the job (its User-Agent includes your repository's address and `CONTACT_EMAIL`), and the job waits at least 2 seconds between requests to the same site, retries included. If you switch modes, also update the coverage wording on the signup page (`docs/index.html`; look for the `SOURCE=sccourts` comments) and in `README.md`. The sccourts.org parsers were tested against pages saved on November 16, 2026. If the site is redesigned, see [If a source changes its format](#if-a-source-changes-its-format).

## Billing

### The Claude API is billed separately from your Claude subscription

- A Claude.ai subscription (such as Pro or Max) and the Claude API are **billed separately**. Your subscription covers building and maintaining this project with Claude Code. It does **not** cover the GitHub Action's API calls. Those are paid from prepaid credits in a separate Claude Console account ([steps 2–5](#2-create-a-claude-console-account)).
- One exception worth checking: Anthropic includes monthly API credits with its Max, Ultra, and Team plans. If you have one of those plans, you can link the credits to your Console organization on claude.ai under Settings > Billing > "API credits". They are used before purchased credits and expire each billing cycle. API usage is never charged to the plan itself.

### The API key lives only in GitHub Secrets

`ANTHROPIC_API_KEY` belongs in exactly one place: your repository's GitHub Secrets. GitHub hides it in logs and hands it only to the weekly job.

- **Never set it on your own computer**: not in a terminal, not in Windows environment variables, and not in a shell profile (`~/.zshrc`, `~/.bashrc`, PowerShell's `$PROFILE`). If `ANTHROPIC_API_KEY` is set where Claude Code runs, Claude Code bills your API account instead of using your subscription.
- **Never put it in a file**, including `.env`, code, and notes, and never paste it into a chat, including Claude. If it ever ends up somewhere it shouldn't, [rotate it](#rotate-the-api-key).
- **You never need it locally.** The tests and the local preview (`--mock-llm`) don't use it, and a local run without `--mock-llm` stops and tells you so. The program also ignores `ANTHROPIC_API_KEY` and `BUTTONDOWN_API_KEY` if they appear in a `.env` file, and warns you to remove them.

To confirm it isn't set, run one of these. They print only whether the key is set, never its value.

```powershell
# Windows PowerShell
foreach ($scope in "Process", "User", "Machine") {
  $found = [Environment]::GetEnvironmentVariable("ANTHROPIC_API_KEY", $scope)
  if ($found) { "${scope}: SET - remove it" } else { "${scope}: not set (good)" }
}
if (Test-Path $PROFILE) { Select-String -Path $PROFILE -Pattern ANTHROPIC_API_KEY | ForEach-Object { "Found in $($_.Path), line $($_.LineNumber)" } }
```

```bash
# macOS / Linux
[ -n "${ANTHROPIC_API_KEY:-}" ] && echo "SET - remove it" || echo "not set (good)"
grep -ns ANTHROPIC_API_KEY ~/.zshrc ~/.zprofile ~/.bashrc ~/.bash_profile ~/.profile | cut -d: -f1,2
```

To remove it on Windows, run `[Environment]::SetEnvironmentVariable("ANTHROPIC_API_KEY", $null, "User")` (use `"Machine"` in an administrator PowerShell if it was set there), and delete any line the profile check found. On macOS or Linux, delete the line the `grep` command found. Then open a new terminal and restart Claude Code.

### What each service costs

| Service | Cost for this project |
|---|---|
| Claude API (Anthropic) | About $1–2 a month on Sonnet, from prepaid credits ([details](#model-and-cost-plan)) |
| GitHub: repository, Actions, Pages | Free for a public repository |
| CourtListener | Free account and API token |
| Buttondown | Free for the first 100 subscribers |

## Model and cost plan

### Models

| Model | Role | Price per million tokens (as listed in `src/config.py`) |
|---|---|---|
| `claude-sonnet-5-5` | **Default.** Accuracy matters most in summaries attorneys rely on | $2.00 input / $10.00 output |
| `claude-haiku-5-5` | Low-cost fallback, for when cost matters more than nuance | $0.10 input / $0.50 output |

Switching is one GitHub Variable ([how](#change-the-model)).

### Expected cost

Plan on about 10 opinions a week, each about 12,000 input tokens (the opinion's text plus the instructions) and about 1,500 output tokens (the summary plus any brief reasoning before it, which is billed as output; the mock run's estimate assumes the same 1,500).

```text
Sonnet, one opinion:   12,000 input  x  $2.00 per million  =  $0.024
                        1,500 output x $10.00 per million  =  $0.015
                                                    total  ~  $0.039  (about 4 cents)
One week, 10 opinions:                                     ~  $0.39
One month (52 weeks / 12 = about 4.33 weeks):              ~  $1.69
```

On Haiku, the same month comes to about $0.08. Busy weeks and long opinions cost more, so budget $1–2 a month on Sonnet. A dry run in GitHub costs as much as a real run, but the real run that follows it is free for the opinions the dry run already summarized (see the summary cache below).

The prices above are the ones in `src/config.py` (`MODEL_PRICING`), which the program uses only for the estimate in its log. **Verify them against current Anthropic pricing** at https://platform.claude.com/docs/en/about-claude/pricing, and update `MODEL_PRICING` if they change. The Console's Cost page is the record of what you actually pay.

The worst case is bounded too. Each opinion gets at most two requests per run, each capped at about 30,000 tokens of opinion text (plus about 2,500 tokens of instructions) and 4,000 output tokens (6,000 on the retry). That is at most about $0.23 per opinion on Sonnet. One exception: when the model declines a document and Anthropic's server-side fallback model answers instead (`SUMMARY_REFUSAL_FALLBACK`), that one request bills both model runs, so the rare opinion that is declined twice can cost about twice as much. With the 25-item cap a single run stays under about $6 (about $12 in that unlikely extreme), and the $5 monthly spend limit stops spending before that. The cost summary counts both runs of a fallback request.

### Cost safeguards

| Safeguard | Setting (default) | What it does |
|---|---|---|
| Output cap on every call | `MAX_OUTPUT_TOKENS` (4,000) | Sent as `max_tokens` with every request. If a reply is cut off, the one retry allows 1.5 times as much (6,000; never more than 8,000). |
| Items per run, with an overflow list | `MAX_OPINIONS_PER_RUN` (25) | At most 25 items are summarized per run (an item found to be already processed once its PDF is read doesn't use up a slot). Any others are logged as a warning, listed by title and link under "Also filed this week (not summarized)", and recorded so they aren't picked up again. |
| Long opinions shortened | `MAX_INPUT_TOKENS_PER_OPINION` (30,000) | Text over the limit is cut before it is sent. The majority opinion is kept and any concurrence or dissent is trimmed first; otherwise the beginning and end are kept. The entry then says "Long opinion: this summary is based on about NN% of its text." |
| Nothing summarized twice | `data/processed.json` | Every item that goes into an email is recorded (so are the older items the first run leaves out), and the workflow commits the file. Later runs skip recorded items. A run that had to wait for another one starts from the file that run committed. |
| Summary cache | `.cache/summaries` | Each validated summary is saved in the GitHub Actions cache. A later identical request (same text, model, effort, and prompt version) is answered from the cache with no API call, which makes a real run after a dry run free for those opinions. |
| At most one retry, then skip | built in | At most two requests per opinion per run; it never loops. If both fail, the email lists the opinion with its link and a note that the summary isn't available. A failed item is tried again on later runs, in at most `MAX_ATTEMPTS_PER_ITEM` (2) runs in all. |
| Fatal errors stop the run | built in | A missing or rejected key, an unknown model, a reached spend limit, used-up credits, or an organization on hold stops the run at once: no more API calls, no email, and `data/processed.json` unchanged. |
| A run where every summary fails | built in | No email is created, and the run fails. Failures that would happen again for the same document (an invalid or cut-off reply, a refusal, an unreadable PDF) are still counted in `data/processed.json`, so such an opinion is tried in at most `MAX_ATTEMPTS_PER_ITEM` runs and then listed, with its link and without a summary, in the next email. An outage ("the AI service was unavailable") is not counted. The very first run records nothing. |
| Token logging for every call | log | One line per opinion: tokens in and out, estimated cost, the model that answered, seconds taken, and the request id. |
| Cost summary for every run | log, run page, `run_summary.json` | Requests, input and output tokens, estimated cost, and the prices used. A mock run also estimates what the API would have cost. |
| Limits outside the code | Claude Console | Prepaid credits with auto-reload off, a $5 monthly spend limit, and a $2 email alert ([steps 3–4](#3-buy-prepaid-credits-with-auto-reload-off)). |

## Setup, step by step

Do these in order. If you can, create the Buttondown account (step 8) early: Buttondown reviews new accounts before they can send, which usually takes 1–3 business days.

| Step | What | Where |
|---|---|---|
| 1 | [Create the repository and push the code](#1-create-the-github-repository-and-push) | GitHub |
| 2 | [Create a Claude Console account](#2-create-a-claude-console-account) | platform.claude.com |
| 3 | [Buy $5–10 of credits, with auto-reload off](#3-buy-prepaid-credits-with-auto-reload-off) | Console: Billing |
| 4 | [Set a $5 monthly spend limit and a $2 email alert](#4-set-a-monthly-spend-limit-and-an-email-alert) | Console: Spend limits |
| 5 | [Create the API key `sc-advance-sheet-github`](#5-create-the-api-key) | Console: API keys, then GitHub Secrets |
| 6 | [Get a free CourtListener API token](#6-get-a-courtlistener-api-token) | courtlistener.com |
| 7 | [Add Secrets and Variables](#7-add-github-secrets-and-variables) | GitHub: Settings |
| 8 | [Set up Buttondown and connect the signup form](#8-set-up-buttondown) | buttondown.com |
| 9 | [Publish the signup page](#9-publish-the-signup-page) | GitHub: Settings > Pages |
| 10 | [First dry run](#10-do-a-first-dry-run) | GitHub: Actions |
| 11 | [First real draft and a test email](#11-create-a-first-real-draft-and-send-a-test) | GitHub: Actions, then Buttondown |
| 12 | [Check usage against the log](#12-check-usage-in-the-console) | Console: Usage |
| 13 | [Go live](#13-go-live) | GitHub: Variables |

> **The schedule is active as soon as the workflow is on GitHub.** Until setup is finished, a scheduled run either stops at once with an "... is not set" message (before it fetches anything or spends money), or, once the keys are in, creates a Buttondown **draft** only, because `AUTO_SEND` is off.

### 1. Create the GitHub repository and push

1. On GitHub, click **+** (top right) > **New repository**.
2. Choose yourself as **Owner**, type a name (for example `sc-advance-sheet-digest`), and choose **Public**. On a free GitHub account, Pages requires a public repository, and Actions are free in public repositories. The code, the run logs, and `data/processed.json` will be public; your keys won't be, because they live in Secrets.
3. Don't add a README, .gitignore, or license on that page (this folder has its own). Click **Create repository**.

Then, in a terminal in this project folder:

```bash
git status            # check the list: no file containing a key should appear (.env is ignored automatically)
git add .
git commit -m "Initial commit"
git branch -M main
git remote -v         # shows where "origin" points, if anywhere
```

If `git remote -v` printed nothing, run `git remote add origin https://github.com/YOUR-USERNAME/YOUR-REPOSITORY.git`. If it printed a different address, run `git remote set-url origin https://github.com/YOUR-USERNAME/YOUR-REPOSITORY.git`. Then push:

```bash
git push -u origin main
```

Finally, open the repository's **Actions** tab:

- If GitHub asks whether to enable workflows, enable them. (If it says Actions are disabled, look under **Settings** > **Actions** > **General**, in "Actions permissions".)
- The left sidebar should list **Weekly SC Advance Sheet Digest** and **Tests**. Your push starts a **Tests** run, which should finish with a green check.
- No permission settings need changing. The weekly workflow grants itself permission to commit `data/processed.json` (`permissions: contents: write` in its file), which is enough for a repository you own.

### 2. Create a Claude Console account

1. Go to https://platform.claude.com and choose **Continue with Google** or **Continue with email**. The email option sends you a login link; there is no password.
2. This account is separate from your claude.ai account, even with the same email address, and your subscription doesn't carry over. (Some messages in the job's log call it the "Anthropic Console".)

New organizations may start on a lower "Evaluation" usage tier. A weekly job doesn't come close to its limits.

### 3. Buy prepaid credits with auto-reload off

1. Open the Billing page, https://platform.claude.com/settings/billing, and click **Buy credits** (on a first purchase the button may read "Purchase credits"). Buy **$5 to $10**. Tax may be added.
2. In the "Credit balance" section, check auto-reload:
   - If the button reads **Set up auto-reload**, auto-reload is off. Leave it that way.
   - If it reads **Manage auto-reload**, it is on. Click **Manage auto-reload** > **Turn off**, and confirm with **Turn off**.
   - Don't click **Turn on and purchase** or **Update and purchase**: both charge you immediately.

With auto-reload off, API calls simply stop when the balance reaches zero, so you can never spend more than you prepaid. When that happens, a run stops with "The Anthropic account's prepaid credit balance is too low" and emails nothing. Credits expire one year after purchase and aren't refundable, so buy small amounts: at $1–2 a month, $10 lasts about 5 to 10 months.

### 4. Set a monthly spend limit and an email alert

The email alert is set on a workspace, and the Console can't put limits on its Default Workspace, so first create a workspace for this project. The API key in step 5 goes into the same workspace.

1. **Workspace.** Go to https://platform.claude.com/settings/workspaces and click **Create workspace**. Name it `sc-advance-sheet-digest`, pick a color, and click **Create workspace**.
2. **$5 monthly limit.** Go to https://platform.claude.com/settings/spend-limits. Next to your organization's "Monthly spend limit", click **Adjust limit** (or **Set limit**) and enter **5**. If the Console won't accept $5, use the lowest amount it allows.
3. **Email alert at about $2.** On the same page, under "Workspace limits", choose `sc-advance-sheet-digest` from the dropdown. Click **Add notification** (or **Edit**) next to "Email notifications" and enter **2**. If the page insists on a workspace spend limit as well, enter one below the organization's limit, such as $4.

Limits are per calendar month. Once the limit is reached, the API refuses requests until the next month or until you raise it, and a run ends without an email ([what to do](#raise-or-lower-the-spend-limit)).

### 5. Create the API key

The key is shown only once, so open the place it goes before you create it.

1. In a second browser tab, open your repository's **Settings** > **Secrets and variables** > **Actions** > **New repository secret**, and type `ANTHROPIC_API_KEY` in **Name**.
2. In the Console, go to https://platform.claude.com/settings/keys and click **Create key**:
   - **Name:** `sc-advance-sheet-github`
   - **Workspace:** `sc-advance-sheet-digest` (from step 4). The key must belong to a workspace, because this project doesn't send the extra workspace header that a key without one requires.
   - **Expiration:** "Never", or a long custom duration plus a calendar reminder to [rotate the key](#rotate-the-api-key). The expiration can't be changed later, and an expired key makes runs fail with HTTP 401. For keys that last 14 days or more, Anthropic emails you 7 days before they expire.
   - **Linked account:** yourself. (Anthropic recommends service accounts for automated jobs. If you prefer, create one at https://platform.claude.com/settings/service-accounts and link the key to it.)
3. The Console then shows the full key, which starts with `sk-ant-`, **one time only**. Copy it, paste it into the **Secret** box in the other tab, and click **Add secret**.

Paste it nowhere else: not into a chat (including Claude), code, a document, an email, or a `.env` file. You don't need to keep a copy. If you lose it, delete it in the Console and create a new one.

### 6. Get a CourtListener API token

1. Create a free account at https://www.courtlistener.com/register/, and confirm your email address if asked.
2. While signed in, open https://www.courtlistener.com/profile/api-token/ and copy your token.
3. Save it as the GitHub Secret `COURTLISTENER_API_TOKEN` (step 7). Paste only the token itself, without the word "Token" in front.

The job works without a token, but then it reads CourtListener's public feeds, which can miss opinions in a busy week ([details](#what-the-free-api-token-adds)).

### 7. Add GitHub Secrets and Variables

Open your repository's **Settings** > **Secrets and variables** (in the "Security" part of the sidebar) > **Actions**.

On the **Secrets** tab, click **New repository secret**, fill in **Name** and **Secret**, and click **Add secret**:

| Secret | Value |
|---|---|
| `ANTHROPIC_API_KEY` | The Claude key (already added in step 5) |
| `COURTLISTENER_API_TOKEN` | The CourtListener token (step 6) |
| `BUTTONDOWN_API_KEY` | The Buttondown key; you'll add it in step 8 |

On the **Variables** tab, click **New repository variable**, fill in **Name** and **Value**, and click **Add variable**:

| Variable | Value |
|---|---|
| `CLAUDE_MODEL` | `claude-sonnet-5-5` |
| `AUTO_SEND` | `false` |
| `INCLUDE_COURT_OF_APPEALS` | `true` (`false` covers only the Supreme Court) |

Optional, and best saved as **Secrets** (the reason is below):

| Name | Value |
|---|---|
| `CONTACT_EMAIL` | Your email address. It is added to the User-Agent the job sends to CourtListener, Buttondown, and (if used) sccourts.org, so their operators can reach you. **Required with `SOURCE=sccourts`**: that mode stops before contacting the site without it. |
| `BUTTONDOWN_TEST_RECIPIENTS` | One or more addresses, separated by commas, that get a test copy of every new draft. |

Type each name exactly as shown. Every other setting has a sensible default; see the [Configuration reference](#configuration-reference).

**Variables or Secrets?** The workflow reads each setting from a Variable with that name if one exists, and otherwise from a Secret with that name. Use Variables for anything that isn't secret. GitHub hides every exact occurrence of a Secret's value anywhere in the log, however short, so an `AUTO_SEND` Secret holding `false` would turn every "false" in the log into `***`. Variables appear as written. (The program prints its yes-or-no settings as "on" and "off" for the same reason.) The two email settings are the exception: the repository is public, so its run logs are too, and GitHub lists each step's settings at the top of that step's log. Saving your addresses as Secrets keeps them out of public view, and hiding an email address in the log does no harm.

### 8. Set up Buttondown

1. **Account.** Sign up at https://buttondown.com/register with a username, email address, and password, and click **Create an account**. Then click the confirmation link Buttondown emails you. Your username becomes part of your newsletter's public address and of the signup form's address.
2. **Account review.** Buttondown reviews every new account before it can send email or collect subscribers, usually within 1–3 business days. A banner on the dashboard links to a verification form; filling it in is the best way to speed things up. Drafts work during the review, so you can keep going.
3. **Newsletter name and time zone.** In **Settings** > **General** (https://buttondown.com/settings/general), set the time zone to Eastern, and look there for the newsletter's name: `SC Advance Sheet Digest`.
4. **Sender.** In **Settings** > **Email** (https://buttondown.com/settings/email), set **Author name** (it may be under a "Sender" heading). This is the "From" name readers see. Mail comes from `YOUR-USERNAME@buttondown.email` unless you set up a custom sending domain, which is optional and requires DNS changes.
5. **API key.** Open **API** > **Keys** (https://buttondown.com/keys) and create a key with **write** access to emails. Save it as the GitHub Secret `BUTTONDOWN_API_KEY`, pasting only the key.
6. **Signup form.** Open `docs/index.html` and find the form's `action` address, `https://buttondown.com/api/emails/embed-subscribe/LawAdvanceSheetDigest`. Its last part is this newsletter's Buttondown username; if you run your own copy, replace it with yours. The embed code in Buttondown at https://buttondown.com/settings/embedding ("Form" section) shows the same address, for comparison. If the address ends in the placeholder `YOUR_BUTTONDOWN_USERNAME`, the page says "Signup isn't connected yet" and its button is disabled. Then publish the change:

   ```bash
   git pull
   git add docs/index.html
   git commit -m "Connect the signup form to Buttondown"
   git push
   ```

Buttondown's free plan covers the first 100 subscribers. If you ever change your Buttondown username, update `docs/index.html` to match.

### 9. Publish the signup page

1. In the repository, open **Settings** > **Pages** (in the "Code, planning, and automation" part of the sidebar).
2. Under **Build and deployment** > **Source**, choose **Deploy from a branch**.
3. In the branch dropdown (it shows "None"), choose `main`. In the folder dropdown (it shows "/(root)"), choose `/docs`. Click **Save**.
4. Within about 10 minutes the site is live at `https://YOUR-USERNAME.github.io/YOUR-REPOSITORY/`. The Pages settings page then shows a **Visit site** button.

Test the signup with your own address. A Buttondown page opens in a new tab, and Buttondown emails you a confirmation link (double opt-in is on by default). Until you click it, Buttondown lists you as "unactivated". If Buttondown's account review isn't finished, signup may not work yet. Don't subscribe and unsubscribe the same address over and over: that can trigger another review.

### 10. Do a first dry run

A dry run in GitHub makes real Claude API calls (about 4 cents per opinion on Sonnet) and builds the digest for you to download. It sends nothing and doesn't create `data/processed.json`. For a free preview with placeholder summaries, see [Local development](#local-development).

1. Open the **Actions** tab, click **Weekly SC Advance Sheet Digest** in the left sidebar, and click **Run workflow** (above the list of runs).
2. Leave **Branch** set to `main` and leave **dry_run** turned on (the default). Leave `lookback_days` and `max_opinions` blank.
3. Click **Run workflow**. After a few seconds the new run appears in the list; click it.

Because `data/processed.json` doesn't exist yet, the first run covers only the most recent week: the items filed within 5 days of the newest one. The first real run records the older items it leaves out as skipped, so a later run doesn't email last week's opinions either. If the run says "No new opinions or orders since the last run", nothing was filed in the last 10 days; run it again with a larger `lookback_days`, such as 14.

**Read the log.** Click the **digest** job, then expand the **Build the digest** step. Its sections open when clicked:

| Section | What it shows |
|---|---|
| Settings | Every setting. Secrets show only as "set" or "not set" |
| Finding opinions | What CourtListener returned, per court, and any warnings |
| Choosing what to summarize | Items found, already processed, and new; the first-run note |
| Item 1 of N: ... | One section per opinion: the download, the text, and a summary line. A warning after the last one lists any items over the per-run cap |
| Digest (Markdown) | The full email body |
| Run summary | The totals and the cost estimate |

Each opinion's summary line looks like this (the numbers are illustrative):

```text
SC 28355 Reid Fleming v. LG Chem Ltd.: summarized after 1 request (12,000 in / 1,500 out tokens, $0.0390, model claude-sonnet-5-5, 18.4s, request_id req_...)
```

**Read the cost summary.** It is the last section of the log. It also appears as a table on the run's summary page (the page that opens when you click the run), followed by any warnings. An excerpt, with illustrative numbers:

```text
Result:          Dry run: digest written to output/ (no email sent)
Mode:            dry run (no email; data/processed.json unchanged)
Model:           claude-sonnet-5-5 (effort low)
Items found:     3
New this run:    3
Summarized:      3 (0 from the cache)
API requests:    3
Input tokens:    36,000
Output tokens:   4,500
Estimated cost:  $0.1170
Prices used:     claude-sonnet-5-5: $2.00 input / $10.00 output per million tokens (verify against current Anthropic pricing)
```

**Download the digest.** On the run's summary page, under **Artifacts**, click `digest-run-N` to download it, then unzip the file and open `digest.html` in your browser:

| File | Contents |
|---|---|
| `digest.html` | A preview of the email |
| `digest.md` | The exact email body that goes to Buttondown |
| `digest.json` | Every summary and error, item by item |
| `run_summary.json` | Counts, tokens, and the cost estimate |
| `texts/` | The text extracted from each opinion, for checking a summary against its source |
| `processed.json` | A copy of `data/processed.json` as the run left it (restore it from here if the commit step failed) |

GitHub keeps these artifacts for 30 days. Read each summary against its opinion before you go further.

### 11. Create a first real draft and send a test

This needs `BUTTONDOWN_API_KEY` (step 8). Keep `AUTO_SEND` set to `false`.

1. Run the workflow as in step 10, but this time turn **dry_run** off.
2. If you did the dry run recently, the summaries come from the cache. The run summary then shows, for example, `Summarized: 3 (3 from the cache)` and `Estimated cost: $0.0000`. (GitHub deletes caches that go unused for about a week, so don't wait long after step 10.)
3. The run shows the notice "Draft created in Buttondown", and the workflow commits `data/processed.json` (the commit is "Record processed opinions [skip ci]", by github-actions[bot]).
4. In Buttondown, open **Emails**. The new email has the status "Draft". Open it, click **Preview** (top right) > **Send draft**, enter your address, and click **Send draft**. The test copy arrives with "[PREVIEW]" in its subject. (To get a test copy of every new draft automatically, set `BUTTONDOWN_TEST_RECIPIENTS`.)
5. Then either send it to your subscribers (**Publish**, top right, then **Publish** in the "Send this email" panel) or delete it. Either way, these opinions are now recorded and won't appear in a later digest.
6. On your computer, run `git pull` to bring down the bot's commit.

If a scheduled run already created a draft before you got here, start at item 4.

### 12. Check usage in the Console

1. Open https://platform.claude.com/usage (tokens by model, date, and API key) and https://platform.claude.com/cost (cost by model and day).
2. Compare them with the dry run's **Input tokens**, **Output tokens**, and **Estimated cost**. The real run in step 11 used the cache, so it added nothing. The figures should be close. New usage can take a while to appear in the Console.
3. If the Console's cost is consistently different, the prices in `src/config.py` (`MODEL_PRICING`) are probably out of date. Compare them with https://platform.claude.com/docs/en/about-claude/pricing and update them. They affect only the estimate, never your bill.

### 13. Go live

When the drafts look right, change the **Variable** `AUTO_SEND` to `true` (Settings > Secrets and variables > Actions > **Variables** tab; edit `AUTO_SEND`). From then on, every scheduled run that finds new opinions creates the email and sends it right away; test recipients, if any, get their copy first. Consider reviewing drafts for a few weeks first. You can also keep `AUTO_SEND=false` for good and click **Publish** in Buttondown each week.

**The schedule.** The job runs every hour at 17 minutes past, from 11:17 AM through 9:17 PM Eastern, on Wednesdays and Thursdays: 22 runs a week. In `.github/workflows/weekly.yml` that is the line `cron: "17 11-21 * * 3,4"` (minute, hour, day of the month, month, day of the week; 0 is Sunday, so 3 is Wednesday and 4 is Thursday), and the `timezone: "America/New_York"` line under it keeps those hours Eastern through daylight saving changes. GitHub can start a scheduled run a few minutes late when it is busy, and occasionally drops one; the next hour's run is the backstop.

Why these hours: the courts release opinions on Wednesday morning (now and then on another day, such as the Thursday after a Wednesday holiday), and CourtListener has usually stored all of them, both courts in the same pass, between about 10:15 AM and 12:30 PM Eastern. (That comes from the storage times of its copies of the opinion PDFs, June to November 2026.) The first run after that creates the email. The runs start at 11:17 rather than earlier so that they rarely catch a week half-stored. In one week of that sample, though, a fourth Supreme Court opinion arrived 84 minutes after the first three, which would have made it a supplement. To give up an hour of speed for fewer supplements, change `11-21` to `12-21`.

**A normal week:**

- **Wednesday, late morning or early afternoon.** The first run after CourtListener has the week's opinions summarizes them, creates the email (and sends it when `AUTO_SEND=true`), and commits `data/processed.json`. The Actions tab shows a green check.
- **The other runs** usually end in about a minute with "No new opinions or orders since the last run. Nothing to send." They make no Claude API call and upload no artifact. If something new appeared (or a summary that failed in an earlier run now works), the run creates a second, smaller digest with just those items, labeled a supplement: its subject ends in "(supplement)", it names the week's earlier issue, and it never says the week had no opinions from a court or no rule changes.
- A week with no new published opinions produces no email.
- The cost is a few cents per opinion; a run that finds nothing new costs nothing (GitHub Actions minutes are free in a public repository). Runs never overlap; a run started while another is going waits its turn, then starts from the newest `data/processed.json`.

**What failure looks like.** A failed run shows a red X in the Actions tab, and GitHub can email you about it, depending on your GitHub notification settings. Open the run: its summary page shows "FAILED:" and the reason (in the **Result** row of the table), and lists the errors and warnings. For the full story, open the **digest** job and expand **Build the digest**. A failed run still uploads whatever it wrote as an artifact, usually including `run_summary.json`.

| Exit code | Meaning | What to do |
|---|---|---|
| 0 | Success, or nothing new | Nothing |
| 1 | Failure. Nothing was emailed, and `data/processed.json` is unchanged (except that a run in which every summary failed records the failed attempts) | Fix the cause, then start a run by hand with **dry_run** off. The next scheduled run also picks the opinions up, but only those filed within `LOOKBACK_DAYS` (10) of it: if the failed run is more than a few days back, run by hand with a `lookback_days` that reaches back to the oldest opinion's filing date, plus a few days (at most 60) |
| 2 | The draft was created and recorded, but the test send or the `AUTO_SEND` send failed | Don't re-run (it would find nothing new). The **Result** row says which step failed. If sending failed, open the draft in Buttondown and send it from there; if only the test send failed, the email itself is fine |

Common messages:

| The log says | What to do |
|---|---|
| `ANTHROPIC_API_KEY is not set` | Add the Secret, spelled exactly ([step 5](#5-create-the-api-key)). |
| `The Anthropic API rejected the API key (HTTP 401)` | The key expired, was disabled or deleted, or was pasted wrong. [Rotate it](#rotate-the-api-key). |
| `does not recognize the model` | Correct the `CLAUDE_MODEL` Variable. |
| `monthly spend limit has been reached`, or `You have reached your specified API usage limits` | The limit is on https://platform.claude.com/settings/spend-limits. [Raise it](#raise-or-lower-the-spend-limit) or wait for next month. Then run the workflow by hand with **dry_run** off and `lookback_days` set to the number of days since the first failed run plus a few (at most 60). A blank `lookback_days` looks back only 10 days, and older opinions would be skipped for good. |
| `prepaid credit balance is too low` | Buy credits ([step 3](#3-buy-prepaid-credits-with-auto-reload-off)). Then run by hand with **dry_run** off and a `lookback_days` that covers the days since the first failed run, plus a few (at most 60); a blank one looks back only 10 days. |
| `CourtListener rejected the API token` | Paste the token into `COURTLISTENER_API_TOKEN` again, without the word "Token". |
| `CourtListener is rate-limiting this API token` | Something else used up the token's hourly or daily limit. Wait an hour (or until the next day), then run by hand. The job doesn't wait out a long `Retry-After`; it stops with this message. |
| `CourtListener's API refused the search` | CourtListener rejected the request itself (its message is quoted). Its API may have changed: see [If a source changes its format](#if-a-source-changes-its-format). |
| `may have changed` or `may have been redesigned`, `none could be read as a published South Carolina opinion`, or the warning `could not be read and were skipped` | A source changed its format. See [If a source changes its format](#if-a-source-changes-its-format). |
| `SOURCE=sccourts needs CONTACT_EMAIL` | Add the `CONTACT_EMAIL` Secret (or Variable) ([step 7](#7-add-github-secrets-and-variables)), or go back to `SOURCE=courtlistener`. |
| `BUTTONDOWN_API_KEY is not set` | Add the Secret ([step 8](#8-set-up-buttondown)). |
| `Buttondown rejected the API key (HTTP 401)` | Create a new Buttondown key with write access to emails, and update the Secret. |
| `Buttondown already has an email with this subject` | A draft from an earlier run exists. Follow the message: delete or rename that email in Buttondown, then run again (with `AUTO_SEND=false` if it was already sent). |
| `confirm the email address of your Buttondown account` | Confirm your Buttondown account's email address, then run again. |
| `Buttondown could not create the draft (HTTP …)` | Buttondown refused the email; the message includes Buttondown's code and detail. The program sends only the subject, the body, and the draft status (no metadata, which is a paid feature for accounts created since April 20, 2026), so the free plan works. Fix what the detail names in Buttondown (for example, a pending account review), then run again. |
| `All N summaries failed; not creating an email.` | Each item's warning above it gives the reason. If it was the AI service being unavailable, run again later. If the message adds that the failed attempts were recorded, nothing else is needed: each opinion is tried in at most 2 runs and then listed, with its link, in the next email. |
| `data/processed.json is not valid JSON` | A hand edit broke the file. The message explains how to fix it or restore it from git history. |
| `Could not push data/processed.json` | The email was created but its record wasn't saved. Before the next run, commit the `processed.json` from this run's artifact as `data/processed.json` (or disable the workflow until you can). Otherwise the next run can email these opinions again: Buttondown's duplicate-subject check stops that only if it finds exactly the same items. |

## Configuration reference

In GitHub, set these under **Settings** > **Secrets and variables** > **Actions**. Locally, use your shell or an optional `.env` file (see [Local development](#local-development)). A blank value means "use the default". Yes-or-no settings accept `true` or `false` (also `yes`/`no`, `on`/`off`, `1`/`0`). An invalid value stops the run with a message that names the setting.

| Name | Default | Set it as | What it does |
|---|---|---|---|
| `ANTHROPIC_API_KEY` | (none) | **Secret**, in GitHub only | Claude API key. Needed for real summaries, including dry runs in GitHub. Never on your computer; ignored in `.env`. |
| `BUTTONDOWN_API_KEY` | (none) | **Secret** | Needed to create the email; dry runs don't need it. Ignored in `.env`. |
| `COURTLISTENER_API_TOKEN` | (none: public feeds) | **Secret** | Complete results from CourtListener's search API. |
| `CLAUDE_MODEL` | `claude-sonnet-5-5` | Variable | `claude-haiku-5-5` is the low-cost fallback. A model not listed in `MODEL_PROFILES` in `src/config.py` gets neither `SUMMARY_EFFORT` nor the refusal fallback (the log warns). |
| `SUMMARY_EFFORT` | `low` | Variable | `low`, `medium`, `high`, `xhigh`, or `max`. More effort means more reasoning tokens, billed as output. Sent only to models whose profile allows it. |
| `AUTO_SEND` | `false` | Variable | `true` sends each new draft to subscribers right away. |
| `INCLUDE_SUPREME_COURT` | `true` | Variable | Cover Supreme Court opinions. |
| `INCLUDE_COURT_OF_APPEALS` | `true` | Variable | Cover Court of Appeals opinions. |
| `INCLUDE_ORDERS` | `true` | Variable | Supreme Court orders and rule amendments. Has an effect only with `SOURCE=sccourts`. |
| `SOURCE` | `courtlistener` | Variable | `sccourts` only with the court's permission ([why](#data-sources)). |
| `LOOKBACK_DAYS` | `10` (1–60) | Variable, or `lookback_days` on a manual run | How many days back each run looks. |
| `MAX_OPINIONS_PER_RUN` | `25` (1–200) | Variable, or `max_opinions` on a manual run | Most items summarized per run; any others are listed without summaries. |
| `MAX_INPUT_TOKENS_PER_OPINION` | `30000` (2,000–150,000) | Variable | Longer opinions are shortened to about this many tokens. |
| `MAX_OUTPUT_TOKENS` | `4000` (1,000–16,000) | Variable | `max_tokens` for each request. |
| `SUMMARY_REFUSAL_FALLBACK` | `true` | Variable | If the model declines to summarize a document, Anthropic's server-side fallback model may answer instead (Sonnet 5.5 and Opus 5.5 only). Turned off for the rest of a run if the API rejects it. |
| `CONTACT_EMAIL` | (none) | Secret (suggested) or Variable | Added to the job's User-Agent so site operators can reach you. Required with `SOURCE=sccourts`. |
| `BUTTONDOWN_TEST_RECIPIENTS` | (none) | Secret (suggested) or Variable | Addresses, separated by commas, that get a test copy of each new draft. |
| `DRY_RUN` | `false` | Set by the workflow | Manual runs follow the **dry_run** switch; scheduled runs are always real. Locally: `--dry-run`. |
| `MOCK_LLM` | `false` | Local only | `--mock-llm`: placeholder summaries; always a dry run. |
| `MAX_ATTEMPTS_PER_ITEM` | `2` (1–5) | Local only\* | How many runs may try an item whose summary failed. An item that used them up without ever being in an email is listed in the next one. |
| `REQUEST_DELAY_SECONDS` | `2.0` (0–60) | Local only\* | Minimum pause between requests to the same website. |
| `LOG_LEVEL` | `INFO` | Local only\* | `DEBUG` shows more detail (same as `-v`). |
| `STATE_FILE` | `data/processed.json` | Local only | Same as `--state-file`. The workflow commits this exact path. |
| `OUTPUT_DIR` | `output` | Local only | Same as `--output-dir`. The workflow uploads `output/`. |
| `SUMMARY_CACHE_DIR` | `.cache/summaries` | Local only | The workflow caches this exact path. |

\* The weekly workflow doesn't pass these to the job, so in GitHub they always keep their defaults. To change one there, add it to the `env:` list of the **Build the digest** step in `.github/workflows/weekly.yml`.

**Inputs of a manual run** (Actions > **Weekly SC Advance Sheet Digest** > **Run workflow**):

| Input | Default | Effect |
|---|---|---|
| `dry_run` | on | Build the digest and upload it as an artifact; no email; `data/processed.json` unchanged. Turn it off for a real run. |
| `lookback_days` | blank (the setting, or 10) | Look further back, for this run only (1–60). |
| `max_opinions` | blank (the setting, or 25) | A different cap, for this run only (1–200). |

## Local development

Run every command from the project folder (the one that contains this guide). You need Python 3.12 or newer.

**Local runs never need `ANTHROPIC_API_KEY`.** The tests use saved pages and fake API clients, and the local preview uses placeholder summaries. Real summaries are made only in GitHub Actions.

### Windows (PowerShell)

```powershell
# One time: create the virtual environment and install the packages
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt

# Run the tests (offline; no keys needed)
.\.venv\Scripts\python.exe -m pytest -q

# Preview the digest with placeholder summaries (no API key, no email)
.\.venv\Scripts\python.exe -m src.main --dry-run --mock-llm
Start-Process .\output\digest.html
```

If you have a newer Python, use its version in the first command (for example `py -3.13`). If `py` isn't found, use `python -m venv .venv` after checking that `python --version` shows 3.12 or newer. You can also activate the environment with `.\.venv\Scripts\Activate.ps1` and then type plain `python`. If PowerShell refuses to run that script, keep using the full path as shown.

### macOS and Linux

```bash
# One time: create the virtual environment and install the packages
python3 -m venv .venv            # python3 --version must show 3.12 or newer
.venv/bin/python -m pip install -r requirements-dev.txt

# Run the tests (offline; no keys needed)
.venv/bin/python -m pytest -q

# Preview the digest with placeholder summaries (no API key, no email)
.venv/bin/python -m src.main --dry-run --mock-llm
open output/digest.html          # on Linux: xdg-open output/digest.html
```

Or run `source .venv/bin/activate` once per terminal and then type plain `python`.

In the rest of this guide, `python` means the project's Python: `.\.venv\Scripts\python.exe` on Windows or `.venv/bin/python` on macOS and Linux (or plain `python` once the environment is activated).

### What the local preview does

`python -m src.main --dry-run --mock-llm`:

- reads CourtListener (it needs an internet connection; without a token in `.env`, it uses the public feeds), downloads and reads the opinion PDFs, and builds a real digest;
- writes placeholder summaries instead of calling Claude. Each takeaway begins "[MOCK SUMMARY — no API call was made]", and the email carries a mock banner;
- writes `digest.md`, `digest.html`, `digest.json`, `run_summary.json`, and `texts/` to the `output/` folder;
- never emails and never touches `data/processed.json`. (`--mock-llm` alone also forces a dry run.)

Its run summary includes **Estimated cost with the API**: what the same items would cost on the configured model. If nothing turns up, look further back with `--lookback-days 14`. To pretend it is another day, add `--today 2026-11-16`. A local run without `--mock-llm` stops at once with "ANTHROPIC_API_KEY is not set", by design. Don't use `--source sccourts` locally unless you have the court's permission: it contacts sccourts.org.

| Option | Effect |
|---|---|
| `--dry-run` | Build the digest in the output folder; send nothing; leave the state file alone |
| `--mock-llm` | Placeholder summaries; no API key; always a dry run |
| `--today YYYY-MM-DD` | Treat that date as today (default: today in Eastern time) |
| `--lookback-days N` | Look back N days, 1–60 (default: `LOOKBACK_DAYS`, or 10) |
| `--max-opinions N` | Summarize at most N items, 1–200 (default: `MAX_OPINIONS_PER_RUN`, or 25) |
| `--source courtlistener` or `--source sccourts` | Where opinions come from (default: `SOURCE`, or courtlistener) |
| `--output-dir PATH` | Where the output files go (default: `OUTPUT_DIR`, or `output`) |
| `--state-file PATH` | The record of processed items (default: `STATE_FILE`, or `data/processed.json`) |
| `-v`, `--verbose` | Log debugging details |

`python -m src.main --help` prints the same list. Exit codes: 0 means success or nothing new; 1 means failure (nothing emailed; the state file is unchanged, except that a run in which every summary failed records the failed attempts); 2 means the draft was created and recorded but the test send or `AUTO_SEND` failed.

**Optional local settings.** Copy `.env.example` to `.env` (`Copy-Item .env.example .env` in PowerShell, `cp .env.example .env` elsewhere) to change settings for local runs, for example to add your CourtListener token for complete local results. Git ignores `.env`, so it is never committed. Never put `ANTHROPIC_API_KEY` or `BUTTONDOWN_API_KEY` in it; the program ignores both there. `DRY_RUN` and `MOCK_LLM` are read from the command line or the shell, not from `.env`.

**Tests.** `python -m pytest -q` runs the whole suite. It never touches the network and needs no keys: it uses saved copies of real pages in `tests/fixtures/` and fake API clients. On GitHub, the **Tests** workflow runs the suite on every push and pull request (a push that changes only `docs/`, Markdown files, or `data/processed.json` is skipped).

## Project layout

```text
README.md                       For subscribers: what the digest is and how to sign up
OPERATING.md                    This guide: setup, configuration, and maintenance
.github/workflows/weekly.yml    The weekly job: schedule, manual runs, artifact, commit of data/processed.json
.github/workflows/tests.yml     Runs the tests on GitHub
.github/assets/                 Images for README.md (an illustrative sample issue)
src/main.py                     Command line and the pipeline (start here)
src/config.py                   Settings, defaults, and model prices
src/scrape.py                   Chooses the source and filters by date
src/sources/courtlistener.py    CourtListener API and public feeds (the default source)
src/sources/sccourts.py         sccourts.org reader (only with SOURCE=sccourts)
src/extract.py                  PDF text, the opinion header, shortening long opinions
src/summarize.py                Claude prompt and schema, retries, the summary cache, the mock
src/digest.py                   Builds the email (Markdown) and its HTML preview
src/send.py                     Buttondown: create the draft, send it, send a test
src/state.py                    Reads and writes data/processed.json
src/net.py, src/logutil.py, src/models.py
                                Polite HTTP client, log formatting, shared data types
docs/                           The signup site (GitHub Pages)
tests/                          The test suite; tests/fixtures/ holds saved pages and PDFs
data/processed.json             Created by the first real run and committed by the workflow
.env.example                    Template for optional local settings
pyproject.toml                  Project details and test settings
requirements.txt                Packages the job needs (exact versions)
requirements-dev.txt            The same, plus the test tools
```

## Maintenance cheat sheet

**Before you change anything locally, run `git pull`.** The job commits `data/processed.json` after every email, so your copy falls behind. Then edit, run the tests, commit, and push.

| Task | How |
|---|---|
| Stop automatic sending, keep drafts | Set the Variable `AUTO_SEND` to `false` |
| Cover only the Supreme Court | Set the Variable `INCLUDE_COURT_OF_APPEALS` to `false` |
| Change the schedule | Edit the `cron` line in `.github/workflows/weekly.yml`. Its hours are Eastern because of the `timezone: "America/New_York"` line under it (see "The schedule" in [step 13](#13-go-live)). Keep an odd minute (GitHub is busiest at the top of the hour), and avoid Thursday 9 PM to midnight Pacific (CourtListener's weekly maintenance). |
| Edit the signup page | Edit `docs/`, then commit and push. GitHub Pages republishes within about 10 minutes. |

### Pause the job

Actions tab > **Weekly SC Advance Sheet Digest** > **...** ("Show workflow options") > **Disable workflow**. To resume: the same place > **Enable workflow**. A disabled workflow doesn't run at all. After a long pause, catch up with a manual run (**dry_run** off) whose `lookback_days` covers the gap, up to 60 days. A long gap can hold more opinions than the 25-item cap; set `max_opinions` higher (each opinion takes about 20 seconds, and the job may run for up to 2 hours) or let the extra ones be listed without summaries. If a large run still times out, run it again: summaries it already paid for come from the cache. To keep the job running but stop automatic sending, set `AUTO_SEND` to `false` instead.

### Change the model

Edit the Variable `CLAUDE_MODEL`: `claude-haiku-5-5` for the low-cost fallback, or `claude-sonnet-5-5` for the default. Try a change with a dry run first, and check that its run summary shows **API requests** of 1 or more: a dry run with nothing new to summarize (or with every summary from the cache) makes no API call, so it proves nothing about the model name. A wrong model name stops the run before any email ("does not recognize the model"). Cached summaries belong to the model that wrote them, so a dry run made with the old model doesn't carry over to a real run with the new one. A model with no prices in `src/config.py` works, but its estimated cost shows as "unknown"; add its prices to `MODEL_PRICING`. A model not listed in `MODEL_PROFILES` gets neither `SUMMARY_EFFORT` nor the refusal fallback: the API's default effort applies (often "high", which costs more), the log warns, and the run summary shows the effort as "API default". Add the model to `MODEL_PROFILES` if it supports them.

### Raise or lower the spend limit

Go to https://platform.claude.com/settings/spend-limits and click **Adjust limit** next to "Monthly spend limit". The workspace's email alert is on the same page. If a run failed because the limit was reached, raise the limit, then run the workflow by hand with **dry_run** off and `lookback_days` set to the number of days since the first failed run plus a few (at most 60): nothing was recorded, so the same opinions are still waiting, but a blank `lookback_days` looks back only 10 days and older opinions would be skipped for good. To add credits, click **Buy credits** on https://platform.claude.com/settings/billing, and leave auto-reload off.

### Rotate the API key

Rotate before the key expires, or right away if it was exposed. For an exposed key, delete it in the Console first.

1. In the Console, go to https://platform.claude.com/settings/keys > **Create key**. Use the `sc-advance-sheet-digest` workspace and a new name, such as `sc-advance-sheet-github-2027`.
2. In GitHub, go to **Settings** > **Secrets and variables** > **Actions** > **Secrets** tab, open `ANTHROPIC_API_KEY`, paste the new key as its value, and save.
3. Run the workflow with **dry_run** on and look at the run summary. The new key was used only if **API requests** is 1 or more. If it is 0 (nothing new to summarize, or every summary came from the cache), the key hasn't been tested yet: keep the old key enabled until a later run with **API requests** of 1 or more succeeds.
4. Back on the Console's API keys page, **Disable** the old key (this can be undone) or **Delete** it (permanent).

The same pattern works for `COURTLISTENER_API_TOKEN` (every run, dry or not, uses it for its search) and for `BUTTONDOWN_API_KEY` (new key at https://buttondown.com/keys), with one difference: a dry run never contacts Buttondown, so keep the old Buttondown key until the next real run has created a draft. For an exposed key, delete it first anyway and watch that next run.

### If a source changes its format

If CourtListener or sccourts.org changes its pages or its API, the parser usually can't read them, and the run fails with a clear error, such as "CourtListener's Supreme Court feed is not an Atom feed (...). CourtListener may have changed it.", "CourtListener's search for published opinions returned 3 result(s), but none could be read as a published South Carolina opinion (...)", or "... sccourts.org may have been redesigned ...". Nothing is emailed and `data/processed.json` is unchanged, so no opinion is lost: once the parser is fixed, a run whose `lookback_days` reaches back far enough (60 days at most) picks them up.

A smaller change can still slip through as a green run: a warning such as "N CourtListener search result(s) could not be read and were skipped", or a run that reports "Items found: 0" in a week when the court did publish opinions. If no draft arrives in a normal week, open the run's summary page and check its warnings and **Items found**.

1. **Read the error**, on the run's summary page and in the "Finding opinions" section of the log.
2. **Capture the changed pages as new fixtures**: save fresh copies under new file names in `tests/fixtures/` and keep the existing files, because other tests depend on their exact entries. `tests/fixtures/README.md` lists every file, where it came from, and which tests use it; add the new files and their capture date there. For sccourts.org, do this only with the court's permission. For example, for CourtListener's public feeds (in Windows PowerShell, type `curl.exe` instead of `curl`; put the capture date in the names):

   ```bash
   curl -fsSL -o tests/fixtures/courtlistener/feed_sc_YYYY-MM-DD.xml https://www.courtlistener.com/feed/court/sc/
   curl -fsSL -o tests/fixtures/courtlistener/feed_scctapp_YYYY-MM-DD.xml https://www.courtlistener.com/feed/court/scctapp/
   ```

3. **Adjust the parser** in `src/sources/courtlistener.py` or `src/sources/sccourts.py`, and add tests for the new captures in `tests/test_courtlistener.py` or `tests/test_sccourts.py`. If you replace an existing fixture instead, also update `tests/test_main.py` and `tests/test_scrape.py`, which use its entries (the Nettles, Fleming, and Moore opinions, their PDF URLs, and the date 2026-11-16).
4. **Run the tests**: `python -m pytest -q`.
5. **Commit and push** (the Tests workflow runs the suite again on GitHub), then do a dry run.

Claude Code can do steps 2 to 4 for you if you give it the error message from the log.

### Re-enable after 60 days without activity

In a public repository, GitHub turns off scheduled workflows after 60 days with no repository activity. The job's own commits may count as activity, but GitHub doesn't promise that, and weeks without new opinions produce no commits. If it happens, open the Actions tab > **Weekly SC Advance Sheet Digest** > **Enable workflow**. (A commit that changes a `cron` line also turns the schedule back on.) If the gap was long, follow with a manual run whose `lookback_days` covers it.

### Reprocess an opinion

To summarize and send an opinion again, delete its record:

1. Run `git pull`.
2. In `data/processed.json`, under `"items"`, delete the opinion's whole entry, from its key through its closing `}`, and mind the commas between entries. Keys look like `"SC:28355"` (Supreme Court Op. No. 28355) or `"COA:6170"` (Court of Appeals Op. No. 6170). A `"CL:..."` key is an opinion recorded by its CourtListener id because its number wasn't known. An entry with `"status": "skipped_first_run"` is an older opinion the first run left out without emailing it; delete it the same way to have it summarized.
3. Check that the file is still valid JSON: `python -m json.tool data/processed.json` prints the file, or the line with the problem.
4. Commit and push: `git commit -am "Reprocess SC 28355"`, then `git push`.
5. Run the workflow by hand with **dry_run** off and a `lookback_days` that reaches back to the opinion's filing date.

If an earlier digest already covered that opinion's week, the new digest is labeled a supplement to it (its subject ends in "(supplement)"); otherwise, if its subject was used before, " (update)" is added to it. If the opinion's text, the model, and the prompt are unchanged, the cached summary is reused (free, and identical to the old one). To force a fresh summary, delete the cache first: look for **Caches** in the Actions tab's sidebar and delete the entries whose names start with `summaries-`.

Related:

- An opinion whose summary failed is retried automatically on later runs, in up to 2 runs in all (a run in which every summary fails counts too, unless the failure was the AI service being unavailable). If it was never in an email by then, the next email lists it with its link. After that, delete its entry as above to try again.
- To start over completely, delete `data/processed.json`. The next run then treats only the most recent week as new, and once it creates a digest it records the older items it left out, so they are not emailed later.

## Disclaimer

**AI-generated summaries for informational purposes only; not legal advice; always read the full opinion.**

- The summaries are written by an AI model from the text of each opinion and may contain errors or omissions. Opinions can be revised, withdrawn, or reheard after they are published.
- Nothing in the digest or on the signup site creates an attorney-client relationship.
- SC Advance Sheet Digest is an independent publication. It is not affiliated with or endorsed by the South Carolina Judicial Branch and is not the Judicial Branch's official Advance Sheets; official opinions are at https://www.sccourts.org/. It is likewise not affiliated with or endorsed by Free Law Project (CourtListener), Anthropic, or Buttondown.
- You publish what this software produces. Review the drafts, especially before you turn on `AUTO_SEND`.

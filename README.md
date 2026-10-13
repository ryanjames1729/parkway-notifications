# Parkway closure notifications

Emails you when the [Blue Ridge Parkway road closures page](https://www.nps.gov/blri/planyourvisit/roadclosures.htm)
changes **for your section of the Parkway only**. It runs on GitHub Actions every 30 minutes and
emails you only when something changes: a new closure that overlaps your mileposts, or a closure
that is no longer listed (usually meaning it reopened).

## Setup

1. **Pick your area.** Edit `config.toml` and set your milepost range(s). Any closure whose
   milepost range overlaps yours counts. You can also add place names under `keywords`
   (for example `"Craggy Gardens"`) to catch notices that don't give a milepost.

2. **Create a Gmail app password.** Your account needs 2-Step Verification turned on. Then go to
   <https://myaccount.google.com/apppasswords> and create a 16-character app password.
   Any other SMTP provider works too; just set `SMTP_HOST` and `SMTP_PORT`.

3. **Add repository secrets.** Go to *Settings → Secrets and variables → Actions → New repository secret*:

   | Secret          | Value                                                       |
   |-----------------|-------------------------------------------------------------|
   | `SMTP_USERNAME` | your Gmail address                                          |
   | `SMTP_PASSWORD` | the app password from step 2                                |
   | `EMAIL_TO`      | where to send alerts; separate multiple addresses with commas |
   | `SMTP_HOST`     | *(optional)* defaults to `smtp.gmail.com`                   |
   | `SMTP_PORT`     | *(optional)* defaults to `465`                              |
   | `EMAIL_FROM`    | *(optional)* defaults to `SMTP_USERNAME`                    |

4. **Test it.** Open *Actions → Check Parkway closures → Run workflow*. Tick *dry run* to see the
   email in the log without sending it. A normal run (not a dry run) sends a
   "BRP watch started" email that lists what's currently closed in your area.

### Getting texts instead of emails

Add your carrier's email-to-SMS gateway to `EMAIL_TO`, for example
`8285551234@vtext.com` (Verizon), `@txt.att.net` (AT&T) or `@tmomail.net` (T-Mobile).
The subject lines are kept short so they read well as texts. Some carriers have been
phasing these gateways out. If yours doesn't deliver, Twilio SMS can be added.

## How it works

- `notifier.py` downloads the page and splits it into table rows, list items and paragraphs.
  It pulls milepost ranges out of each one ("Milepost 95.9 to 106", "MP 0.0-2.2", or a
  table column headed *Milepost*).
- Notices that overlap your ranges are compared with `state.json`, which the workflow commits
  back to the repo. If anything was added or removed, you get one email.
- If the page loads but no mileposts can be found (for example, NPS redesigned the page), the
  run **fails instead of reporting "all clear"**. GitHub then emails you about the failed run,
  so a broken parser doesn't fail silently.

## Running locally

```sh
pip install -r requirements.txt pytest
python -m pytest
python notifier.py --dry-run                 # fetch the live page, print what would be sent
python notifier.py --html saved.html --dry-run
```

## Notes

- GitHub can delay scheduled runs by several minutes, or longer when it's busy.
- In public repositories, GitHub turns off scheduled workflows after 60 days with no repository
  activity. If that happens, re-enable the workflow from the Actions tab.

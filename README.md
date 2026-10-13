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

## Sharing alerts with other people

The easiest way is a free Google Group. People join and leave on their own, and their
addresses never appear in this repo.

1. Create a group at <https://groups.google.com> → *Create group*, for example `brp-nc80-balsam`.
2. Group settings:
   - **Who can search for group / join:** "Anyone on the web" can search, and "Anyone can ask"
     or "Anyone can join" (choose "ask" if you want to approve each person).
   - **Who can post:** *Group managers*. Make the address in `SMTP_USERNAME` an owner or
     manager so the alerts get through and nobody else can email the list.
   - **Who can view members:** *Group managers*, so members can't see each other's addresses.
3. Set the `EMAIL_TO` secret to the group's address (`brp-nc80-balsam@googlegroups.com`),
   or to your own address plus the group, separated by a comma.
4. Share the group's page link (`https://groups.google.com/g/brp-nc80-balsam`). People without
   a Google account can join by emailing `brp-nc80-balsam+subscribe@googlegroups.com`,
   and anyone can leave with the unsubscribe link Google adds to every email.

Everyone in the group gets the same alerts, for the area set in `config.toml`.

After adding people, run the workflow with **Send the 'alerts are on' summary** ticked so they
get the current status right away instead of waiting for the next change.

## How it works

- `notifier.py` downloads the page and reads the closures tables row by row: milepost
  range, crossroads, status, and notes. It also reads the "Facility Closures" and
  "Trail and Backcountry Closures" lists.
- It keeps rows that are **Closed**, or open but with a closure or restriction in the notes
  (single-lane closures, closed campgrounds, detours). Sections that are simply Open or
  Ungated are ignored.
- Rows that overlap your milepost ranges are compared with `state.json`, which the workflow
  commits back to the repo. If a closure is new, changes status, or drops off the page, you
  get one email: a formatted HTML message with a plain-text version for SMS gateways.
- If the page loads but the closures table can't be found (for example, NPS redesigned the
  page), the run **fails instead of reporting "all clear"**. GitHub then emails you about the
  failed run, so a broken parser doesn't fail silently.

## Running locally

```sh
pip install -r requirements.txt pytest
python -m pytest
python notifier.py --dry-run                 # fetch the live page, print what would be sent
python notifier.py --html saved.html --dry-run
python notifier.py --dry-run --preview email.html   # open email.html to see the email
```

## Notes

- GitHub can delay scheduled runs by several minutes, or longer when it's busy.
- In public repositories, GitHub turns off scheduled workflows after 60 days with no repository
  activity. If that happens, re-enable the workflow from the Actions tab.

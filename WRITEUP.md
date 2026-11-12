# Writeup

**What I built:** An automatic daily scraper reads Bellhaven's community webpages and the CRM, and queues
each difference as a proposal. A reviewer approves, edits or rejects it in the web app, and only approved
fixes are written to the CRM. I made all 29 fixes this way, with no errors. Running it again now finds nothing to fix.

**How I match a website location to a CRM account:** I look at the address first and the name second. When
a facility is sold, its name often changes, but the building stays in the same place. The same street and
ZIP are strong evidence. A matching/similar name, phone or contact person only adds a little.

When there are duplicates, I keep the account that is already under Bellhaven, or has billing or contacts.
The extra accounts are marked as duplicates and set to Inactive. For the billing rule, if an account has
revenue and outstanding AR, I don't move it. I create a new account under Bellhaven and link the old one to
it. Otherwise I move the account directly. Lima had revenue but nothing owed, so it moved. If a location is
no longer on the website, I set it to Needs Review, not Inactive, because a missing web page does not prove
the facility closed.

**How I used AI:** I built this with Claude Code. I made the main decisions: address first, which duplicate
to keep, Needs Review instead of Inactive, and a daily scraper that only reads the CRM. I also checked the work
myself and wrote tests with a fake CRM. I compared a test run with the result I expected from the data, and
I read every fix before I approved it. At the end, I compared the CRM with its starting copy: 25 accounts
changed, 6 new accounts were added, 1 contact moved, and nothing else changed.

**Next steps:** First, use it for other companies. Only the scraper is made for Bellhaven, so a small
settings file per company would be enough. Second, make the review faster when there are hundreds of fixes,
like the approval queues in Salesforce and the data quality tools in HubSpot. Simple, safe fixes such as a
ZIP typo could be approved in bulk. Risky ones, like a new account or a billing case, would go to the account
owner with an email or Slack alert, and all of it would live in a shared database with login.

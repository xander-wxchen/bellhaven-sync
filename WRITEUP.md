# Writeup

**What I built.** Every day, a job reads Bellhaven's website and our CRM. When they don't match, it adds a
suggested fix to a review page. Only a person's approval writes to the CRM. I made all 29 fixes this way,
and running it again now finds nothing to fix.

**How I match.** I look at the address first, because a facility's name often changes after a sale, but the
building doesn't move. Name, phone and contacts only add a little. So Juniper's Amberly Manor in Colorado,
which has the same name as a Bellhaven location in Ohio, was left alone. For duplicates, I keep the account
already under Bellhaven or with billing or contacts, and set the others to Inactive. If an account has
revenue and money still owed, I don't move it: I create a new account under Bellhaven and link the old one
to it. A location missing from the website gets Needs Review, not Inactive, because a missing page doesn't
prove it closed.

**How I used AI.** I built this with Claude Code. I made the main decisions, like address first and
Needs Review instead of Inactive. I checked the work with 63 tests, read every fix before approving it, and
at the end compared the CRM with its starting copy to make sure nothing else changed.

**Next steps.** First, use it for other companies. Only the scraper is made for Bellhaven, so a small
settings file per company would be enough. Second, make the review faster when there are hundreds of fixes,
like the approval queues in Salesforce and the data quality tools in HubSpot. Simple, safe fixes such as a
ZIP typo could be approved in bulk. Risky ones, like a new account or a billing case, would go to the
account owner with an email or Slack alert, and all of it would live in a shared database with login.

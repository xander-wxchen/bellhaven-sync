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

**Next steps.** First, sync phone numbers and contacts. At 15 of 35 locations the phone is different from
the website, and sales needs it to reach the right person. Second, use it for other companies. Only the
scraper is made for Bellhaven, so a small settings file per company would be enough, with a shared review
database so a team can use it.

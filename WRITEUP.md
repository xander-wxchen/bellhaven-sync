# Writeup

**What I built.** Every day, a job reads Bellhaven's website (35 communities) and our CRM. When they don't
match, it adds a suggested fix to a review page. A person approves, changes or rejects each fix. Only
Approve writes to the CRM. I made all 29 fixes this way, with no errors. Running it again now finds
nothing to fix.

**How I match a website location to a CRM account.** I look at the address first and the name second. When
a facility is sold, its name often changes, but the building stays in the same place. The same street and
ZIP gives the most points. A matching name, phone or contact person only adds a little. A similar name alone
is not enough, so Amberly Manor in Colorado and Union Square Senior Living, which belong to another company,
were left alone.

When there are duplicates, I keep the account that is already under Bellhaven, or has billing or contacts.
The extra accounts are marked as duplicates and set to Inactive. For the billing rule, if an account has
revenue and also money still owed, I don't move it. I create a new account under Bellhaven and link the old
one to it, as with Tiffin and Marietta. Otherwise I move the account directly. Lima had revenue but nothing
owed, so it moved. If a location is no longer on the website, I set it to Needs Review, not Inactive,
because a missing web page does not prove the facility closed.

**How I used AI.** I built this with Claude Code. I made the main decisions: address first, which duplicate
to keep, Needs Review instead of Inactive, and a daily job that only reads the CRM. I also checked the work
myself. There are 63 tests with a fake CRM. I compared a test run with the result I expected from the data,
and I read every fix before I approved it. At the end, I compared the CRM with its starting copy: 25
accounts changed, 6 new accounts were added, 1 contact moved, and nothing else changed.

**Next steps.** I would sync phone numbers, because most of them are different from the website. I would
add a head office address to the Bellhaven parent account, and check Harborview Care Group, which now has
only inactive accounts. I would also keep the review data in a shared database with login, and support the
websites of other companies.

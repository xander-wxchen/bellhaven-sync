# Writeup

**What I built.** Every day, a job reads Bellhaven's website (35 communities) and our CRM. When they don't
match, it adds a suggested fix to a review page. A person approves, changes or rejects each fix. Only
Approve writes to the CRM. I made all 29 fixes this way, with no errors. Running it again now finds
nothing to fix.

**How I match a website location to a CRM account.** I look at the address first and the name second. When
a facility is sold, its name often changes, but the building stays in the same place.
- **Address:** the same street and ZIP gives the most points. A matching name, phone or contact person only
  adds a little.
- **Same name, different place:** a similar name alone is not enough. So Amberly Manor in Colorado and
  Union Square Senior Living, owned by another company, were left alone.
- **Duplicates:** I keep the account that is already under Bellhaven, or has billing or contacts. The extra
  accounts are marked as duplicates and set to Inactive.
- **Billing rule:** if an account has revenue and also money still owed, I don't move it. I create a new
  account under Bellhaven and link the old one to it (Tiffin, Marietta). Otherwise I move the account
  directly (Lima had revenue but nothing owed).
- **Not on the website anymore:** I set the status to Needs Review, not Inactive. A missing web page does
  not prove the facility closed.

**How I used AI.** I built this with Claude Code. I made the main decisions:
- address first
- which duplicate to keep
- Needs Review instead of Inactive
- the daily job only reads the CRM

I checked the work myself:
- 63 tests with a fake CRM
- a test run compared with the result I expected from the data
- I read every fix before I approved it
- at the end, I compared the CRM with its starting copy: 25 accounts changed, 6 new accounts, 1 contact
  moved, and nothing else

**Next steps.**
- Sync phone numbers. Most of them are different from the website.
- Add a head office address to the Bellhaven parent account.
- Check Harborview Care Group, which now has only inactive accounts.
- Keep the review data in a shared database with login.
- Support websites of other companies.

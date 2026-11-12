# Writeup

**What I built.** A daily job scrapes Bellhaven's 35 communities, reads the CRM, and queues each
difference as a proposal. A reviewer approves, edits or rejects it in a small web app; only Approve writes
to the CRM. I made all 29 fixes that way with no failed writes, and a fresh run now finds nothing to change.

**Matching approach.** Address first, name second: a facility's name changes after a sale, its building
doesn't.
- **Evidence:** a normalized street and ZIP match carries most of the weight. Name, phone, and "the
  website's administrator is a contact" only support it.
- **Lookalikes:** a shared brand word alone never counts. That kept Juniper's Amberly Manor (Colorado) and
  Union Square Senior Living untouched.
- **Duplicates:** the record already under Bellhaven, or with billing or contacts, survives. The others
  become Inactive duplicates.
- **Billing SOP:** one tested function. Revenue and open AR above zero means a new account plus a CHOW
  link (Tiffin, Marietta); otherwise the account moves directly (Lima has revenue but no AR).
- **Not on the website:** Needs Review, not Inactive, because a missing page isn't proof of closure.

**How I used AI.** I built this with Claude Code as a pair programmer. I made the judgment calls:
- address-first matching
- which duplicate survives
- Needs Review instead of Inactive
- a read-only daily job
- a clearer review screen after my first version

I verified instead of trusting:
- 63 offline tests against a fake CRM
- a dry run compared with an end state I worked out by hand
- reading every card before approving
- a final diff of the live CRM against the original snapshot (25 accounts changed, 6 created, 1 contact
  moved, nothing else touched)

**Next.**
- Sync phone numbers; most differ from the website.
- Add an HQ address to Bellhaven's parent account.
- Review Harborview Care Group, which now has only inactive children.
- Move the queue to a shared database with sign-in.
- Add per-site configs for other operators.

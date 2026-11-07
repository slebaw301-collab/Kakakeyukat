# Public transaction testimonials — review branch

The owner requested replacing buyer rating/review collection with automatic public transaction receipts. Private admin notification text and routing must stay as before. This branch has not been deployed to production.

The receipt contains buyer ID only, an HTML quote block, actual stored payment amount, product and order time. It has two URL buttons: Order Sekarang to this bot, and Hubungi Admin using the existing link_admin setting.

Publication runs after successful delivery persistence, outside the buyer's critical path. It has no access to mark an order paid or to resend a product. Existing payment validation, HMAC, atomic payment/delivery claims, private admin template and rate-limited send behavior are covered by regression checks.

No migration or historical backfill runs. A unique row in the existing testimonials table reserves publication. Old review data remains intact. Timeout/cancellation/unknown post-send outcomes remain reserved and require manual reconciliation rather than an automatic second post.

Local validation: 190 passed using Python 3.11.11, emulated Telegram, and PostgreSQL WASM. Native CI adds real PostgreSQL 18 multi-connection checks on a disposable localhost database. CI uses no Railway variables, production data or Telegram credentials and cannot deploy. The test password is synthetic and confined to the ephemeral service.

The automatic approval review rejected direct production replacement even after owner approval. This branch supplies reproducible checks for review; it does not bypass that block. Production remains on the restored original admin/review version until deployment passes review.

Rollback target is commit 45e0ab6a9e46415581f23442dd6849ad56f9c6e9 (same application content as 5ee80ff), Railway deployment b57a1e68-5674-4f92-b583-0123cfeed7d9. No schema rollback is required.

/* Base URLs for both frontends.
 *
 * Loaded by the merchant console and the storefront alike, so the two never drift
 * apart on where a service lives. Ports are fixed repo-wide: merchant 8001,
 * agent 8002, payments 8003.
 *
 * The frontends never talk to payments directly. Money moves only through the
 * agent, which holds the session, the preview and the confirmation token — a
 * browser that could call /payment/confirm itself would route around the trust
 * gate entirely.
 */
window.TOES = {
  MERCHANT_BASE: "http://localhost:8001",
  AGENT_BASE: "http://localhost:8002",

  // Which merchant this console and storefront are acting for. One merchant per
  // demo; the backend is multi-merchant throughout (profiles, indexes and carts
  // are all keyed by merchant_id), so this is a frontend convenience, not a limit.
  MERCHANT_ID: "eyewear_co",
};

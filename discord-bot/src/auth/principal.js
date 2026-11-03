// Authentication: resolve a Discord user id to the bot's Principal.
//
// A Principal is the authenticated subject the router hands to command handlers.
// v1 shape is just { person }. This is the extension point for authorization:
// later, enrich the principal here with the person's memberships / roles /
// isTeamAdmin (GET /memberships?person_id=...) so policies can gate on them —
// command handlers and the router contract stay unchanged.
//
// TRUST BOUNDARY: Discord guarantees the incoming user id is authentic, so the
// Discord-user -> Person mapping is only as trustworthy as the *link* behind it.
// In v1 links are created on email-match alone (no proof of email ownership);
// see the `// TODO: verification` in linkService.js. Harden that before treating
// this principal as a strong identity assertion.
export async function resolvePrincipal(directory, discordUserId) {
  const person = await directory.getPersonByDiscordId(discordUserId);
  if (!person || person.active === false) return null;
  return { person };
}

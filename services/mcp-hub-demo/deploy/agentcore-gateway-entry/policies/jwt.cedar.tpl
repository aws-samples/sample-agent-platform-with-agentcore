// User-facing gateway: a signed-in user may call the hub's tool when the
// token's team claim holds the allowed team.
//
// The engine is default-deny: without this permit nobody calls anything, and
// tools/list shows a caller only what some policy permits them.
//
// Every JWT claim reaches Cedar as a STRING tag: a list-valued claim arrives
// as its JSON text (["team-a","team-b"]), a single string as itself. There
// is no .contains() on a string, so membership has to be spelled out as the
// positions an entry can take in that text - never as "the quoted name
// appears somewhere", which a single string such as  other "team-a" group
// would satisfy:
//
//   the only entry      ["team-a"]            ==
//   the first entry     ["team-a",...]        like  ["team-a",*]
//   the last entry      [...,"team-a"]        like  [*,"team-a"]
//   an entry between    [...,"team-a",...]    like  [*,"team-a",*]
//   a bare string       team-a                ==
//
// Every `like` is anchored at both ends on the brackets, so the value has to
// be a list for a list position to count.
//
// The patterns assume the IdP serializes the list compactly (no space after
// a comma), as Keycloak's tokens reached Cedar in this deployment (no other
// IdP has been checked). In a list written with spaces the allowed team still
// matches as the only or the first entry, whose patterns hold no comma before
// it, but not in any later position: such a member is denied, never an
// outsider admitted, so the failure is closed.
//
// What this cannot rule out: a team whose own name embeds the pattern, e.g. a
// single team literally named  x","team-a  inside a list. Team names come
// from the IdP's group names, which the IdP's administrators control; give
// them an alphabet without quotes and commas (the allowed team's name is
// checked for that before this template is rendered).
//
// hasTag first: a policy that reads a missing tag errors and is skipped.
permit(
  principal is AgentCore::OAuthUser,
  action == AgentCore::Action::"${ACTION}",
  resource == AgentCore::Gateway::"${GATEWAY_ARN}"
) when {
  principal.hasTag("${TEAM_CLAIM}") &&
  (principal.getTag("${TEAM_CLAIM}") == "[\"${ALLOWED_TEAM}\"]" ||
   principal.getTag("${TEAM_CLAIM}") like "[\"${ALLOWED_TEAM}\",*]" ||
   principal.getTag("${TEAM_CLAIM}") like "[*,\"${ALLOWED_TEAM}\"]" ||
   principal.getTag("${TEAM_CLAIM}") like "[*,\"${ALLOWED_TEAM}\",*]" ||
   principal.getTag("${TEAM_CLAIM}") == "${ALLOWED_TEAM}")
};

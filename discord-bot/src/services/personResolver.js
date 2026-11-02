/** A safe clarification, with no directory candidates or backend detail. */
export class PersonResolutionError extends Error {
  constructor(outcome, message) {
    super(message);
    this.outcome = outcome;
  }
}

/** Validate model references; stable directory IDs are never model selections. */
export function validatePersonReference(value) {
  if (typeof value === 'string' && /^[0-9]{1,20}$/.test(value)) {
    return Object.freeze({ id: value });
  }
  if (
    !value ||
    typeof value !== 'object' ||
    Array.isArray(value) ||
    !Object.keys(value).length ||
    Object.keys(value).some((key) => !['name', 'email'].includes(key))
  )
    throw new Error('Supply an explicit mention ID or a name/email reference.');
  const reference = {};
  for (const [key, max] of [
    ['name', 200],
    ['email', 320],
  ]) {
    if (value[key] === undefined) continue;
    if (
      typeof value[key] !== 'string' ||
      !value[key].trim() ||
      [...value[key]].length > max ||
      value[key].includes('\0')
    ) {
      throw new Error('Supply a nonblank name (up to 200 characters) or email (up to 320).');
    }
    reference[key] = value[key].trim().toLowerCase();
  }
  if (!Object.keys(reference).length) throw new Error('Supply a name or email reference.');
  return Object.freeze(reference);
}

/** One lookup path for team services and helper preparation; never guesses. */
export async function resolvePersonTarget(directory, { personId, discordSnowflake, reference }) {
  let person;
  if (personId) {
    person = await directory.getPerson(personId);
  } else if (discordSnowflake || reference?.id) {
    person = await directory.getPersonByDiscordId(discordSnowflake ?? reference.id);
    if (!person)
      throw new PersonResolutionError(
        'USER_NOT_LINKED',
        'That mentioned member is not linked. Ask them to link, or supply their directory name/email.',
      );
  } else {
    const filters = validatePersonReference(reference);
    const result = await directory.searchPeople(filters);
    if (result.has_more || result.needs_disambiguation || result.matches.length > 1) {
      throw new PersonResolutionError(
        'PERSON_AMBIGUOUS',
        'More detail is needed to identify a member. Ask for a more specific name or an explicit mention. For email-based clarification, ask the requester to send the full name and exact email to Misty in a DM. Never request or repeat email details in a shared reply. Nothing was changed.',
      );
    }
    person = result.matches[0];
  }
  if (!person)
    throw new PersonResolutionError(
      'PERSON_NOT_FOUND',
      'No active directory member matched. Ask for a corrected name or explicit mention; use a DM to Misty for email-based clarification. Nothing was changed.',
    );
  if (person.active === false)
    throw new PersonResolutionError(
      'PERSON_INACTIVE',
      'The selected directory member is inactive. Nothing was changed.',
    );
  return person;
}

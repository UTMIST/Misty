import {
  AccessLevelChangeForbidden,
  DirectoryUnavailable,
  PersonUpdateInvalid,
} from '../clients/directoryClient.js';
import { promotionDecision } from '../auth/policy.js';
import { resolvePersonTarget, PersonResolutionError } from './personResolver.js';

export const llmSafe = true;

// Any change to these fields after preview invalidates the proposal.
const SNAPSHOT_FIELDS = [
  'id',
  'display_name',
  'primary_email',
  'active',
  'access_level',
  'updated_at',
];

/** A rejected promotion with a safe, candidate-free message. */
export class PromotionRejected extends Error {
  constructor(outcome, message) {
    super(message);
    this.outcome = outcome;
  }
}

const DENIED_MESSAGE =
  'That promotion is not permitted. You can promote only another active member whose current global level is below yours, up to your own level. Nothing was changed.';

function rejectionFor(outcome) {
  if (outcome === 'UNCHANGED')
    return new PromotionRejected(
      outcome,
      'That member already has the requested global access level. Nothing was changed.',
    );
  return new PromotionRejected(outcome, DENIED_MESSAGE);
}

export function createPromotionService({ directory }) {
  // The caller is always the freshly authenticated principal, never an argument
  // supplied by a model or command option.
  function evaluate(caller, person, level) {
    if (person.id === caller?.id) return 'SELF';
    return promotionDecision({
      callerLevel: caller?.access_level,
      targetLevel: person.access_level,
      requestedLevel: level,
    });
  }

  async function preparePromotion({ caller, personId, discordSnowflake, reference, level }) {
    let person = await resolvePersonTarget(directory, { personId, discordSnowflake, reference });
    // Name/email search returns only a minimal match (no access_level or
    // updated_at), so re-read the full record by its stable ID.
    if (!personId && !discordSnowflake && !reference?.id)
      person = await resolvePersonTarget(directory, { personId: person.id });
    const outcome = evaluate(caller, person, level);
    if (outcome !== 'ALLOWED') throw rejectionFor(outcome);
    return Object.freeze(Object.fromEntries(SNAPSHOT_FIELDS.map((key) => [key, person[key]])));
  }

  async function checkPromotion({ caller, expectedPerson, level }) {
    const person = await preparePromotion({ caller, personId: expectedPerson.id, level });
    if (SNAPSHOT_FIELDS.some((key) => person[key] !== expectedPerson[key]))
      throw new PromotionRejected(
        'PERSON_CHANGED',
        'The selected member changed. Ask again for a fresh preview.',
      );
    return person;
  }

  async function promote({
    caller,
    personId,
    discordSnowflake,
    level,
    expectedPerson,
    beforeWrite,
  }) {
    let writeStarted = false;
    let person;
    try {
      person = expectedPerson
        ? await checkPromotion({ caller, expectedPerson, level })
        : await preparePromotion({ caller, personId, discordSnowflake, level });
      await beforeWrite?.();
      writeStarted = true;
      const updated = await directory.updatePersonAccessLevel(person.id, level);
      if (!updated) return { outcome: 'PERSON_NOT_FOUND' };
      // Report success only from the directory's own response.
      if (updated.id !== person.id || updated.access_level !== level)
        return { outcome: 'UPDATE_UNCERTAIN' };
      return { outcome: 'PROMOTED', person: updated, previousLevel: person.access_level };
    } catch (error) {
      if (error instanceof DirectoryUnavailable)
        return { outcome: writeStarted ? 'UPDATE_UNCERTAIN' : 'DIRECTORY_DOWN' };
      if (error instanceof AccessLevelChangeForbidden) return { outcome: 'SCOPE_MISSING' };
      if (error instanceof PersonUpdateInvalid) return { outcome: 'UPDATE_REJECTED' };
      if (error instanceof PromotionRejected || error instanceof PersonResolutionError)
        return { outcome: error.outcome };
      throw error;
    }
  }

  return { preparePromotion, checkPromotion, promote };
}
